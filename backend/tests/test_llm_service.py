"""
Regression test for a confirmed live hang: with LLM_PROVIDER=gemini (this
deployment's active provider), genai.Client() built with no explicit
HttpOptions.timeout defaults that field to None, which the SDK passes
straight through to httpx/aiohttp as timeout=None — those libraries treat
None as "wait forever," not "use a sane default." A stalled Gemini call
anywhere in the video-clip pipeline (coreference resolution, perspective
normalization, topic/entity/semantic classification, per-candidate
verification) hung the whole WS turn indefinitely with no exception ever
raised — confirmed to be the root cause of a live "Finding a clip…" hang
with nothing in the browser console. anthropic/openai's clients already
default to a bounded (600s) timeout, but all three are pinned explicitly
in llm.py for the same guarantee regardless of which provider
LLM_PROVIDER selects — verified here for all three.
"""

import pytest

from app.services.llm import _DETERMINISTIC_SEED, LLM_CALL_TIMEOUT_SECONDS, LLMService


def test_anthropic_client_has_bounded_timeout(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")
    service = LLMService()
    assert service.client.timeout == LLM_CALL_TIMEOUT_SECONDS


def test_openai_client_has_bounded_timeout(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", None)
    service = LLMService()
    assert service.client.timeout == LLM_CALL_TIMEOUT_SECONDS


def test_ollama_client_has_bounded_timeout(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", None)
    service = LLMService()
    assert service.client.timeout == LLM_CALL_TIMEOUT_SECONDS


def test_gemini_client_has_bounded_timeout_not_none(monkeypatch):
    """The specific confirmed bug: HttpOptions.timeout left at its None
    default means "wait forever," not a sane default — this must never
    regress back to an implicit/unset timeout."""
    from app.config import settings

    monkeypatch.setattr(settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    service = LLMService()
    configured_timeout_ms = service.client._api_client._http_options.timeout
    assert configured_timeout_ms is not None
    assert configured_timeout_ms == LLM_CALL_TIMEOUT_SECONDS * 1000


# ── deterministic seed (reproducibility on Gemini/OpenAI) ────────────────────


@pytest.mark.asyncio
async def test_gemini_call_pins_deterministic_seed(monkeypatch):
    """Gemini's GenerateContentConfig.seed defaults to a RANDOM number, so
    identical temperature-0 prompts still varied run to run — the observed
    non-determinism. This pins a fixed seed on every generate_response call
    and must not regress to an unset (random) seed."""
    from app.config import settings

    monkeypatch.setattr(settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    service = LLMService()

    captured = {}

    class _FakeResp:
        text = "[]"
        usage_metadata = None

    async def fake_generate_content(model, contents, config):
        captured["config"] = config
        return _FakeResp()

    monkeypatch.setattr(service.client.aio.models, "generate_content", fake_generate_content)

    out = await service.generate_response(
        [{"role": "user", "content": "hi"}], system_prompt="sys", temperature=0
    )
    assert out == "[]"
    assert captured["config"].seed == _DETERMINISTIC_SEED
    assert captured["config"].temperature == 0


@pytest.mark.asyncio
async def test_openai_call_pins_deterministic_seed(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", None)
    service = LLMService()

    captured = {}

    class _Msg:
        content = "[]"

    class _Choice:
        message = _Msg()

    class _FakeResp:
        choices = [_Choice()]

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeResp()

    monkeypatch.setattr(service.client.chat.completions, "create", fake_create)

    out = await service.generate_response(
        [{"role": "user", "content": "hi"}], system_prompt="sys", temperature=0
    )
    assert out == "[]"
    assert captured["seed"] == _DETERMINISTIC_SEED


def test_gemini_usage_log_reports_cached_token_count(monkeypatch, caplog):
    """The archive-read call's prompt is built for prompt caching (static
    archive first, question last), but nothing was reporting whether a cache
    was ever HIT — the log carried only in/out tokens, so a permanent 0 and a
    working cache looked identical from outside. Pinned here because the value
    of this field is entirely that someone notices when it CHANGES."""
    import logging

    service = LLMService.__new__(LLMService)  # no client needed for the logger

    class _Usage:
        prompt_token_count = 3604
        candidates_token_count = 134
        cached_content_token_count = 3594

    with caplog.at_level(logging.INFO):
        service._log_gemini_usage(_Usage())

    record = next(r for r in caplog.records if r.msg == "llm_usage")
    assert record.cache_read_tokens == 3594
    assert record.in_tokens == 3604
    assert record.out_tokens == 134
    assert record.provider == "gemini"


def test_gemini_usage_log_handles_absent_cache_field():
    """Real responses omit cached_content_token_count entirely when nothing was
    cached — which is every call today. That must log 0, not raise."""
    import logging

    service = LLMService.__new__(LLMService)

    class _Usage:
        prompt_token_count = 3604
        candidates_token_count = 134
        # no cached_content_token_count at all

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("app.services.llm")
    logger.addHandler(handler)
    try:
        service._log_gemini_usage(_Usage())
    finally:
        logger.removeHandler(handler)

    record = next(r for r in records if r.msg == "llm_usage")
    assert record.cache_read_tokens == 0
