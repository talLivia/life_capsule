"""
Regression tests for bugs found in the 2026-06 end-to-end audit.

Each test pins a specific production-facing failure:

  * Deleting a session that has messages AND an auto-titled conversation
    used to raise an FK violation (conversations.session_id had no cascade)
    and surface as HTTP 500 from the history panel's delete button.
  * Deleting an avatar that had ever been chatted with hit the same class
    of bug via sessions.avatar_id.
  * The rate limiter raised fastapi.HTTPException from inside
    BaseHTTPMiddleware, which FastAPI's exception handlers never see —
    clients got 500 "Internal server error" instead of 429.
  * Settings crashed at import when CORS_ORIGINS / ALLOWED_EXTENSIONS were
    given in the comma-separated form used by .env.example.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import Settings
from app.models import Avatar, Conversation, Message, Session

pytestmark = pytest.mark.asyncio


async def _seed_full_session(db_session, user_id: str):
    """Avatar + session + messages + auto-titled conversation (a real chat)."""
    avatar = Avatar(
        user_id=user_id,
        name="Cascade Avatar",
        image_url="http://x/i.jpg",
        thumbnail_url="http://x/t.jpg",
        s3_key="avatars/x/image.jpg",
        status="ready",
    )
    db_session.add(avatar)
    await db_session.commit()
    await db_session.refresh(avatar)

    session = Session(user_id=user_id, avatar_id=avatar.id, status="active")
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)

    db_session.add(Message(session_id=session.id, role="user", content="hi"))
    db_session.add(Message(session_id=session.id, role="assistant", content="hello"))
    db_session.add(Conversation(session_id=session.id, title="Hi", message_count=2))
    await db_session.commit()
    return avatar, session


async def test_delete_session_with_conversation_and_messages(
    client: AsyncClient, db_session, test_user, auth_headers
):
    """DELETE /sessions/{id} must cascade to messages AND conversations."""
    _, session = await _seed_full_session(db_session, test_user.id)

    resp = await client.delete(f"/api/v1/sessions/{session.id}", headers=auth_headers)
    assert resp.status_code == 204

    # Children are gone too — no orphans, no FK error
    msgs = (
        (await db_session.execute(select(Message).where(Message.session_id == session.id)))
        .scalars()
        .all()
    )
    convos = (
        (
            await db_session.execute(
                select(Conversation).where(Conversation.session_id == session.id)
            )
        )
        .scalars()
        .all()
    )
    assert msgs == []
    assert convos == []


async def test_delete_avatar_with_chat_history(
    client: AsyncClient, db_session, test_user, auth_headers
):
    """DELETE /avatars/{id} must cascade through sessions → messages/conversations."""
    avatar, session = await _seed_full_session(db_session, test_user.id)

    resp = await client.delete(f"/api/v1/avatars/{avatar.id}", headers=auth_headers)
    assert resp.status_code == 204

    remaining = (
        (await db_session.execute(select(Session).where(Session.avatar_id == avatar.id)))
        .scalars()
        .all()
    )
    assert remaining == []


async def test_rate_limit_returns_429_not_500():
    """Limit violations must surface as 429 with Retry-After, not 500."""
    from fastapi import FastAPI

    from app.middleware.rate_limiter import RateLimitMiddleware

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, rate_per_minute=2, rate_per_hour=100)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/ping")).status_code == 200
        assert (await c.get("/ping")).status_code == 200
        third = await c.get("/ping")
        assert third.status_code == 429
        assert "Retry-After" in third.headers
        assert third.json()["detail"] == "Rate limit exceeded. Try again later."


def test_settings_accepts_comma_separated_lists():
    """Both .env styles must parse: JSON arrays and bare comma-separated."""
    assert Settings._split_comma_separated("http://a.com, http://b.com") == [
        "http://a.com",
        "http://b.com",
    ]
    assert Settings._split_comma_separated(["already", "a-list"]) == ["already", "a-list"]
    # Trailing comma / stray whitespace shouldn't produce empty entries
    assert Settings._split_comma_separated("jpg, png,") == ["jpg", "png"]


async def test_avatar_metadata_patch_actually_persists(
    client: AsyncClient, db_session, test_user, auth_headers
):
    """PATCH /avatars/{id}/metadata must survive a round-trip to the DB.

    The endpoint used to mutate the ORM-held JSON dict in place and reassign
    the same object — SQLAlchemy detected no change and emitted no UPDATE, so
    the personality editor showed "Saved" while persisting nothing.
    """
    avatar = Avatar(
        user_id=test_user.id,
        name="Meta Avatar",
        image_url="http://x/i.jpg",
        s3_key="avatars/x/image.jpg",
        status="ready",
        avatar_metadata={"face_detected": True},
    )
    db_session.add(avatar)
    await db_session.commit()
    await db_session.refresh(avatar)

    resp = await client.patch(
        f"/api/v1/avatars/{avatar.id}/metadata",
        json={"system_prompt": "You are a pirate."},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    # Drop all in-memory ORM state and reload from the database — the bug is
    # only visible across a real round-trip (the identity map hid it).
    avatar_id = avatar.id  # capture before expire_all (sync lazy-load would fail)
    db_session.expire_all()
    result = await db_session.execute(select(Avatar).where(Avatar.id == avatar_id))
    fresh = result.scalar_one()
    assert (fresh.avatar_metadata or {}).get("system_prompt") == "You are a pirate."
    # And the pre-existing keys were merged, not clobbered
    assert (fresh.avatar_metadata or {}).get("face_detected") is True


def test_cleanup_task_sweeps_session_dirs(tmp_path, monkeypatch):
    """cleanup_old_files must reap the avatar-session-* dirs the WS pipeline
    actually writes to — not just the legacy /tmp/{avatars,videos,audio}."""
    import time as _time

    import app.celery_app as celery_mod

    # Redirect the system temp dir to an isolated tmp_path for this test.
    monkeypatch.setattr(celery_mod.tempfile, "gettempdir", lambda: str(tmp_path))

    old = tmp_path / "avatar-session-abc123"
    old.mkdir()
    stale_file = old / "0_video.mp4"
    stale_file.write_bytes(b"x")
    # Backdate the file and dir well past the 24h threshold.
    past = _time.time() - 48 * 3600
    import os as _os

    _os.utime(stale_file, (past, past))
    _os.utime(old, (past, past))

    # A fresh session dir must be left alone.
    fresh = tmp_path / "avatar-session-fresh"
    fresh.mkdir()
    (fresh / "0_video.mp4").write_bytes(b"y")

    result = celery_mod.cleanup_old_files_task()

    assert result["cleaned_files"] >= 1
    assert not stale_file.exists()
    assert not old.exists()  # empty + stale dir pruned
    assert fresh.exists()  # recent dir untouched
