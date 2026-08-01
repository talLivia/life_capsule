import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_root_endpoint(client: AsyncClient):
    """Test the root endpoint returns API info."""
    response = await client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "AI Avatar System API"
    assert data["version"] == "2.0.0"
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_health_endpoint(client: AsyncClient):
    """Test the health check endpoint."""
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "services" in data
    assert "timestamp" in data


@pytest.mark.asyncio
async def test_docs_available_in_debug(client: AsyncClient):
    """Test that OpenAPI docs are available in debug mode."""
    response = await client.get("/docs")
    # In debug mode, docs should be available (redirect or 200)
    assert response.status_code in [200, 307]


# ── Redis health reporting ────────────────────────────────────────────────
# A Redis that never connected used to report "not configured" with status
# "healthy" — identical to a cache deliberately turned off. In production that
# meant the entire clip cache could silently no-op (every answer paying full
# ffmpeg assembly) with nothing to show for it but one log line at boot. These
# pin the four states apart.


@pytest.mark.asyncio
async def test_health_reports_failed_redis_connect_as_degraded(client: AsyncClient, monkeypatch):
    """The case that was invisible: REDIS_URL is set, the connect FAILED."""
    from app.services.cache import cache_service

    monkeypatch.setattr(cache_service, "redis", None)
    monkeypatch.setattr(cache_service, "disabled", False)
    monkeypatch.setattr(cache_service, "init_error", "ConnectionError: nope")

    data = (await client.get("/health")).json()
    assert data["status"] == "degraded"
    assert data["services"]["redis"].startswith("unreachable")
    # the underlying cause must survive into the probe, not just "unreachable" —
    # "it is broken" without "why" is most of the way back to the silence this
    # whole change exists to remove
    assert "ConnectionError" in data["services"]["redis"]
    assert "nope" in data["services"]["redis"]


@pytest.mark.asyncio
async def test_health_reports_blank_redis_url_as_not_configured(client: AsyncClient, monkeypatch):
    """Deliberately running without a cache must not cry wolf — the ONE benign
    way to have no Redis.

    The assertion is on the SERVICE STRING, not on the aggregate `status`:
    "not configured" and setting degraded are mutually exclusive branches of
    the probe, so pinning the string pins the non-degradation. `status` itself
    aggregates every service and is not stable across the suite."""
    from app.services.cache import cache_service

    monkeypatch.setattr(cache_service, "redis", None)
    monkeypatch.setattr(cache_service, "disabled", True)
    monkeypatch.setattr(cache_service, "init_error", None)

    data = (await client.get("/health")).json()
    assert data["services"]["redis"] == "not configured"


@pytest.mark.asyncio
async def test_health_reports_live_redis_as_connected(client: AsyncClient, monkeypatch):
    from app.services.cache import cache_service

    class _Live:
        async def ping(self):
            return True

    monkeypatch.setattr(cache_service, "redis", _Live())
    data = (await client.get("/health")).json()
    # Only the redis field is asserted: `status` aggregates every service, and
    # the other probes' results vary with the test environment.
    assert data["services"]["redis"] == "connected"


@pytest.mark.asyncio
async def test_health_reports_redis_that_died_after_startup_as_degraded(
    client: AsyncClient, monkeypatch
):
    """Connected at boot, unreachable now — distinct from never connecting."""
    from app.services.cache import cache_service

    class _Dead:
        async def ping(self):
            raise ConnectionError("gone")

    monkeypatch.setattr(cache_service, "redis", _Dead())
    data = (await client.get("/health")).json()
    assert data["services"]["redis"] == "disconnected"
    assert data["status"] == "degraded"


@pytest.mark.asyncio
async def test_blank_redis_url_disables_cache_without_recording_an_error(monkeypatch):
    """initialize() must set `disabled` (not `init_error`) for a blank URL —
    that distinction is the whole basis of the health probe above."""
    from app.config import settings
    from app.services.cache import CacheService

    monkeypatch.setattr(settings, "REDIS_URL", "   ")
    svc = CacheService()
    await svc.initialize()
    assert svc.disabled is True
    assert svc.init_error is None
    assert svc.redis is None
    # and it still behaves as a no-op cache rather than raising
    assert await svc.get("k") is None
    assert await svc.set("k", "v") is False


@pytest.mark.asyncio
async def test_failed_connect_records_init_error(monkeypatch):
    """A bad REDIS_URL must leave a diagnosable reason behind, not just None.

    from_url is stubbed to raise rather than dialling a dead port: a real
    client built against an unreachable host is never closed on this path, and
    its pool then tries to clean up on an already-closed event loop, failing an
    unrelated later test."""
    from app.config import settings
    from app.services import cache as cache_mod

    def _boom(*a, **k):
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(settings, "REDIS_URL", "redis://unreachable:6379/0")
    monkeypatch.setattr(cache_mod.aioredis, "from_url", _boom)

    svc = cache_mod.CacheService()
    await svc.initialize()
    assert svc.redis is None
    assert svc.disabled is False
    assert "ConnectionError" in svc.init_error
    assert "Connection refused" in svc.init_error
