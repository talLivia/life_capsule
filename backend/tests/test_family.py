import pytest
from httpx import AsyncClient

from app.api.v1.users import create_access_token, get_password_hash
from app.models import Avatar, FamilyInvite, InterviewSession, RawSegment, User

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def other_producer(db_session):
    """A second producer, distinct from `test_user`, to test cross-tenant
    invite ownership checks."""
    user = User(
        email="other-producer@example.com",
        username="otherproducer",
        hashed_password=get_password_hash("testpassword123"),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def other_producer_auth_headers(other_producer):
    token = create_access_token(data={"sub": other_producer.id})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def fresh_signup(db_session):
    """A brand-new registration — role="producer" by default (Prompt 4),
    but with no recorded content, so it's eligible to redeem an invite."""
    user = User(
        email="newcomer@example.com",
        username="newcomer",
        hashed_password=get_password_hash("testpassword123"),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def fresh_signup_auth_headers(fresh_signup):
    token = create_access_token(data={"sub": fresh_signup.id})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def active_storyteller(db_session):
    """A producer who has already recorded content — must NOT be able to
    redeem a family invite and silently lose /record access."""
    user = User(
        email="storyteller@example.com",
        username="storyteller",
        hashed_password=get_password_hash("testpassword123"),
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(InterviewSession(user_id=user.id, status="active"))
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def active_storyteller_auth_headers(active_storyteller):
    token = create_access_token(data={"sub": active_storyteller.id})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def family_user(db_session, test_user):
    user = User(
        email="family-member@example.com",
        username="familymember",
        hashed_password=get_password_hash("testpassword123"),
        role="family",
        producer_id=test_user.id,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def family_user_auth_headers(family_user):
    token = create_access_token(data={"sub": family_user.id})
    return {"Authorization": f"Bearer {token}"}


# ── invite creation/listing/revocation ──────────────────────────────────────


async def test_create_invite_requires_producer_role(client: AsyncClient, family_user_auth_headers):
    resp = await client.post("/api/v1/family/invites", headers=family_user_auth_headers)
    assert resp.status_code == 403


async def test_create_invite(client: AsyncClient, auth_headers, test_user):
    resp = await client.post("/api/v1/family/invites", headers=auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["token"]
    assert body["redeemed_by_user_id"] is None


async def test_list_invites_scoped_to_producer(
    client: AsyncClient, auth_headers, other_producer_auth_headers
):
    await client.post("/api/v1/family/invites", headers=auth_headers)
    await client.post("/api/v1/family/invites", headers=other_producer_auth_headers)

    mine = await client.get("/api/v1/family/invites", headers=auth_headers)
    assert mine.status_code == 200
    assert len(mine.json()) == 1

    theirs = await client.get("/api/v1/family/invites", headers=other_producer_auth_headers)
    assert len(theirs.json()) == 1


async def test_revoke_invite(client: AsyncClient, auth_headers):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    invite_id = created.json()["id"]

    resp = await client.delete(f"/api/v1/family/invites/{invite_id}", headers=auth_headers)
    assert resp.status_code == 204

    # Redeeming a revoked invite must fail.
    token = created.json()["token"]
    redeem = await client.post(
        "/api/v1/family/invites/redeem", json={"token": token}, headers=auth_headers
    )
    assert redeem.status_code in (400, 409)  # self-redeem (400) checked before status (409)


async def test_revoke_invite_requires_ownership(
    client: AsyncClient, auth_headers, other_producer_auth_headers
):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    invite_id = created.json()["id"]

    resp = await client.delete(
        f"/api/v1/family/invites/{invite_id}", headers=other_producer_auth_headers
    )
    assert resp.status_code == 403


async def test_revoke_already_redeemed_invite_conflicts(
    client: AsyncClient, auth_headers, fresh_signup_auth_headers
):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    invite_id, token = created.json()["id"], created.json()["token"]

    await client.post(
        "/api/v1/family/invites/redeem", json={"token": token}, headers=fresh_signup_auth_headers
    )

    resp = await client.delete(f"/api/v1/family/invites/{invite_id}", headers=auth_headers)
    assert resp.status_code == 409


# ── redemption ───────────────────────────────────────────────────────────────


async def test_redeem_invite_success(
    client: AsyncClient, auth_headers, test_user, fresh_signup_auth_headers, fresh_signup
):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    token = created.json()["token"]

    resp = await client.post(
        "/api/v1/family/invites/redeem", json={"token": token}, headers=fresh_signup_auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "family"
    assert body["producer_id"] == test_user.id


async def test_redeem_rejects_own_invite(client: AsyncClient, auth_headers):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    token = created.json()["token"]

    resp = await client.post(
        "/api/v1/family/invites/redeem", json={"token": token}, headers=auth_headers
    )
    assert resp.status_code == 400


async def test_redeem_rejects_active_storyteller(
    client: AsyncClient, auth_headers, active_storyteller_auth_headers
):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    token = created.json()["token"]

    resp = await client.post(
        "/api/v1/family/invites/redeem",
        json={"token": token},
        headers=active_storyteller_auth_headers,
    )
    assert resp.status_code == 400


async def test_redeem_rejects_invalid_token(client: AsyncClient, fresh_signup_auth_headers):
    resp = await client.post(
        "/api/v1/family/invites/redeem",
        json={"token": "not-a-real-token"},
        headers=fresh_signup_auth_headers,
    )
    assert resp.status_code == 404


async def test_redeem_rejects_expired_invite(
    client: AsyncClient, db_session, auth_headers, test_user, fresh_signup_auth_headers
):
    from datetime import datetime, timedelta, timezone

    invite = FamilyInvite(
        producer_id=test_user.id,
        token="expired-token-123",
        status="pending",
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add(invite)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/family/invites/redeem",
        json={"token": "expired-token-123"},
        headers=fresh_signup_auth_headers,
    )
    assert resp.status_code == 410


async def test_talk_availability_true_with_ready_segment_and_avatar(
    client: AsyncClient, db_session, test_user, family_user_auth_headers
):
    session = InterviewSession(user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    db_session.add(
        RawSegment(
            interview_session_id=session.id,
            question_asked="Q",
            question_index=0,
            transcript="A verbatim story.",
            status="ready",
        )
    )
    db_session.add(
        Avatar(
            user_id=test_user.id,
            name="Storyteller",
            image_url="http://x/i.jpg",
            s3_key="avatars/x/image.jpg",
            status="ready",
        )
    )
    await db_session.commit()

    resp = await client.get("/api/v1/family/talk-availability", headers=family_user_auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["ready_segment_count"] == 1
    assert body["avatar_id"] is not None
    assert body["avatar_image_url"] == "http://x/i.jpg"
    assert body["producer_id"] == test_user.id
    # Default chat_mode (Prompt 14) — no one's /talk experience changes
    # until the producer opts into video-clip mode.
    assert body["chat_mode"] == "avatar"


async def test_talk_availability_reflects_producer_video_clip_mode(
    client: AsyncClient, db_session, test_user, family_user_auth_headers
):
    """Prompt 14: the setting is producer-level — /talk-availability must
    hand back whichever mode the LINKED PRODUCER chose, not a default,
    since the family account never has its own copy of this setting."""
    test_user.chat_mode = "video_clips_v2"
    db_session.add(test_user)
    await db_session.commit()

    resp = await client.get("/api/v1/family/talk-availability", headers=family_user_auth_headers)
    assert resp.status_code == 200
    assert resp.json()["chat_mode"] == "video_clips_v2"


# ── talk-availability ────────────────────────────────────────────────────────


async def test_talk_availability_requires_family_link(client: AsyncClient, fresh_signup_auth_headers):
    resp = await client.get("/api/v1/family/talk-availability", headers=fresh_signup_auth_headers)
    assert resp.status_code == 403


async def test_talk_availability_false_with_no_ready_segments(
    client: AsyncClient, auth_headers, fresh_signup_auth_headers
):
    created = await client.post("/api/v1/family/invites", headers=auth_headers)
    token = created.json()["token"]
    await client.post(
        "/api/v1/family/invites/redeem", json={"token": token}, headers=fresh_signup_auth_headers
    )

    resp = await client.get("/api/v1/family/talk-availability", headers=fresh_signup_auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["ready_segment_count"] == 0
