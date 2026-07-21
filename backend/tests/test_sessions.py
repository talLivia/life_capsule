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
