"""
Tests for stt.py's two additions:

Prompt 11 — preserving phrase/word-level timestamps alongside the existing
plain-text transcription contract.

Prompt 11 follow-up — splitting the live-conversation model (WHISPER_MODEL,
fast) from the ingestion-only model (WHISPER_MODEL_INGESTION, larger/more
accurate) after benchmarking showed "medium" was both ~3x slower AND
occasionally produced a hallucinated, looping wrong transcription on real
short live-question-length clips — unacceptable live, fine offline.

No real Whisper model here — `.transcribe` is mocked on whichever fake
model object each method is expected to use, returning real faster-whisper
`Segment`/`Word` namedtuples (so field names are guaranteed accurate rather
than hand-rolled stand-ins).
"""
from unittest.mock import MagicMock

import pytest
from faster_whisper.transcribe import Segment, Word

from app.config import settings
from app.services.stt import STTService

pytestmark = pytest.mark.asyncio


def _segment(start, end, text, words=None):
    return Segment(
        id=0,
        seek=0,
        start=start,
        end=end,
        text=text,
        tokens=[],
        avg_logprob=-0.1,
        compression_ratio=1.0,
        no_speech_prob=0.01,
        words=words,
        temperature=0.0,
    )


def _word(word, start, end):
    return Word(start=start, end=end, word=word, probability=0.9)


@pytest.fixture(autouse=True)
def _force_local_stt(monkeypatch):
    """Pin every test to the LOCAL live path regardless of what .env says.
    Without this, LIVE_STT_PROVIDER=deepgram would send the whole suite down
    the Deepgram branch — tests would depend on a key and the network, and
    would only "pass" because a fake path raises before the request goes out.
    The Deepgram tests below opt in explicitly."""
    monkeypatch.setattr(settings, "LIVE_STT_PROVIDER", "local")


@pytest.fixture
def service():
    """Both the live-conversation model (`transcribe`) and the ingestion
    model (`transcribe_with_timestamps`) pre-populated as separate mocks —
    covers whichever method a given test calls without either one lazily
    trying to load a real Whisper model."""
    svc = STTService()
    svc.model = MagicMock(name="live_model")
    svc.ingestion_model = MagicMock(name="ingestion_model")
    return svc


def _fake_info(duration=5.0, language="en", language_probability=1.0):
    info = MagicMock()
    info.duration = duration
    info.language = language
    info.language_probability = language_probability
    return info


async def test_transcribe_with_timestamps_preserves_phrase_and_word_timing(service):
    segments = [
        _segment(0.0, 2.0, " Hello there.", words=[_word("Hello", 0.0, 0.5), _word("there.", 0.5, 2.0)]),
        _segment(2.0, 4.5, " How are you?", words=[_word("How", 2.0, 2.3), _word("are", 2.3, 2.5), _word("you?", 2.5, 4.5)]),
    ]
    service.ingestion_model.transcribe.return_value = (segments, _fake_info())

    result = await service.transcribe_with_timestamps("fake/path.webm", language="en")

    assert result["text"] == "Hello there. How are you?"
    assert len(result["phrases"]) == 2
    assert result["phrases"][0]["start_sec"] == 0.0
    assert result["phrases"][0]["end_sec"] == 2.0
    assert result["phrases"][0]["text"] == "Hello there."
    assert result["phrases"][0]["words"] == [
        {"word": "Hello", "start_sec": 0.0, "end_sec": 0.5},
        {"word": "there.", "start_sec": 0.5, "end_sec": 2.0},
    ]
    assert result["phrases"][1]["text"] == "How are you?"

    # word_timestamps=True must actually be requested from faster-whisper —
    # without it, .words would be None and Prompt 13 has nothing to pinpoint.
    _, kwargs = service.ingestion_model.transcribe.call_args
    assert kwargs["word_timestamps"] is True
    assert kwargs["vad_filter"] is False

    # And this must NOT have touched the live-conversation model at all.
    service.model.transcribe.assert_not_called()


