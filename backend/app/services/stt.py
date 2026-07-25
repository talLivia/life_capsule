"""
Speech-to-text service backed by faster-whisper.

The Whisper model is several hundred MB and takes 30–60 s to load on a cold
start. We defer loading until the first transcription so FastAPI's lifespan
hook stays fast and the /health endpoint becomes available promptly.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import numpy as np
import soundfile as sf

from app.config import settings

logger = logging.getLogger(__name__)

# Hard ceiling on a single LIVE transcription so a hung model.transcribe()
# can't leave a /talk turn stuck forever (same class of bug as the unbounded
# Gemini client timeout fixed in llm.py). Generous: medium on this CPU box is
# ~9s for a short question, so 120s is far above any legitimate live turn but
# still catches a pathological infinite hang. Only the live path is bounded —
# ingestion (transcribe_with_timestamps) processes whole recordings offline
# and isn't on any turn's critical path.
_LIVE_TRANSCRIBE_TIMEOUT_SECONDS = 120

_RAW_TRACE_PATH = Path(tempfile.gettempdir()) / "avatar_raw_trace.log"


def _raw_trace(msg: str) -> None:
    try:
        with open(_RAW_TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} [stt] {msg}\n")
    except OSError:
        pass


class STTService:
    def __init__(self):
        self.provider = settings.STT_PROVIDER
        self.model_name = settings.WHISPER_MODEL
        self.model = None
        # Lock ensures the model is loaded exactly once even under burst load.
        self._load_lock: Optional[asyncio.Lock] = None
        # A real (threading, not asyncio) lock around every model.transcribe()
        # call. Continuous conversation mode fires a new "audio" WS message
        # every few seconds; interrupt_active_turn cancels the PREVIOUS
        # turn's asyncio Task, but that does NOT stop its transcribe() call
        # already running inside asyncio.to_thread — the underlying thread
        # keeps executing regardless of the Task's cancellation. CTranslate2
        # models (which faster-whisper is built on) aren't documented as
        # safe for concurrent inference from multiple threads, so without
        # this lock a still-running "cancelled" call and a fresh one could
        # overlap inside the same model instance — a plausible source of the
        # empty/garbled transcriptions seen under rapid-fire real usage.
        self._model_lock = threading.Lock()

        # Separate model + lock for analysis_graph.py's ingestion pipeline
        # ONLY (transcribe_with_timestamps, below) — deliberately NOT shared
        # with the live-conversation model/lock above. Two independent
        # threading.Locks (rather than one shared lock) so a slow ingestion
        # transcription can never make a live /talk turn wait behind it —
        # see WHISPER_MODEL_INGESTION's config.py comment for why these two
        # paths need genuinely different models, not just different names
        # for the same one.
        self.ingestion_model_name = settings.WHISPER_MODEL_INGESTION
        self.ingestion_model = None
        self._ingestion_load_lock: Optional[asyncio.Lock] = None
        self._ingestion_model_lock = threading.Lock()

    def _check_cuda(self) -> bool:
        try:
            import torch

            return torch.cuda.is_available()
        except Exception:
            return False

    def _build_model(self, model_name: str):
        """Synchronous model load — run inside a thread to avoid blocking the loop."""
        from faster_whisper import WhisperModel

        device = "cuda" if self._check_cuda() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        logger.info(f"Loading Whisper model {model_name!r} on {device} ({compute_type})…")
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        logger.info(f"Whisper model {model_name!r} loaded")
        return model

    async def initialize(self) -> None:
        """Eager warm-up for the LIVE-conversation model. Optional —
        `transcribe` will load on first call too. Does NOT load the
        ingestion model — that only ever loads lazily on first actual use
        from analysis_graph.py, since local dev / a fresh process may never
        touch the ingestion path at all in a given run."""
        if self.model is not None or self.provider != "whisper":
            return
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()
        async with self._load_lock:
            if self.model is None:
                self.model = await asyncio.to_thread(self._build_model, self.model_name)

    async def _initialize_ingestion(self) -> None:
        if self.ingestion_model is not None or self.provider != "whisper":
            return
        if self._ingestion_load_lock is None:
            self._ingestion_load_lock = asyncio.Lock()
        async with self._ingestion_load_lock:
            if self.ingestion_model is None:
                self.ingestion_model = await asyncio.to_thread(
                    self._build_model, self.ingestion_model_name
                )

    async def transcribe(self, audio_data: Union[bytes, str], language: str = "en") -> str:
        """Live-conversation STT (websocket.py's _handle_audio_inner, via
        gpu_client.py) — always uses WHISPER_MODEL (fast), never
        WHISPER_MODEL_INGESTION."""
        if self.provider != "whisper":
            raise ValueError(f"Unsupported STT provider: {self.provider}")
        if self.model is None:
            await self.initialize()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._transcribe_sync_full, audio_data, language, self.model, self._model_lock
                ),
                timeout=_LIVE_TRANSCRIBE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as e:
            # The awaiting turn is freed with a real error (caught by
            # _handle_audio_inner) instead of hanging forever; the orphaned
            # worker thread can't be killed but finishes on its own in the
            # background. Note: it still holds _model_lock until it does, so
            # the next live turn briefly waits — acceptable vs. a stuck turn.
            logger.error(f"Live STT timed out after {_LIVE_TRANSCRIBE_TIMEOUT_SECONDS}s")
            raise RuntimeError(
                f"Transcription timed out after {_LIVE_TRANSCRIBE_TIMEOUT_SECONDS}s"
            ) from e
        return result["text"]

    async def transcribe_with_timestamps(
        self, audio_data: Union[bytes, str], language: str = "en"
    ) -> dict:
        """Ingestion-only STT (analysis_graph.py's transcribe_node, Prompt
        11's TranscriptChunk creation) — always uses WHISPER_MODEL_INGESTION
        (larger/more accurate, offline-only), never the live-conversation
        model. Returns phrase/word-level timing alongside the plain joined
        text: {"text": str, "phrases": [{"start_sec", "end_sec", "text",
        "words": [...]}]}. `words` is a list of {"word", "start_sec",
        "end_sec"} dicts. Does exactly one model.transcribe() call
        (word_timestamps is always on internally)."""
        if self.provider != "whisper":
            raise ValueError(f"Unsupported STT provider: {self.provider}")
        if self.ingestion_model is None:
            await self._initialize_ingestion()
        return await asyncio.to_thread(
            self._transcribe_sync_full,
            audio_data,
            language,
            self.ingestion_model,
            self._ingestion_model_lock,
        )

    def _decode_with_soundfile(self, audio_data: Union[bytes, str]) -> np.ndarray:
        """Fallback decoder for formats PyAV chokes on (raw WAV/FLAC/OGG)."""
        if isinstance(audio_data, bytes):
            audio, sample_rate = sf.read(io.BytesIO(audio_data))
        else:
            audio, sample_rate = sf.read(audio_data)

        # Mono mixdown
        if len(audio.shape) > 1:
            audio = audio.mean(axis=1)

        # Resample to 16 kHz (Whisper's expected rate)
        if sample_rate != 16000:
            import librosa

            audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=16000)

        return audio.astype(np.float32)

    @staticmethod
    def _segments_to_phrases(segments) -> list[dict]:
        """Convert faster-whisper Segment objects (with word_timestamps=True)
        into the plain-dict phrase shape the rest of the app uses — one dict
        per Whisper-detected phrase/sentence, each carrying its own
        word-level timing. Never grouped into fixed-duration windows; each
        natural phrase boundary Whisper detected becomes its own entry."""
        phrases = []
        for seg in segments:
            words = [
                {"word": w.word, "start_sec": w.start, "end_sec": w.end}
                for w in (seg.words or [])
            ]
            phrases.append(
                {
                    "start_sec": seg.start,
                    "end_sec": seg.end,
                    "text": seg.text.strip(),
                    "words": words,
                }
            )
        return phrases

    def _transcribe_sync_full(
        self,
        audio_data: Union[bytes, str],
        language: str,
        model,
        lock: threading.Lock,
    ) -> dict:
        """Does the actual model.transcribe() call exactly once and returns
        everything callers need: the plain joined text (transcribe()'s
        contract, unchanged) AND, additively, per-phrase/word timestamps
        (needed by analysis_graph.py's chunk creation, Prompt 11). `model`/
        `lock` are passed in rather than read from self so the live-
        conversation and ingestion paths (transcribe/transcribe_with_
        timestamps) can each use their own independently-configured model
        and lock without one ever blocking behind the other."""
        try:
            assert model is not None  # for type checker

            if isinstance(audio_data, str):
                exists = os.path.exists(audio_data)
                size = os.path.getsize(audio_data) if exists else -1
                _raw_trace(
                    f"_transcribe_sync_full ENTER path={audio_data!r} exists={exists} "
                    f"size_on_disk={size} lang={language} thread={threading.get_ident()}"
                )
            else:
                _raw_trace(
                    f"_transcribe_sync_full ENTER bytes len={len(audio_data)} "
                    f"lang={language} thread={threading.get_ident()}"
                )

            # Hand the path / raw bytes straight to faster-whisper: it decodes
            # via PyAV, which handles the browser's WebM/Opus mic recordings
            # (libsndfile can NOT — decoding with soundfile first broke every
            # voice turn coming from MediaRecorder). PyAV also resamples to
            # 16 kHz mono internally, so no librosa pass is needed.
            source = io.BytesIO(audio_data) if isinstance(audio_data, bytes) else audio_data
            with lock:
                try:
                    # vad_filter=False: our OWN client-side VAD
                    # (useContinuousVoiceInput.ts) already gates what audio
                    # ever reaches here — every segment sent has already been
                    # confirmed to contain real detected speech. Whisper's
                    # bundled Silero VAD is a SECOND, independent, more
                    # aggressive filter on top of that, and it was discarding
                    # entire short real utterances (3-4.5s clips, confirmed
                    # live: "הלו"/"היי"/"אני לא"/"תודה רבה" all real, coherent
                    # Hebrew, all zero-segment'd out with vad_filter=True) —
                    # the exact cause of "Could not transcribe audio" for
                    # perfectly good recordings.
                    segments, info = model.transcribe(
                        source,
                        language=language,
                        beam_size=5,
                        vad_filter=False,
                        word_timestamps=True,
                    )
                    segments = list(segments)
                    _raw_trace(
                        f"_transcribe_sync_full PyAV OK duration={info.duration:.2f} "
                        f"lang_prob={info.language_probability:.2f} segments={len(segments)} "
                        f"seg_detail={[(round(s.start,2), round(s.end,2), s.text, round(s.avg_logprob,3), round(s.no_speech_prob,3)) for s in segments]!r}"
                    )
                    logger.info(
                        f"PyAV decode: duration={info.duration:.2f}s "
                        f"lang_prob={info.language_probability:.2f} segments={len(segments)}"
                    )
                except Exception as decode_err:
                    _raw_trace(f"_transcribe_sync_full PyAV FAILED: {type(decode_err).__name__}: {decode_err}")
                    logger.warning(f"PyAV decode failed ({decode_err}); retrying via soundfile")
                    audio = self._decode_with_soundfile(audio_data)
                    segments, info = model.transcribe(
                        audio,
                        language=language,
                        beam_size=5,
                        vad_filter=False,
                        word_timestamps=True,
                    )
                    segments = list(segments)
                    _raw_trace(f"_transcribe_sync_full soundfile fallback segments={len(segments)}")

            phrases = self._segments_to_phrases(segments)
            transcription = " ".join(p["text"] for p in phrases).strip()

            _raw_trace(f"_transcribe_sync_full RETURN text={transcription!r} phrases={len(phrases)}")
            logger.info(f"Transcribed {len(transcription)} chars (lang={info.language})")
            return {"text": transcription, "phrases": phrases}

        except Exception as e:
            _raw_trace(f"_transcribe_sync_full EXCEPTION {type(e).__name__}: {e}")
            logger.error(f"Whisper transcription error: {e}")
            raise


stt_service = STTService()
