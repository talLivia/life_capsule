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

    def _check_cuda(self) -> bool:
        try:
            import torch

            return torch.cuda.is_available()
        except Exception:
            return False

    def _build_model(self):
        """Synchronous model load — run inside a thread to avoid blocking the loop."""
        from faster_whisper import WhisperModel

        device = "cuda" if self._check_cuda() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        logger.info(f"Loading Whisper model {self.model_name!r} on {device} ({compute_type})…")
        model = WhisperModel(self.model_name, device=device, compute_type=compute_type)
        logger.info("Whisper model loaded")
        return model

    async def initialize(self) -> None:
        """Eager warm-up. Optional — `transcribe` will load on first call too."""
        if self.model is not None or self.provider != "whisper":
            return
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()
        async with self._load_lock:
            if self.model is None:
                self.model = await asyncio.to_thread(self._build_model)

    async def transcribe(self, audio_data: Union[bytes, str], language: str = "en") -> str:
        if self.provider != "whisper":
            raise ValueError(f"Unsupported STT provider: {self.provider}")
        if self.model is None:
            await self.initialize()
        return await asyncio.to_thread(self._transcribe_sync, audio_data, language)

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

    def _transcribe_sync(self, audio_data: Union[bytes, str], language: str) -> str:
        try:
            assert self.model is not None  # for type checker

            if isinstance(audio_data, str):
                exists = os.path.exists(audio_data)
                size = os.path.getsize(audio_data) if exists else -1
                _raw_trace(
                    f"_transcribe_sync ENTER path={audio_data!r} exists={exists} "
                    f"size_on_disk={size} lang={language} thread={threading.get_ident()}"
                )
            else:
                _raw_trace(
                    f"_transcribe_sync ENTER bytes len={len(audio_data)} "
                    f"lang={language} thread={threading.get_ident()}"
                )

            # Hand the path / raw bytes straight to faster-whisper: it decodes
            # via PyAV, which handles the browser's WebM/Opus mic recordings
            # (libsndfile can NOT — decoding with soundfile first broke every
            # voice turn coming from MediaRecorder). PyAV also resamples to
            # 16 kHz mono internally, so no librosa pass is needed.
            source = io.BytesIO(audio_data) if isinstance(audio_data, bytes) else audio_data
            with self._model_lock:
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
                    segments, info = self.model.transcribe(
                        source,
                        language=language,
                        beam_size=5,
                        vad_filter=False,
                    )
                    segments = list(segments)
                    _raw_trace(
                        f"_transcribe_sync PyAV OK duration={info.duration:.2f} "
                        f"lang_prob={info.language_probability:.2f} segments={len(segments)} "
                        f"seg_detail={[(round(s.start,2), round(s.end,2), s.text, round(s.avg_logprob,3), round(s.no_speech_prob,3)) for s in segments]!r}"
                    )
                    logger.info(
                        f"PyAV decode: duration={info.duration:.2f}s "
                        f"lang_prob={info.language_probability:.2f} segments={len(segments)}"
                    )
                    transcription = " ".join(seg.text for seg in segments).strip()
                except Exception as decode_err:
                    _raw_trace(f"_transcribe_sync PyAV FAILED: {type(decode_err).__name__}: {decode_err}")
                    logger.warning(f"PyAV decode failed ({decode_err}); retrying via soundfile")
                    audio = self._decode_with_soundfile(audio_data)
                    segments, info = self.model.transcribe(
                        audio,
                        language=language,
                        beam_size=5,
                        vad_filter=False,
                    )
                    segments = list(segments)
                    transcription = " ".join(seg.text for seg in segments).strip()
                    _raw_trace(f"_transcribe_sync soundfile fallback segments={len(segments)} text={transcription!r}")

            _raw_trace(f"_transcribe_sync RETURN text={transcription!r}")
            logger.info(f"Transcribed {len(transcription)} chars (lang={info.language})")
            return transcription

        except Exception as e:
            _raw_trace(f"_transcribe_sync EXCEPTION {type(e).__name__}: {e}")
            logger.error(f"Whisper transcription error: {e}")
            raise


stt_service = STTService()
