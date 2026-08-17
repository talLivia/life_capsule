"""Presenter videos (docs/PRESENTER_VIDEOS_PLAN.md): keys by convention,
no mapping file — so the load-bearing tests are that the id walk really
covers the whole interview INCLUDING gate branches, in document order,
and that the endpoint derives one URL per id plus the intro."""

import pytest
from httpx import AsyncClient

from app import interview_config
from app.api.v1 import interview as interview_api
from app.api.v1.users import create_access_token, get_password_hash
from app.models import User


def test_all_question_ids_covers_gate_branches_in_document_order():
    ids = interview_config.all_question_ids("he")
    # The number the whole feature is built around: 129 files ↔ 129
    # questions. If this changes, the presenter set is incomplete and the
    # upload script's completeness check is what must catch it.
    assert len(ids) == 129
    assert ids[0] == "childhood_q01"
    # Branch questions (inside a gate's options[].steps) are included —
    # the original flat-walk bug counted only 89.
    assert "military_service_q01" in ids
    assert "holocaust_q01" in ids
    # Gates themselves are excluded: no presenter video exists for them.
    assert not any(i.startswith("gate_") for i in ids)
    assert len(set(ids)) == len(ids)
    # Document order: holocaust (gated) comes after adolescence, before
    # military — same order the presenter read them.
    assert ids.index("holocaust_q01") < ids.index("military_service_q01")


@pytest.mark.asyncio
async def test_presenter_videos_requires_auth(client: AsyncClient):
    response = await client.get("/api/v1/interview/presenter-videos")
    assert response.status_code == 401


@pytest.fixture
async def family_auth_headers_local(db_session):
    user = User(
        email="family-pv@example.com",
        username="familypv",
        hashed_password=get_password_hash("testpassword123"),
        role="family",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    token = create_access_token(data={"sub": user.id})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_presenter_videos_is_producer_only(
    client: AsyncClient, family_auth_headers_local
):
    response = await client.get(
        "/api/v1/interview/presenter-videos", headers=family_auth_headers_local
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_presenter_videos_returns_one_url_per_question_plus_intro(
    client: AsyncClient, auth_headers, monkeypatch
):
    async def fake_serving_url(key, ttl_seconds=3600):
        return f"https://signed.test/{key}"

    monkeypatch.setattr(
        interview_api.storage_service, "serving_url", fake_serving_url
    )

    response = await client.get(
        "/api/v1/interview/presenter-videos", headers=auth_headers
    )
    assert response.status_code == 200
    body = response.json()

    ids = interview_config.all_question_ids("he")
    assert set(body["questions"].keys()) == set(ids)
    # Convention is the contract: presenter/{id}.mp4, nothing configurable.
    assert body["questions"]["childhood_q01"] == (
        "https://signed.test/presenter/childhood_q01.mp4"
    )
    assert body["questions"]["military_service_q01"] == (
        "https://signed.test/presenter/military_service_q01.mp4"
    )
    assert body["intro"] == "https://signed.test/presenter/intro.mp4"
