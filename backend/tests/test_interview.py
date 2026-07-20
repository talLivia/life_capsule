import pytest
from httpx import AsyncClient

from app.api.v1.users import create_access_token, get_password_hash
from app.models import User


@pytest.fixture(autouse=True)
def no_celery_broker(monkeypatch):
    """Never let a test touch a real Redis broker — the ingest endpoint
    enqueues analysis as a side effect we don't want network-dependent
    or slow in the unit-test suite."""
    from app.celery_app import analyze_segment_task

    monkeypatch.setattr(analyze_segment_task, "delay", lambda *a, **kw: None)


@pytest.fixture
async def family_user(db_session):
    user = User(
        email="family@example.com",
        username="familymember",
        hashed_password=get_password_hash("testpassword123"),
        role="family",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def family_auth_headers(family_user):
    token = create_access_token(data={"sub": family_user.id})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_questions_requires_auth(client: AsyncClient):
    response = await client.get("/api/v1/interview/questions")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_family_role_forbidden(client: AsyncClient, family_auth_headers):
    response = await client.get("/api/v1/interview/questions", headers=family_auth_headers)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_list_questions_default_hebrew(client: AsyncClient, auth_headers):
    """test_user has no recording_language set explicitly -> defaults to 'he'."""
    response = await client.get("/api/v1/interview/questions", headers=auth_headers)
    assert response.status_code == 200
    questions = response.json()
    assert len(questions) == 12
    assert questions[0]["index"] == 0
    assert questions[0]["category"] == "childhood"
    assert all("text" in q and q["text"] for q in questions)


@pytest.mark.asyncio
async def test_get_session_is_idempotent_across_calls(client: AsyncClient, auth_headers):
    """Simulates a browser refresh mid-interview: the second GET must return
    the SAME session, not create a new one, so progress isn't lost."""
    first = await client.get("/api/v1/interview/session", headers=auth_headers)
    second = await client.get("/api/v1/interview/session", headers=auth_headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["session"]["id"] == second.json()["session"]["id"]
    assert second.json()["session"]["current_question_index"] == 0
    assert second.json()["segments"] == []


@pytest.mark.asyncio
async def test_update_session_navigation(client: AsyncClient, auth_headers):
    session = (await client.get("/api/v1/interview/session", headers=auth_headers)).json()["session"]

    resp = await client.patch(
        f"/api/v1/interview/session/{session['id']}",
        json={"current_question_index": 3},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["current_question_index"] == 3

    # Navigate back
    resp = await client.patch(
        f"/api/v1/interview/session/{session['id']}",
        json={"current_question_index": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["current_question_index"] == 1


@pytest.mark.asyncio
async def test_update_session_out_of_range(client: AsyncClient, auth_headers):
    session = (await client.get("/api/v1/interview/session", headers=auth_headers)).json()["session"]
    resp = await client.patch(
        f"/api/v1/interview/session/{session['id']}",
        json={"current_question_index": 999},
        headers=auth_headers,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_session_other_users_session_forbidden(
    client: AsyncClient, auth_headers, family_auth_headers, family_user, db_session
):
    # Give the family user their own producer-like session directly in DB
    # (bypassing the role gate) so we can assert cross-user protection
    # independent of the role check.
    from app.models import InterviewSession

    other_session = InterviewSession(user_id=family_user.id, status="active")
    db_session.add(other_session)
    await db_session.commit()
    await db_session.refresh(other_session)

    resp = await client.patch(
        f"/api/v1/interview/session/{other_session.id}",
        json={"current_question_index": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_presign_local_mode_returns_backend_upload_url(client: AsyncClient, auth_headers):
    resp = await client.post(
        "/api/v1/interview/segments/presign",
        json={"question_index": 0, "content_type": "video/webm"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["video_key"].startswith("segments/")
    assert "/api/v1/interview/segments/upload-local/" in data["upload_url"]


@pytest.mark.asyncio
async def test_ingest_segment_creates_pending_transcription(client: AsyncClient, auth_headers, test_user):
    session = (await client.get("/api/v1/interview/session", headers=auth_headers)).json()["session"]
    video_key = f"segments/{test_user.id}/{session['id']}/0/take.webm"

    resp = await client.post(
        "/api/v1/interview/segments/ingest",
        json={
            "interview_session_id": session["id"],
            "question_index": 0,
            "question_asked": "Tell me about your childhood",
            "video_key": video_key,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending_transcription"
    assert body["question_index"] == 0
    assert body["video_key"] == video_key


@pytest.mark.asyncio
async def test_ingest_segment_rerecord_upserts(client: AsyncClient, auth_headers, test_user):
    session = (await client.get("/api/v1/interview/session", headers=auth_headers)).json()["session"]
    first_key = f"segments/{test_user.id}/{session['id']}/0/take1.webm"
    second_key = f"segments/{test_user.id}/{session['id']}/0/take2.webm"

    first = await client.post(
        "/api/v1/interview/segments/ingest",
        json={
            "interview_session_id": session["id"],
            "question_index": 0,
            "question_asked": "Q1",
            "video_key": first_key,
        },
        headers=auth_headers,
    )
    second = await client.post(
        "/api/v1/interview/segments/ingest",
        json={
            "interview_session_id": session["id"],
            "question_index": 0,
            "question_asked": "Q1",
            "video_key": second_key,
        },
        headers=auth_headers,
    )
    assert first.status_code == 201
    assert second.status_code == 201
    # Same row (re-record), not a duplicate.
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["video_key"] == second_key

    segments = await client.get(
        f"/api/v1/interview/segments/session/{session['id']}", headers=auth_headers
    )
    assert len(segments.json()) == 1


@pytest.mark.asyncio
async def test_ingest_segment_rejects_foreign_video_key(client: AsyncClient, auth_headers):
    session = (await client.get("/api/v1/interview/session", headers=auth_headers)).json()["session"]
    resp = await client.post(
        "/api/v1/interview/segments/ingest",
        json={
            "interview_session_id": session["id"],
            "question_index": 0,
            "question_asked": "Q1",
            "video_key": "segments/someone-else/other-session/0/take.webm",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400
