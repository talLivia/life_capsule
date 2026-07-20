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


def test_llm_gemini_provider_builds_client(monkeypatch):
    """LLM_PROVIDER=gemini reuses GEMINI_API_KEY — no separate Anthropic/
    OpenAI account needed for analysis_graph.py's own topic/entity/importance
    calls (as distinct from Graphiti's own GRAPHITI_LLM_PROVIDER)."""
    from app.services import llm as llm_module

    monkeypatch.setattr(llm_module.settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(llm_module.settings, "LLM_MODEL", "gemini-flash-latest")
    monkeypatch.setattr(llm_module.settings, "GEMINI_API_KEY", "test-key")

    service = llm_module.LLMService()
    assert service.provider == "gemini"
    assert service.model == "gemini-flash-latest"


def test_map_gemini_exception_rate_limit():
    from google.genai import errors as genai_errors

    from app.services.llm import LLMRateLimited, _map_gemini_exception

    exc = genai_errors.ClientError(429, {"message": "rate limited"})
    assert isinstance(_map_gemini_exception(exc), LLMRateLimited)


def test_map_gemini_exception_auth():
    from google.genai import errors as genai_errors

    from app.services.llm import LLMAuthError, _map_gemini_exception

    exc = genai_errors.ClientError(403, {"message": "forbidden"})
    assert isinstance(_map_gemini_exception(exc), LLMAuthError)


def test_map_gemini_exception_server_error():
    from google.genai import errors as genai_errors

    from app.services.llm import LLMUnavailable, _map_gemini_exception

    exc = genai_errors.ServerError(500, {"message": "oops"})
    assert isinstance(_map_gemini_exception(exc), LLMUnavailable)


def test_gemini_contents_maps_assistant_role_to_model(monkeypatch):
    from app.services import llm as llm_module

    monkeypatch.setattr(llm_module.settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(llm_module.settings, "GEMINI_API_KEY", "test-key")
    service = llm_module.LLMService()

    contents = service._gemini_contents(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    )
    assert contents[0].role == "user"
    assert contents[1].role == "model"
