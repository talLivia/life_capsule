"""
Pytest fixtures for the API test suite.

Each test gets a completely fresh in-memory SQLite database (function-scoped
engine + StaticPool so the single in-memory connection is shared within the
test but discarded after it). This guarantees isolation — a user created in
one test can't collide with the same fixture in the next.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1.users import create_access_token, get_password_hash
from app.database import Base, get_db
from app.models import User  # noqa: F401 — ensures models are registered on Base
from main import app

# Pure in-memory DB — no file on disk, fully isolated per engine instance.
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def test_engine():
    """Fresh in-memory engine + schema for every test (full isolation)."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine):
    """A session bound to the per-test engine."""
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest.fixture
async def client(db_session):
    """Async test client with the DB dependency overridden to the test session."""

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def test_user(db_session):
    """Create a test user in the per-test database."""
    user = User(
        email="test@example.com",
        username="testuser",
        full_name="Test User",
        hashed_password=get_password_hash("testpassword123"),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def auth_headers(test_user):
    """Authorization headers for the test user."""
    token = create_access_token(data={"sub": test_user.id})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _no_real_gemini_caches(monkeypatch):
    """The explicit-cache flag defaults ON in production; tests must never
    create real cachedContents (billing + network). Tests that exercise the
    cache path re-enable it explicitly against a mocked client."""
    from app.config import settings

    monkeypatch.setattr(settings, "GEMINI_CONTEXT_CACHE", "off")


@pytest.fixture(autouse=True)
def _no_real_redis(monkeypatch):
    """Tests must never touch a real Redis (2026-08-31: the moment a local
    Redis actually existed, the clip-URL cache leaked ACROSS suite runs —
    test_video_clip_e2e served the PREVIOUS run's tmp_path clip via the 24h
    cached URL and failed on exists()). Blank REDIS_URL takes cache_service's
    deliberate-off path at app startup; resetting the global instance covers
    tests that use it without restarting the app."""
    from app.config import settings
    from app.services.cache import cache_service

    monkeypatch.setattr(settings, "REDIS_URL", "")
    monkeypatch.setattr(cache_service, "redis", None)
    monkeypatch.setattr(cache_service, "disabled", True)
