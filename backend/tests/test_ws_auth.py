"""
Integration tests for the WebSocket handshake auth (`main._verify_ws_session`).

This is the security-critical gate that the ConnectionManager unit tests
don't cover: it talks to the real database (via AsyncSessionLocal) to confirm
the JWT's subject owns the session before any chat traffic is accepted. We
point AsyncSessionLocal at the per-test in-memory engine so the real code
path runs end-to-end without a live Postgres.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import main
from app.api.v1.users import create_access_token
from app.models import Avatar, Session


async def _seed_session(sessionmaker, user_id: str) -> str:
    async with sessionmaker() as db:
        avatar = Avatar(
            user_id=user_id,
            name="A",
            image_url="http://x/i.jpg",
            s3_key="avatars/x/image.jpg",
            status="ready",
        )
        db.add(avatar)
        await db.commit()
        await db.refresh(avatar)

        session = Session(user_id=user_id, avatar_id=avatar.id, status="active")
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session.id


@pytest.fixture
def patched_session_local(test_engine, monkeypatch):
    """Point main.AsyncSessionLocal at the test engine for the handshake path."""
    sm = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(main, "AsyncSessionLocal", sm)
    return sm


@pytest.mark.asyncio
async def test_valid_token_for_owned_session(patched_session_local):
    sid = await _seed_session(patched_session_local, "user-A")
    token = create_access_token(data={"sub": "user-A"})
    assert await main._verify_ws_session(sid, token) == "user-A"


@pytest.mark.asyncio
async def test_token_for_someone_elses_session_rejected(patched_session_local):
    sid = await _seed_session(patched_session_local, "user-A")
    # A valid token, but for a different user → must be rejected (no IDOR).
    token = create_access_token(data={"sub": "user-B"})
    assert await main._verify_ws_session(sid, token) is None


@pytest.mark.asyncio
async def test_unknown_session_rejected(patched_session_local):
    token = create_access_token(data={"sub": "user-A"})
    assert await main._verify_ws_session("does-not-exist", token) is None


@pytest.mark.asyncio
async def test_garbage_token_rejected(patched_session_local):
    sid = await _seed_session(patched_session_local, "user-A")
    assert await main._verify_ws_session(sid, "not-a-jwt") is None


@pytest.mark.asyncio
async def test_no_token_rejected_when_not_debug(patched_session_local, monkeypatch):
    sid = await _seed_session(patched_session_local, "user-A")
    monkeypatch.setattr(main.settings, "DEBUG", False)
    assert await main._verify_ws_session(sid, None) is None


@pytest.mark.asyncio
async def test_no_token_demo_session_allowed_in_debug(patched_session_local, monkeypatch):
    # The seeded demo fallback only applies to the demo-user session in DEBUG.
    sid = await _seed_session(patched_session_local, "demo-user")
    monkeypatch.setattr(main.settings, "DEBUG", True)
    assert await main._verify_ws_session(sid, None) == "demo-user"


@pytest.mark.asyncio
async def test_no_token_non_demo_session_rejected_in_debug(patched_session_local, monkeypatch):
    sid = await _seed_session(patched_session_local, "real-user")
    monkeypatch.setattr(main.settings, "DEBUG", True)
    # Even in DEBUG, the tokenless fallback is demo-user only.
    assert await main._verify_ws_session(sid, None) is None
