"""
End-to-end journey through the real ASGI app: a brand-new user registers,
logs in (receiving an httpOnly cookie), uploads an avatar, starts a session,
exchanges a message, and exports the transcript — authenticating the whole
way via the cookie alone (no bearer header). Only the heavy ML/storage edges
(face processing, S3 upload) are stubbed; every route, auth check, ownership
guard, and DB write is real.
"""

from pathlib import Path

import pytest
from httpx import AsyncClient


@pytest.fixture
def stub_media(monkeypatch):
    """Stub avatar image processing + object storage so no GPU/S3 is needed."""
    from app.api.v1 import avatars

    async def fake_process(orig_path, out_path):
        Path(out_path).write_bytes(b"processed-image-bytes")
        return out_path, {"width": 512, "height": 512, "face_detected": True}

    async def fake_upload(data, key, content_type="application/octet-stream", metadata=None):
        return f"http://test-storage/{key}"

    monkeypatch.setattr(avatars.avatar_processor, "process_image", fake_process)
    monkeypatch.setattr(avatars.storage_service, "upload_file", fake_upload)


@pytest.mark.asyncio
async def test_full_user_journey_cookie_auth(client: AsyncClient, stub_media):
    # 1) Register a brand-new user.
    reg = await client.post(
        "/api/v1/users/register",
        json={
            "email": "journey@example.com",
            "username": "journey",
            "full_name": "Journey User",
            "password": "journeypass123",
        },
    )
    assert reg.status_code == 201, reg.text

    # 2) Log in — sets the httpOnly cookie in the client's jar.
    login = await client.post(
        "/api/v1/users/login",
        data={"username": "journey@example.com", "password": "journeypass123"},
    )
    assert login.status_code == 200
    assert "access_token=" in login.headers.get("set-cookie", "")

    # From here on we send NO Authorization header — the cookie authenticates.
    # 3) Upload an avatar.
    up = await client.post(
        "/api/v1/avatars/upload",
        files={"file": ("face.jpg", b"\xff\xd8\xff fake jpeg", "image/jpeg")},
        data={"name": "My Avatar"},
    )
    assert up.status_code == 201, up.text
    avatar = up.json()
    assert avatar["status"] == "ready"
    avatar_id = avatar["id"]

    # 4) It shows up in the user's avatar list.
    avatars_list = await client.get("/api/v1/avatars/")
    assert avatars_list.status_code == 200
    assert any(a["id"] == avatar_id for a in avatars_list.json())

    # 5) Start a session against that avatar.
    sess = await client.post("/api/v1/sessions/create", json={"avatar_id": avatar_id})
    assert sess.status_code == 201, sess.text
    session_id = sess.json()["id"]

    # 6) Session appears in history.
    sessions_list = await client.get("/api/v1/sessions/")
    assert sessions_list.status_code == 200
    assert any(s["id"] == session_id for s in sessions_list.json())

    # 7) Send a message via the REST fallback path.
    msg = await client.post(
        "/api/v1/messages/send",
        json={"session_id": session_id, "content": "Hello avatar"},
    )
    assert msg.status_code == 201, msg.text

    # 8) It's retrievable.
    msgs = await client.get(f"/api/v1/messages/session/{session_id}")
    assert msgs.status_code == 200
    assert len(msgs.json()) == 1
    assert msgs.json()[0]["content"] == "Hello avatar"

    # 9) Export the session transcript.
    export = await client.get(f"/api/v1/sessions/{session_id}/export")
    assert export.status_code == 200
    body = export.json()
    assert body["session"]["id"] == session_id
    assert len(body["messages"]) == 1
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_cross_user_avatar_access_blocked(client: AsyncClient, stub_media, db_session):
    """A second user cannot see or use the first user's avatar (ownership)."""
    from app.api.v1.users import get_password_hash
    from app.models import Avatar, User

    # Seed user A with an avatar directly.
    user_a = User(
        email="a@example.com",
        username="usera",
        hashed_password=get_password_hash("passworda1"),
    )
    db_session.add(user_a)
    await db_session.commit()
    await db_session.refresh(user_a)
    avatar = Avatar(
        user_id=user_a.id,
        name="A's avatar",
        image_url="http://x/i.jpg",
        s3_key="avatars/a/image.jpg",
        status="ready",
    )
    db_session.add(avatar)
    await db_session.commit()
    await db_session.refresh(avatar)

    # Register + log in as user B.
    await client.post(
        "/api/v1/users/register",
        json={"email": "b@example.com", "username": "userb", "password": "passwordb1"},
    )
    await client.post(
        "/api/v1/users/login",
        data={"username": "b@example.com", "password": "passwordb1"},
    )

    # B cannot GET A's avatar...
    got = await client.get(f"/api/v1/avatars/{avatar.id}")
    assert got.status_code == 403

    # ...and cannot start a session against it.
    sess = await client.post("/api/v1/sessions/create", json={"avatar_id": avatar.id})
    assert sess.status_code == 403
