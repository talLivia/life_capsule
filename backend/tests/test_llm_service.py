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
from google.genai import errors as genai_errors

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


# ── 503-only retry (measured burst behavior, 2026-08-16 control run) ─────────


def _gemini_service(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    return LLMService()


class _Fake503(genai_errors.ServerError):
    def __init__(self):
        Exception.__init__(self, "503 UNAVAILABLE. high demand")
        self.code = 503


class _Fake429(genai_errors.ClientError):
    def __init__(self):
        Exception.__init__(self, "429 RESOURCE_EXHAUSTED")
        self.code = 429


class _FakeResp:
    text = "ok"
    usage_metadata = None


def _scripted_generate(outcomes, calls):
    """generate_content stub that raises/returns per the outcomes script."""

    async def fake(model, contents, config):
        calls.append(1)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return fake


@pytest.fixture
def no_backoff(monkeypatch):
    """Retries shouldn't actually sleep in tests; record the backoffs."""
    from app.services import llm as llm_module

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(llm_module, "_sleep", fake_sleep)
    return slept


@pytest.mark.asyncio
async def test_first_try_success_makes_exactly_one_call(monkeypatch, no_backoff):
    service = _gemini_service(monkeypatch)
    calls = []
    monkeypatch.setattr(
        service.client.aio.models, "generate_content",
        _scripted_generate([_FakeResp()], calls),
    )
    out = await service.generate_response(
        [{"role": "user", "content": "hi"}], system_prompt="sys"
    )
    assert out == "ok"
    assert len(calls) == 1
    assert no_backoff == []  # no retry, no sleep


@pytest.mark.asyncio
async def test_one_503_then_success_retries_once(monkeypatch, no_backoff):
    service = _gemini_service(monkeypatch)
    calls = []
    monkeypatch.setattr(
        service.client.aio.models, "generate_content",
        _scripted_generate([_Fake503(), _FakeResp()], calls),
    )
    out = await service.generate_response(
        [{"role": "user", "content": "hi"}], system_prompt="sys"
    )
    assert out == "ok"
    assert len(calls) == 2
    assert no_backoff == [2.0]


@pytest.mark.asyncio
async def test_two_503s_then_success_uses_the_second_retry(monkeypatch, no_backoff):
    """The deep-burst case the control run showed one retry cannot cover —
    back-to-back 503s recovered only by the second, longer-backoff attempt."""
    service = _gemini_service(monkeypatch)
    calls = []
    monkeypatch.setattr(
        service.client.aio.models, "generate_content",
        _scripted_generate([_Fake503(), _Fake503(), _FakeResp()], calls),
    )
    out = await service.generate_response(
        [{"role": "user", "content": "hi"}], system_prompt="sys"
    )
    assert out == "ok"
    assert len(calls) == 3
    assert no_backoff == [2.0, 4.0]


@pytest.mark.asyncio
async def test_three_503s_exhaust_and_raise_unavailable(monkeypatch, no_backoff):
    """Exhaustion surfaces LLMUnavailable — which _read_archive_for_ranges
    maps to read_failed, and the renderers map to the transient-failure
    line (each link pinned by its own test: the read_failed select_units
    test and test_spoken_answer's transient-line test)."""
    from app.services.llm import LLMUnavailable

    service = _gemini_service(monkeypatch)
    calls = []
    monkeypatch.setattr(
        service.client.aio.models, "generate_content",
        _scripted_generate([_Fake503(), _Fake503(), _Fake503()], calls),
    )
    with pytest.raises(LLMUnavailable):
        await service.generate_response(
            [{"role": "user", "content": "hi"}], system_prompt="sys"
        )
    assert len(calls) == 3  # first try + exactly two retries, never more


@pytest.mark.asyncio
async def test_non_503_errors_never_retry(monkeypatch, no_backoff):
    """Scoping guarantee: a 429 means back off, not hammer — one attempt,
    fail honestly, no sleep."""
    from app.services.llm import LLMRateLimited

    service = _gemini_service(monkeypatch)
    calls = []
    monkeypatch.setattr(
        service.client.aio.models, "generate_content",
        _scripted_generate([_Fake429()], calls),
    )
    with pytest.raises(LLMRateLimited):
        await service.generate_response(
            [{"role": "user", "content": "hi"}], system_prompt="sys"
        )
    assert len(calls) == 1
    assert no_backoff == []


@pytest.mark.asyncio
async def test_gemini_per_call_timeout_override(monkeypatch):
    """The archive read passes timeout= (seconds); it must land in the
    request config as HttpOptions milliseconds — the per-call override of
    the client-wide 30s guard (2026-08-31: that guard, doubling as
    X-Server-Timeout, was 504-killing every 30s+ whole-archive read)."""
    from app.config import settings

    monkeypatch.setattr(settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    service = LLMService()

    captured = {}

    class _FakeResp:
        text = "ok"
        usage_metadata = None

    async def fake_generate_content(*, model, contents, config):
        captured["config"] = config
        return _FakeResp()

    monkeypatch.setattr(service.client.aio.models, "generate_content", fake_generate_content)

    await service.generate_response(
        [{"role": "user", "content": "q"}], system_prompt="s", timeout=90
    )
    assert captured["config"].http_options.timeout == 90_000
    # The explicit header is REQUIRED (google-genai 2.12.1): the SDK bakes
    # 'X-Server-Timeout: 30' into the client's shared headers on every
    # no-override call, and per-request headers are the only thing that
    # outranks it in the merge. Without this, the server still 504s at 30s.
    assert captured["config"].http_options.headers["X-Server-Timeout"] == "90"

    await service.generate_response([{"role": "user", "content": "q"}], system_prompt="s")
    assert getattr(captured["config"], "http_options", None) is None


def test_gemini_server_timeout_header_survives_client_pollution():
    """End-to-end through the REAL SDK request builder: after a no-override
    call pollutes the client's shared headers with X-Server-Timeout: 30,
    the archive read's per-request options must still put 90 on the wire."""
    from google.genai import types as genai_types

    from app.config import settings

    service = LLMService.__new__(LLMService)  # skip __init__; build client only
    import google.genai as genai

    client = genai.Client(
        api_key="test-key",
        http_options=genai_types.HttpOptions(timeout=LLM_CALL_TIMEOUT_SECONDS * 1000),
    )
    api = client._api_client
    # pollute, exactly as any classifier call does
    api._build_request("post", "models/m:generateContent", {"contents": []}, None)
    assert (api._http_options.headers or {}).get("X-Server-Timeout") == "30"
    # bare timeout is NOT enough - the polluted header wins the merge
    bare = api._build_request(
        "post", "models/m:generateContent", {"contents": []},
        genai_types.HttpOptions(timeout=90_000),
    )
    assert bare.headers.get("X-Server-Timeout") == "30"
    # the shipped shape: explicit header outranks the pollution
    fixed = api._build_request(
        "post", "models/m:generateContent", {"contents": []},
        genai_types.HttpOptions(timeout=90_000, headers={"X-Server-Timeout": "90"}),
    )
    assert fixed.headers.get("X-Server-Timeout") == "90"
    assert fixed.timeout == 90.0
