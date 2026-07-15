import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_register_user(client: AsyncClient):
    """Test user registration."""
    response = await client.post(
        "/api/v1/users/register",
        json={
            "email": "newuser@example.com",
            "username": "newuser",
            "full_name": "New User",
            "password": "securepassword123",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "newuser@example.com"
    assert data["username"] == "newuser"
    assert "id" in data
    assert "hashed_password" not in data


@pytest.mark.asyncio
async def test_register_duplicate_email(client: AsyncClient, test_user):
    """Test registration with existing email fails."""
    response = await client.post(
        "/api/v1/users/register",
        json={
            "email": "test@example.com",
            "username": "differentuser",
            "password": "password123",
        },
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_login_success(client: AsyncClient, test_user):
    """Test login with valid credentials."""
    response = await client.post(
        "/api/v1/users/login",
        data={
            "username": "test@example.com",
            "password": "testpassword123",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_login_sets_httponly_cookie(client: AsyncClient, test_user):
    """Login sets an httpOnly auth cookie so the token isn't exposed to JS."""
    response = await client.post(
        "/api/v1/users/login",
        data={"username": "test@example.com", "password": "testpassword123"},
    )
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie
    assert "httponly" in set_cookie.lower()


@pytest.mark.asyncio
async def test_cookie_authenticates_without_bearer_header(client: AsyncClient, test_user):
    """After login, the persisted cookie alone authenticates /me (no header)."""
    await client.post(
        "/api/v1/users/login",
        data={"username": "test@example.com", "password": "testpassword123"},
    )
    # httpx keeps the Set-Cookie in its jar — this request carries no
    # Authorization header, only the cookie.
    response = await client.get("/api/v1/users/me")
    assert response.status_code == 200
    assert response.json()["email"] == "test@example.com"


@pytest.mark.asyncio
async def test_logout_clears_cookie(client: AsyncClient, test_user):
    """Logout removes the cookie, so subsequent cookie-only requests are 401."""
    await client.post(
        "/api/v1/users/login",
        data={"username": "test@example.com", "password": "testpassword123"},
    )
    logout = await client.post("/api/v1/users/logout")
    assert logout.status_code == 200
    # Cookie jar cleared → /me without a bearer header is now unauthenticated.
    client.cookies.clear()
    response = await client.get("/api/v1/users/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_wrong_password(client: AsyncClient, test_user):
    """Test login with incorrect password."""
    response = await client.post(
        "/api/v1/users/login",
        data={
            "username": "test@example.com",
            "password": "wrongpassword",
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_profile(client: AsyncClient, test_user, auth_headers):
    """Test getting current user profile."""
    response = await client.get("/api/v1/users/me", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "test@example.com"
    assert data["username"] == "testuser"


@pytest.mark.asyncio
async def test_get_profile_unauthorized(client: AsyncClient):
    """Test getting profile without authentication."""
    response = await client.get("/api/v1/users/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_update_profile(client: AsyncClient, test_user, auth_headers):
    """Test updating user profile."""
    response = await client.put(
        "/api/v1/users/me",
        headers=auth_headers,
        json={"full_name": "Updated Name"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["full_name"] == "Updated Name"


@pytest.mark.asyncio
async def test_list_users_requires_auth(client: AsyncClient, test_user):
    """Listing users without a token is rejected (401)."""
    response = await client.get("/api/v1/users/")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_list_users_forbidden_for_non_superuser(client: AsyncClient, auth_headers):
    """A normal authenticated user cannot list all users (403)."""
    response = await client.get("/api/v1/users/", headers=auth_headers)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_list_users_superuser_ok(client: AsyncClient, db_session):
    """A superuser can list all users (200)."""
    from app.api.v1.users import create_access_token, get_password_hash
    from app.models import User

    admin = User(
        email="admin@example.com",
        username="admin",
        full_name="Admin",
        hashed_password=get_password_hash("adminpass123"),
        is_superuser=True,
    )
    db_session.add(admin)
    await db_session.commit()
    await db_session.refresh(admin)

    headers = {"Authorization": f"Bearer {create_access_token(data={'sub': admin.id})}"}
    response = await client.get("/api/v1/users/", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
