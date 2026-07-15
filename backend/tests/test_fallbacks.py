"""
Tests for the TTS fallback chain and LLM provider wiring.

The TTS degradation path matters for UX: when Chatterbox can't load (no GPU,
missing model), users should get Microsoft Edge neural voices — not the
robotic gTTS — and the result must be labelled so the UI can warn that voice
cloning was dropped.
"""

from unittest.mock import AsyncMock

import pytest

from app.services.tts import TTSService

pytestmark = pytest.mark.asyncio


def test_legacy_coqui_provider_aliases_to_chatterbox(monkeypatch):
    """Old .env files with TTS_PROVIDER=coqui must keep working."""
    from app.services import tts as tts_module

    monkeypatch.setattr(tts_module.settings, "TTS_PROVIDER", "coqui")
    service = TTSService()
    assert service.provider == "chatterbox"


async def test_tts_falls_back_to_edge_when_chatterbox_unavailable(monkeypatch, tmp_path):
    service = TTSService()
    out = str(tmp_path / "out.wav")

    monkeypatch.setattr(service, "initialize", AsyncMock(side_effect=RuntimeError("no chatterbox")))
    monkeypatch.setattr(service, "_edge_fallback", AsyncMock(return_value=out))
    gtts = AsyncMock()
    monkeypatch.setattr(service, "_gtts_fallback", gtts)

    result = await service.synthesize("Hello world", out, speaker_wav=None, language="en")

    assert result.engine == "edge-tts"
    assert result.fallback is True
    assert result.voice_cloned is False
    gtts.assert_not_awaited()  # gTTS is last resort only


async def test_tts_falls_back_to_gtts_when_edge_also_fails(monkeypatch, tmp_path):
    service = TTSService()
    out = str(tmp_path / "out.wav")

    monkeypatch.setattr(service, "initialize", AsyncMock(side_effect=RuntimeError("no chatterbox")))
    monkeypatch.setattr(service, "_edge_fallback", AsyncMock(side_effect=RuntimeError("edge down")))
    monkeypatch.setattr(service, "_gtts_fallback", AsyncMock(return_value=out))

    result = await service.synthesize("Hello world", out, speaker_wav=None, language="en")

    assert result.engine == "gtts"
    assert result.fallback is True


def test_llm_ollama_provider_uses_openai_compatible_client(monkeypatch):
    """LLM_PROVIDER=ollama wires an OpenAI client at the local base URL."""
    from app.services import llm as llm_module

    monkeypatch.setattr(llm_module.settings, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(llm_module.settings, "OPENAI_BASE_URL", None)
    monkeypatch.setattr(llm_module.settings, "OPENAI_API_KEY", "")

    service = llm_module.LLMService()
    assert service.provider == "openai"  # downstream paths are the OpenAI ones
    assert "localhost:11434" in str(service.client.base_url)


def test_llm_openai_provider_respects_custom_base_url(monkeypatch):
    from app.services import llm as llm_module

    monkeypatch.setattr(llm_module.settings, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(llm_module.settings, "OPENAI_BASE_URL", "http://vllm:8001/v1")
    monkeypatch.setattr(llm_module.settings, "OPENAI_API_KEY", "k")

    service = llm_module.LLMService()
    assert "vllm:8001" in str(service.client.base_url)