async def test_transcribe_with_timestamps_handles_missing_words(service):
    """A segment with words=None (word_timestamps wasn't actually available
    for some reason) shouldn't crash — just yields an empty words list for
    that phrase rather than failing the whole transcription."""
    segments = [_segment(0.0, 1.0, "hi", words=None)]
    service.ingestion_model.transcribe.return_value = (segments, _fake_info())

    result = await service.transcribe_with_timestamps("fake/path.webm", language="en")

    assert result["phrases"][0]["words"] == []


async def test_transcribe_unchanged_contract_returns_plain_string(service):
    """The existing, widely-used `transcribe()` (live conversation STT,
    websocket.py's _handle_audio_inner) must still return a bare string —
    Prompt 11 only ADDS transcribe_with_timestamps, it doesn't change this
    method's signature or behavior for any existing caller."""
    segments = [_segment(0.0, 1.0, "hello", words=[_word("hello", 0.0, 1.0)])]
    service.model.transcribe.return_value = (segments, _fake_info())

    result = await service.transcribe("fake/path.webm", language="en")

    assert result == "hello"
    assert isinstance(result, str)


async def test_transcribe_uses_exactly_one_call_on_the_live_model(service):
    """transcribe() routes through _transcribe_sync_full exactly once against
    its own model — confirms it doesn't burn a second, redundant Whisper
    pass just because transcribe_with_timestamps/ingestion_model also exist."""
    segments = [_segment(0.0, 1.0, "hi", words=[])]
    service.model.transcribe.return_value = (segments, _fake_info())

    await service.transcribe("fake/path.webm", language="en")

    assert service.model.transcribe.call_count == 1
    service.ingestion_model.transcribe.assert_not_called()


# ── Live-conversation / ingestion model independence ────────────────────────


async def test_transcribe_and_transcribe_with_timestamps_use_different_models(service):
    """The whole point of the split: live conversation (`transcribe`) and
    ingestion (`transcribe_with_timestamps`) must call DIFFERENT model
    objects, never each other's. Benchmarked directly (not in this test
    suite) that sharing one model here would either slow live turns back
    down to the pre-fix ~12s/turn latency, or risk a live turn hitting
    medium's occasional 30-60s hallucinated-loop failure mode."""
    live_segments = [_segment(0.0, 1.0, "live turn", words=[])]
    ingestion_segments = [_segment(0.0, 1.0, "ingested phrase", words=[])]
    service.model.transcribe.return_value = (live_segments, _fake_info())
    service.ingestion_model.transcribe.return_value = (ingestion_segments, _fake_info())

    live_result = await service.transcribe("live.webm", language="he")
    ingestion_result = await service.transcribe_with_timestamps("ingest.webm", language="he")

    assert live_result == "live turn"
    assert ingestion_result["text"] == "ingested phrase"
    service.model.transcribe.assert_called_once()
    service.ingestion_model.transcribe.assert_called_once()


async def test_live_and_ingestion_locks_are_independent_objects(service):
    """Two separate threading.Locks, not one shared lock — a slow ingestion
    transcription (e.g. a long recording on the larger model) must never
    make a live /talk turn wait behind it."""
    assert service._model_lock is not service._ingestion_model_lock


async def test_initialize_loads_only_the_live_model_with_its_own_configured_name(monkeypatch):
    """initialize() (eager warm-up) must load WHISPER_MODEL into `self.model`
    and must NOT touch the ingestion model at all — ingestion only ever
    loads lazily on its own first real use from analysis_graph.py."""
    svc = STTService()
    svc.model_name = "small"
    svc.ingestion_model_name = "medium"
    built_with = []

    def fake_build(model_name):
        built_with.append(model_name)
        return MagicMock(name=f"built-{model_name}")

    monkeypatch.setattr(svc, "_build_model", fake_build)

    await svc.initialize()

    assert built_with == ["small"]
    assert svc.model is not None
    assert svc.ingestion_model is None


