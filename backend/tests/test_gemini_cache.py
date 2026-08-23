"""Phase B skeleton (gemini_cache): off-by-default inertness, version-keyed
identity, fail-soft lifecycle. No test here talks to a real API — the client
is always mocked, and the OFF tests assert the API is never even touched."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import gemini_cache as gc


@pytest.fixture(autouse=True)
def _clean_registry():
    gc._reset_registry_for_tests()
    yield
    gc._reset_registry_for_tests()


def _explosive_client():
    """A client whose every cache method fails the test if called."""

    async def _boom(*a, **kw):  # pragma: no cover - reaching it IS the failure
        raise AssertionError("cache API touched while GEMINI_CONTEXT_CACHE=off")

    return SimpleNamespace(
        aio=SimpleNamespace(
            caches=SimpleNamespace(create=_boom, delete=_boom, update=_boom)
        )
    )


# ── off in tests: inert, and provably no API surface touched ────────────────
# (Production default flipped to "on" at Phase B activation, 2026-08-23; the
# conftest autouse fixture forces it off for every test so no test can create
# real billable caches. This test pins that isolation.)


def test_flag_is_forced_off_under_tests():
    assert settings.GEMINI_CONTEXT_CACHE == "off"


@pytest.mark.asyncio
async def test_everything_is_a_noop_when_off(monkeypatch):
    from app.services.llm import llm_service

    monkeypatch.setattr(llm_service, "client", _explosive_client())
    assert gc.registry_lookup("p1", (1, "a", "b")) is None
    assert await gc.ensure_cache("p1", (1, "a", "b"), "SYSTEM") is None
    await gc.drop_cache("p1")  # no raise, no API call
    call = AsyncMock(return_value="text")
    text, used = await gc.read_with_cache("p1", (1, "a", "b"), "SYSTEM", call)
    assert (text, used) == ("text", False)
    call.assert_awaited_once_with(cached_content=None)


# ── identity: the version fingerprint is part of the cache key ──────────────


def test_version_hash_is_stable_and_version_sensitive():
    v1 = (18, "2026-08-20", "2026-08-19")
    assert gc.version_hash(v1) == gc.version_hash(tuple(v1))
    assert gc.version_hash(v1) != gc.version_hash((19, "2026-08-23", "2026-08-19"))
    assert gc.cache_display_name("p1", v1) == f"archive:p1:{gc.version_hash(v1)}"


def test_lookup_rejects_version_mismatch_and_expiry(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_CONTEXT_CACHE", "on")
    v = (18, "a", "b")
    gc._REGISTRY["p1"] = gc._Handle(
        name="cachedContents/x",
        version_key=gc.version_hash(v),
        expires_at=time.time() + 600,
    )
    assert gc.registry_lookup("p1", v) == "cachedContents/x"
    # archive changed -> stale entry is ignored (correct by construction)
    assert gc.registry_lookup("p1", (19, "a", "b")) is None
    # expired -> dropped
    gc._REGISTRY["p1"].expires_at = time.time() - 1
    assert gc.registry_lookup("p1", v) is None
    assert "p1" not in gc._REGISTRY


# ── lifecycle: create, fail-soft read, best-effort delete ───────────────────


@pytest.mark.asyncio
async def test_ensure_cache_creates_and_registers(monkeypatch):
    from app.services.llm import llm_service

    monkeypatch.setattr(settings, "GEMINI_CONTEXT_CACHE", "on")
    created = SimpleNamespace(name="cachedContents/abc")
    client = SimpleNamespace(
        aio=SimpleNamespace(caches=SimpleNamespace(create=AsyncMock(return_value=created)))
    )
    monkeypatch.setattr(llm_service, "client", client)
    v = (18, "a", "b")
    assert await gc.ensure_cache("p1", v, "SYSTEM") == "cachedContents/abc"
    assert gc.registry_lookup("p1", v) == "cachedContents/abc"
    # second call is a registry hit, no second create
    assert await gc.ensure_cache("p1", v, "SYSTEM") == "cachedContents/abc"
    assert client.aio.caches.create.await_count == 1


@pytest.mark.asyncio
async def test_ensure_cache_failure_is_soft(monkeypatch):
    from app.services.llm import llm_service

    monkeypatch.setattr(settings, "GEMINI_CONTEXT_CACHE", "on")
    client = SimpleNamespace(
        aio=SimpleNamespace(
            caches=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("too small")))
        )
    )
    monkeypatch.setattr(llm_service, "client", client)
    assert await gc.ensure_cache("p1", (1, "a", "b"), "SYSTEM") is None  # no raise


@pytest.mark.asyncio
async def test_cached_read_failure_retries_uncached_and_drops_entry(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_CONTEXT_CACHE", "on")
    v = (18, "a", "b")
    gc._REGISTRY["p1"] = gc._Handle(
        name="cachedContents/x",
        version_key=gc.version_hash(v),
        expires_at=time.time() + gc.CACHE_TTL_SECONDS,
    )

    calls = []

    async def call(cached_content=None):
        calls.append(cached_content)
        if cached_content is not None:
            raise RuntimeError("CachedContent not found")
        return "answer"

    text, used = await gc.read_with_cache("p1", v, "SYSTEM", call)
    assert (text, used) == ("answer", False)
    assert calls == ["cachedContents/x", None]  # cached first, uncached fallback
    assert "p1" not in gc._REGISTRY  # stale handle dropped


@pytest.mark.asyncio
async def test_drop_cache_swallows_delete_errors(monkeypatch):
    from app.services.llm import llm_service

    monkeypatch.setattr(settings, "GEMINI_CONTEXT_CACHE", "on")
    v = (18, "a", "b")
    gc._REGISTRY["p1"] = gc._Handle(
        name="cachedContents/x",
        version_key=gc.version_hash(v),
        expires_at=time.time() + 600,
    )
    client = SimpleNamespace(
        aio=SimpleNamespace(
            caches=SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("gone")))
        )
    )
    monkeypatch.setattr(llm_service, "client", client)
    await gc.drop_cache("p1")  # no raise
    assert "p1" not in gc._REGISTRY


# ── the wired read path (_read_archive_for_ranges) ──────────────────────────


@pytest.mark.asyncio
async def test_archive_read_references_cache_when_active(monkeypatch):
    from app.services import full_archive_retrieval as ar
    from app.services.llm import llm_service

    monkeypatch.setattr(settings, "GEMINI_CONTEXT_CACHE", "on")
    v = (18, "a", "b")
    gc._REGISTRY["p1"] = gc._Handle(
        name="cachedContents/x",
        version_key=gc.version_hash(v),
        expires_at=time.time() + gc.CACHE_TTL_SECONDS,
    )
    # TTL renewal path is exercised by touch_cache after a hit; give the
    # mocked client an update method that succeeds silently.
    client = SimpleNamespace(
        aio=SimpleNamespace(caches=SimpleNamespace(update=AsyncMock()))
    )
    monkeypatch.setattr(llm_service, "client", client)
    seen = {}

    async def fake_generate(**kwargs):
        seen["cached_content"] = kwargs.get("cached_content")
        return '{"unit_ids": []}'

    monkeypatch.setattr(llm_service, "generate_response", fake_generate)
    read = await ar._read_archive_for_ranges(
        "q", "T", "E", "", "he", group_id="p1", archive_version=v
    )
    assert not read.failed
    assert seen["cached_content"] == "cachedContents/x"


@pytest.mark.asyncio
async def test_archive_read_uncached_when_flag_off(monkeypatch):
    from app.services import full_archive_retrieval as ar
    from app.services.llm import llm_service

    # autouse fixture already forces the flag off
    seen = {}

    async def fake_generate(**kwargs):
        seen["cached_content"] = kwargs.get("cached_content")
        return '{"unit_ids": []}'

    monkeypatch.setattr(llm_service, "generate_response", fake_generate)
    read = await ar._read_archive_for_ranges(
        "q", "T", "E", "", "he", group_id="p1", archive_version=(1, "a", "b")
    )
    assert not read.failed
    assert seen["cached_content"] is None
