import pytest
from httpx import AsyncClient

from app.api.v1.users import create_access_token, get_password_hash
from app.models import Avatar, User


@pytest.mark.asyncio
async def test_list_sessions(client: AsyncClient):
    """Test listing sessions endpoint."""
    response = await client.get("/api/v1/sessions/")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_get_session_not_found(client: AsyncClient):
    """Test getting a non-existent session."""
    response = await client.get("/api/v1/sessions/nonexistent-id")
    assert response.status_code == 404


# ── Prompt 9: family access to a producer's avatar ──────────────────────────


@pytest.fixture
async def ready_avatar(db_session, test_user):
    avatar = Avatar(
        user_id=test_user.id,
        name="Storyteller",
        image_url="http://x/i.jpg",
        s3_key="avatars/x/image.jpg",
        status="ready",
    )
    db_session.add(avatar)
    await db_session.commit()
    await db_session.refresh(avatar)
    return avatar


@pytest.fixture
async def linked_family_user(db_session, test_user):
    user = User(
        email="family-linked@example.com",
        username="familylinked",
        hashed_password=get_password_hash("testpassword123"),
        role="family",
        producer_id=test_user.id,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def linked_family_headers(linked_family_user):
    token = create_access_token(data={"sub": linked_family_user.id})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def unlinked_family_user(db_session):
    user = User(
        email="family-unlinked@example.com",
        username="familyunlinked",
        hashed_password=get_password_hash("testpassword123"),
        role="family",
        producer_id=None,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def unlinked_family_headers(unlinked_family_user):
    token = create_access_token(data={"sub": unlinked_family_user.id})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_linked_family_member_can_create_session(
    client: AsyncClient, ready_avatar, linked_family_headers
):
    resp = await client.post(
        "/api/v1/sessions/create",
        json={"avatar_id": ready_avatar.id},
        headers=linked_family_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["avatar_id"] == ready_avatar.id


@pytest.mark.asyncio
async def test_unlinked_family_member_cannot_create_session(
    client: AsyncClient, ready_avatar, unlinked_family_headers
):
    resp = await client.post(
        "/api/v1/sessions/create",
        json={"avatar_id": ready_avatar.id},
        headers=unlinked_family_headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_producer_can_still_chat_with_own_avatar(
    client: AsyncClient, ready_avatar, auth_headers
):
    resp = await client.post(
        "/api/v1/sessions/create",
        json={"avatar_id": ready_avatar.id},
        headers=auth_headers,
    )
    assert resp.status_code == 201


# ── v2-primary: sessions without any avatar (docs/V2_PRIMARY_AVATAR_DORMANT_PLAN.md §3.2)


@pytest.mark.asyncio
async def test_a_v2_producer_with_zero_avatars_creates_a_session(
    client: AsyncClient, test_user, auth_headers
):
    """The defining case of the inversion: a producer who never opened
    Avatar Studio (no avatars row anywhere) starts a conversation against
    their own archive. chat_mode defaults to video_clips_v2."""
    resp = await client.post("/api/v1/sessions/create", json={}, headers=auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["producer_id"] == test_user.id
    assert body["avatar_id"] is None


@pytest.mark.asyncio
async def test_linked_family_creates_a_session_with_no_avatar_anywhere(
    client: AsyncClient, test_user, linked_family_user, linked_family_headers
):
    """Family access is derived from the account linkage (User.producer_id),
    not from an avatar's owner — so it works against a photo-less archive."""
    resp = await client.post(
        "/api/v1/sessions/create", json={}, headers=linked_family_headers
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["producer_id"] == test_user.id
    assert body["user_id"] == linked_family_user.id
    assert body["avatar_id"] is None


@pytest.mark.asyncio
async def test_unlinked_family_cannot_create_a_session_at_all(
    client: AsyncClient, unlinked_family_headers
):
    resp = await client.post(
        "/api/v1/sessions/create", json={}, headers=unlinked_family_headers
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_avatar_mode_resolves_the_ready_avatar_server_side(
    client: AsyncClient, db_session, test_user, ready_avatar, auth_headers
):
    """An avatar-mode create with no body avatar_id picks the producer's
    newest ready avatar — replacing the frontend's old `?? avatars[0]`
    guess, which could pick a non-ready avatar and fail obscurely."""
    test_user.chat_mode = "avatar"
    await db_session.commit()

    resp = await client.post("/api/v1/sessions/create", json={}, headers=auth_headers)
    assert resp.status_code == 201
    assert resp.json()["avatar_id"] == ready_avatar.id


@pytest.mark.asyncio
async def test_avatar_mode_without_a_ready_avatar_is_a_400_that_names_it(
    client: AsyncClient, db_session, test_user, auth_headers
):
    test_user.chat_mode = "avatar"
    await db_session.commit()

    resp = await client.post("/api/v1/sessions/create", json={}, headers=auth_headers)
    assert resp.status_code == 400
    assert "no avatar is ready" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_a_body_avatar_id_owned_by_someone_else_is_403(
    client: AsyncClient, db_session, test_user, auth_headers
):
    """The body can name which of the producer's avatars speaks; it can
    never widen access to another account's avatar."""
    other = User(
        email="other-producer@example.com",
        username="otherproducer",
        hashed_password=get_password_hash("testpassword123"),
    )
    db_session.add(other)
    await db_session.flush()
    foreign_avatar = Avatar(
        user_id=other.id,
        name="Foreign",
        image_url="http://x/f.jpg",
        s3_key="avatars/f/image.jpg",
        status="ready",
    )
    db_session.add(foreign_avatar)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/sessions/create",
        json={"avatar_id": foreign_avatar.id},
        headers=auth_headers,
    )
    assert resp.status_code == 403
