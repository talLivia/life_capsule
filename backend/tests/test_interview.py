import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1.users import create_access_token, get_password_hash
from app.models import User


@pytest.fixture(autouse=True)
def deletion_uses_test_db(test_engine, monkeypatch):
    """segment_deletion opens its OWN session (it is called from places that
    don't have one), so without this the replace-on-re-record path would run
    its delete against the real configured database instead of the test one —
    the row would survive and the test would report a phantom duplicate."""
    from app.services import segment_deletion

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(segment_deletion, "AsyncSessionLocal", factory)
    monkeypatch.setattr(
        segment_deletion.graph_memory, "remove_episodes_for_segment", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        segment_deletion.storage_service, "delete_file", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(segment_deletion, "_refresh_caches", AsyncMock())


@pytest.fixture(autouse=True)
def no_celery_broker(monkeypatch):
    """Never let a test touch a real Redis broker — the ingest endpoint
    enqueues analysis as a side effect we don't want network-dependent
    or slow in the unit-test suite.

    Also forces settings.DEBUG=False here: the real .env this test suite
    loads has DEBUG=true (for local dev), and ingest_segment's DEBUG-gated
    path runs analysis_graph.run_segment_analysis directly via
    asyncio.create_task - unmocked, that's a real LLM/Neo4j-calling
    background task, which every ordinary ingest test would otherwise
    kick off unintentionally. Tests that specifically want to exercise
    that DEBUG path (see test_ingest_segment_runs_analysis_in_process_
    when_debug) override settings.DEBUG back to True themselves and mock
    run_segment_analysis explicitly."""
    from app.api.v1 import interview as interview_module
    from app.celery_app import analyze_segment_task

    monkeypatch.setattr(analyze_segment_task, "delay", lambda *a, **kw: None)
    monkeypatch.setattr(interview_module.settings, "DEBUG", False)


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
async def test_ingest_segment_runs_analysis_in_process_when_debug(
    client: AsyncClient, auth_headers, test_user, monkeypatch
):
    """Local dev has no Celery worker/broker running by default, and
    measured directly, Kombu's connection/retry behavior against an
    unreachable broker hangs well past axios's 30s client-side timeout
    rather than failing fast - so DEBUG=true must skip attempting Celery
    entirely and run the pipeline in-process, not try-then-fall-back."""
    from app.api.v1 import interview as interview_module
    from app.celery_app import analyze_segment_task

    monkeypatch.setattr(interview_module.settings, "DEBUG", True)
    mock_delay = AsyncMock()
    monkeypatch.setattr(analyze_segment_task, "delay", mock_delay)
    mock_run_analysis = AsyncMock()
    monkeypatch.setattr("app.analysis_graph.run_segment_analysis", mock_run_analysis)

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
    segment_id = resp.json()["id"]

    await asyncio.sleep(0)  # let the scheduled asyncio.create_task actually run
    mock_run_analysis.assert_awaited_once_with(segment_id)
    mock_delay.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_segment_appends_sibling(client: AsyncClient, auth_headers, test_user):
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
    # One question can hold SEVERAL recordings — a second take ADDS, it does
    # not destroy the first. Replacing a specific take is delete + add.
    assert first.json()["id"] != second.json()["id"]
    assert second.json()["video_key"] == second_key

    state = (await client.get("/api/v1/interview/session", headers=auth_headers)).json()
    for_q0 = [s for s in state["segments"] if s["question_index"] == 0]
    assert len(for_q0) == 2, "both takes are kept"
    assert {s["id"] for s in for_q0} == {first.json()["id"], second.json()["id"]}
    # The FIRST take must still be intact, not blanked in place.
    kept = next(s for s in for_q0 if s["id"] == first.json()["id"])
    assert kept["video_key"] == first_key

    segments = await client.get(
        f"/api/v1/interview/segments/session/{session['id']}", headers=auth_headers
    )
    assert len(segments.json()) == 2, "the session lists both takes, not just the latest"


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


@pytest.mark.asyncio
async def test_delete_segment_removes_only_that_take(
    client: AsyncClient, auth_headers, test_user, db_session, monkeypatch
):
    """Deleting one take leaves its siblings on the same question alone.

    With several takes per question this is the ONLY way a recording is
    destroyed — ingest no longer replaces — so the endpoint has to be
    precise about which one goes.
    """
    from app.api.v1 import interview as interview_module

    deleted: list[str] = []

    async def fake_delete(segment_id, group_id, **kw):
        from app.models import RawSegment as RS
        from app.services.segment_deletion import DeletionResult

        deleted.append(segment_id)
        # Stands in for the real fan-out (neither storage nor Graphiti runs
        # here) but DOES delete the row, so the assertions below are about
        # observable state rather than about the mock. It must use the test's
        # own session: the real function opens AsyncSessionLocal, which is a
        # different engine from the one the client is overridden onto, so a
        # row deleted there would not be missing here.
        row = (await db_session.execute(select(RS).where(RS.id == segment_id))).scalar_one_or_none()
        if row is not None:
            await db_session.delete(row)
            await db_session.commit()
        return DeletionResult(segments_deleted=1)

    monkeypatch.setattr(
        "app.services.segment_deletion.delete_segment_data", fake_delete
    )

    session = (await client.get("/api/v1/interview/session", headers=auth_headers)).json()["session"]
    ids = []
    for n in (1, 2):
        r = await client.post(
            "/api/v1/interview/segments/ingest",
            json={
                "interview_session_id": session["id"],
                "question_index": 0,
                "question_asked": "Q1",
                "video_key": f"segments/{test_user.id}/{session['id']}/0/take{n}.webm",
            },
            headers=auth_headers,
        )
        ids.append(r.json()["id"])

    resp = await client.delete(f"/api/v1/interview/segments/{ids[0]}", headers=auth_headers)
    assert resp.status_code == 204
    assert deleted == [ids[0]], "delegates to the shared deletion path, for that take only"

    state = (await client.get("/api/v1/interview/session", headers=auth_headers)).json()
    remaining = [s["id"] for s in state["segments"] if s["question_index"] == 0]
    assert remaining == [ids[1]], "the sibling survives"


@pytest.mark.asyncio
async def test_delete_segment_rejects_another_producers_recording(
    client: AsyncClient, auth_headers, test_user, db_session
):
    """404, not 403: distinguishing "not yours" from "doesn't exist" would
    confirm that another producer's recording id is real."""
    from app.models import InterviewSession as IS, RawSegment as RS, User

    other = User(
        id="other-producer", email="other@example.com", username="other",
        hashed_password="x", full_name="Other", role="producer",
    )
    db_session.add(other)
    await db_session.flush()
    other_session = IS(id="other-session", user_id=other.id, status="active")
    db_session.add(other_session)
    await db_session.flush()
    db_session.add(RS(
        id="other-seg", interview_session_id=other_session.id,
        question_asked="Q", question_index=0, status="ready",
        video_key="segments/other/x.webm",
    ))
    await db_session.commit()

    resp = await client.delete("/api/v1/interview/segments/other-seg", headers=auth_headers)
    assert resp.status_code == 404

    still_there = (await db_session.execute(select(RS).where(RS.id == "other-seg"))).scalar_one_or_none()
    assert still_there is not None, "and it is genuinely untouched, not just hidden"


@pytest.mark.asyncio
async def test_delete_segment_404_when_missing(client: AsyncClient, auth_headers):
    resp = await client.delete("/api/v1/interview/segments/nope", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type,expected_ext",
    [
        ("video/webm", "webm"),
        ("video/webm;codecs=vp8,opus", "webm"),  # what MediaRecorder actually sends
        ("video/mp4", "mp4"),
        ("video/quicktime", "mov"),  # .mov straight off a phone
    ],
)
async def test_presign_accepts_every_supported_video_type(
    client: AsyncClient, auth_headers, content_type, expected_ext
):
    """Upload reuses the RECORDING entry point, so presign has to hand out a
    correctly-named key for files that never came from MediaRecorder."""
    resp = await client.post(
        "/api/v1/interview/segments/presign",
        json={"question_index": 0, "content_type": content_type},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["video_key"].endswith(f".{expected_ext}")


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["application/pdf", "image/png", "text/plain", "video/x-msvideo"])
async def test_presign_rejects_non_video(client: AsyncClient, auth_headers, content_type):
    """Rejected at the door, not silently renamed.

    The old fallback named anything unrecognised `.webm`, so a PDF became a
    `.webm` key and only surfaced as a decode failure deep inside
    transcription — minutes later, nowhere near the file picker. Recording
    could never hit that; uploading can.
    """
    resp = await client.post(
        "/api/v1/interview/segments/presign",
        json={"question_index": 0, "content_type": content_type},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "video" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_local_upload_rejects_oversized_video(
    client: AsyncClient, auth_headers, test_user, monkeypatch
):
    """The local PUT is the one place the bytes pass through us, so the cap
    is enforceable there. (In R2 mode the browser PUTs straight to storage
    and a presigned URL carries no size condition — see the endpoint.)"""
    from app.api.v1 import interview as interview_module

    monkeypatch.setattr(interview_module.settings, "USE_LOCAL_STORAGE", True)
    monkeypatch.setattr(interview_module.settings, "MAX_SEGMENT_UPLOAD_BYTES", 1024)
    stored = AsyncMock()
    monkeypatch.setattr(interview_module.storage_service, "upload_file", stored)

    key = f"segments/{test_user.id}/sess/0/big.webm"
    resp = await client.put(
        f"/api/v1/interview/segments/upload-local/{key}",
        content=b"x" * 4096,
        headers={**auth_headers, "content-type": "video/webm"},
    )
    assert resp.status_code == 413
    stored.assert_not_awaited()  # and nothing was written


@pytest.mark.asyncio
async def test_local_upload_accepts_a_video_within_the_cap(
    client: AsyncClient, auth_headers, test_user, monkeypatch
):
    from app.api.v1 import interview as interview_module

    monkeypatch.setattr(interview_module.settings, "USE_LOCAL_STORAGE", True)
    stored = AsyncMock()
    monkeypatch.setattr(interview_module.storage_service, "upload_file", stored)

    key = f"segments/{test_user.id}/sess/0/ok.mp4"
    resp = await client.put(
        f"/api/v1/interview/segments/upload-local/{key}",
        content=b"x" * 512,
        headers={**auth_headers, "content-type": "video/mp4"},
    )
    assert resp.status_code == 204
    stored.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_upload_rejects_non_video_content_type(
    client: AsyncClient, auth_headers, test_user, monkeypatch
):
    """Presign is not the only door — the PUT is separately reachable, so it
    validates rather than trusting that presign already did."""
    from app.api.v1 import interview as interview_module

    monkeypatch.setattr(interview_module.settings, "USE_LOCAL_STORAGE", True)
    stored = AsyncMock()
    monkeypatch.setattr(interview_module.storage_service, "upload_file", stored)

    key = f"segments/{test_user.id}/sess/0/sneaky.webm"
    resp = await client.put(
        f"/api/v1/interview/segments/upload-local/{key}",
        content=b"%PDF-1.4",
        headers={**auth_headers, "content-type": "application/pdf"},
    )
    assert resp.status_code == 400
    stored.assert_not_awaited()