async def test_initialize_ingestion_loads_only_the_ingestion_model(monkeypatch):
    svc = STTService()
    svc.model_name = "small"
    svc.ingestion_model_name = "medium"
    built_with = []

    def fake_build(model_name):
        built_with.append(model_name)
        return MagicMock(name=f"built-{model_name}")

    monkeypatch.setattr(svc, "_build_model", fake_build)

    await svc._initialize_ingestion()

    assert built_with == ["medium"]
    assert svc.ingestion_model is not None
    assert svc.model is None


async def test_transcribe_with_timestamps_lazily_loads_ingestion_model(monkeypatch):
    """A fresh STTService (nothing loaded yet) must load the INGESTION
    model, not the live one, the first time transcribe_with_timestamps is
    called — mirrors how transcribe() already lazily loads `self.model`."""
    svc = STTService()
    svc.ingestion_model_name = "medium"
    fake_model = MagicMock()
    fake_model.transcribe.return_value = (
        [_segment(0.0, 1.0, "hi", words=[])],
        _fake_info(),
    )

    async def fake_initialize_ingestion():
        svc.ingestion_model = fake_model

    monkeypatch.setattr(svc, "_initialize_ingestion", fake_initialize_ingestion)

    result = await svc.transcribe_with_timestamps("fake/path.webm", language="en")

    assert result["text"] == "hi"
    fake_model.transcribe.assert_called_once()


# ── live-path provider routing (Deepgram batch swap) ────────────────────────


async def test_use_deepgram_requires_both_flag_and_key(service, monkeypatch):
    monkeypatch.setattr(settings, "LIVE_STT_PROVIDER", "deepgram")
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "")
    assert service._use_deepgram() is False, "no key must mean stay local"

    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "present")
    assert service._use_deepgram() is True

    monkeypatch.setattr(settings, "LIVE_STT_PROVIDER", "local")
    assert service._use_deepgram() is False


async def test_live_transcribe_uses_deepgram_and_skips_the_local_model(
    service, monkeypatch
):
    monkeypatch.setattr(settings, "LIVE_STT_PROVIDER", "deepgram")
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "present")

    async def fake_dg(audio_data, language):
        return "שירתתי בחיל האוויר"

    monkeypatch.setattr(service, "_transcribe_deepgram", fake_dg)

    result = await service.transcribe(b"audio", language="he")

    assert result == "שירתתי בחיל האוויר"
    service.model.transcribe.assert_not_called()  # local model untouched


async def test_live_transcribe_falls_back_to_local_when_deepgram_fails(
    service, monkeypatch
):
    """A third-party outage must never fail the turn — the local model is kept
    warm precisely so this fallback is real."""
    monkeypatch.setattr(settings, "LIVE_STT_PROVIDER", "deepgram")
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "present")

    async def boom(audio_data, language):
        raise RuntimeError("Deepgram HTTP 503")

    monkeypatch.setattr(service, "_transcribe_deepgram", boom)
    segments = [_segment(0.0, 1.0, "fallback text", words=[_word("fallback", 0.0, 1.0)])]
    service.model.transcribe.return_value = (segments, _fake_info(language="he"))

    result = await service.transcribe(b"audio", language="he")

    assert result == "fallback text"
    service.model.transcribe.assert_called_once()


async def test_ingestion_never_uses_deepgram(service, monkeypatch):
    """The flag governs the LIVE path only. Ingestion runs offline where
    accuracy beats latency, and must not ship the archive to a third party."""
    monkeypatch.setattr(settings, "LIVE_STT_PROVIDER", "deepgram")
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "present")

    async def should_not_run(audio_data, language):
        raise AssertionError("ingestion must never call Deepgram")

    monkeypatch.setattr(service, "_transcribe_deepgram", should_not_run)
    segments = [_segment(0.0, 1.0, "ingested", words=[_word("ingested", 0.0, 1.0)])]
    service.ingestion_model.transcribe.return_value = (segments, _fake_info(language="he"))

    result = await service.transcribe_with_timestamps(b"audio", language="he")

    assert result["text"] == "ingested"
    service.ingestion_model.transcribe.assert_called_once()
