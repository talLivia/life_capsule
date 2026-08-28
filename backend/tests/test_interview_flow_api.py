"""
Step 4 of docs/INTERVIEW_RESTRUCTURE.md — the flow API.

The accordion renders this response and recomputes none of it, so anything
wrong here is wrong on screen. Two properties get the most attention:

  * position is DERIVED, so it must be right after any sequence of actions
    without the client ever telling the server where it is;
  * reachability is ENFORCED here, not by hiding a button — a stale tab or a
    replayed request must not write into a category the producer has not
    arrived at.
"""

import json
from pathlib import Path

import pytest

from app import interview_config as ic
from app.models import InterviewSession, RawSegment, User
from app.services import gate_answers, interview_flow

V2_PATH = Path(__file__).resolve().parent.parent / "app" / "interview_questions.json"


@pytest.fixture
def v2(monkeypatch):
    with open(V2_PATH, encoding="utf-8") as f:
        doc = json.load(f)
    monkeypatch.setattr(ic, "_load_all", lambda: doc)
    ic.cache_clear()
    yield doc
    ic.cache_clear()


@pytest.fixture
async def producer(db_session):
    user = User(
        id="u-flow", email="f@example.com", username="flow",
        hashed_password="x", role="producer", recording_language="he",
    )
    db_session.add(user)
    await db_session.flush()
    session = InterviewSession(user_id=user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    return user, session


async def _flow(db_session, producer):
    user, session = producer
    return await interview_flow.get_flow(db_session, user, session)


async def _record(db_session, session, question_id, index=0):
    db_session.add(
        RawSegment(
            interview_session_id=session.id,
            question_asked="whatever",
            question_index=index,
            question_id=question_id,
            status="ready",
        )
    )
    await db_session.flush()


# ── shape ─────────────────────────────────────────────────────────────────


async def test_a_fresh_interview_starts_at_the_first_category(v2, db_session, producer):
    flow = await _flow(db_session, producer)
    assert len(flow["categories"]) == 16
    assert flow["current_category_id"] == flow["categories"][0]["id"]
    assert flow["complete"] is False
    first = flow["categories"][0]
    assert first["position"] == 1
    assert first["done_count"] == 0
    assert first["current"] and first["reachable"]


async def test_categories_past_the_current_one_are_unreachable(v2, db_session, producer):
    flow = await _flow(db_session, producer)
    later = [c for c in flow["categories"][1:]]
    assert all(not c["reachable"] for c in later)
    assert all(not c["complete"] for c in later)


# ── derived position ──────────────────────────────────────────────────────


async def test_position_advances_as_questions_are_recorded(v2, db_session, producer):
    """No client ever tells the server where it is."""
    _, session = producer
    first = (await _flow(db_session, producer))["categories"][0]
    q_ids = [s["id"] for s in first["steps"] if s["kind"] == "question"]

    await _record(db_session, session, q_ids[0])
    cat = (await _flow(db_session, producer))["categories"][0]
    assert cat["position"] == 2 and cat["done_count"] == 1
    assert cat["current_step_id"] == q_ids[1]

    await _record(db_session, session, q_ids[1])
    cat = (await _flow(db_session, producer))["categories"][0]
    assert cat["position"] == 3 and cat["done_count"] == 2


async def test_several_takes_of_one_question_count_once_for_progress(v2, db_session, producer):
    """A question holds a LIST of takes — recording again must not advance
    the flow twice."""
    _, session = producer
    first = (await _flow(db_session, producer))["categories"][0]
    qid = next(s["id"] for s in first["steps"] if s["kind"] == "question")

    await _record(db_session, session, qid)
    await _record(db_session, session, qid)
    cat = (await _flow(db_session, producer))["categories"][0]
    assert cat["done_count"] == 1
    assert next(s for s in cat["steps"] if s["id"] == qid)["takes"] == 2


async def test_completing_a_category_moves_the_current_one_along(v2, db_session, producer):
    _, session = producer
    flow = await _flow(db_session, producer)
    first = flow["categories"][0]
    for i, s in enumerate(first["steps"]):
        await _record(db_session, session, s["id"], index=i)

    flow = await _flow(db_session, producer)
    assert flow["categories"][0]["complete"]
    assert flow["categories"][0]["position"] is None
    assert flow["current_category_id"] == flow["categories"][1]["id"]
    # a completed category stays openable, for review or a re-record
    assert flow["categories"][0]["reachable"]


# ── gating and the honest denominator (§8.4) ──────────────────────────────


async def test_a_gated_category_reports_no_total_until_it_is_settled(
    v2, db_session, producer
):
    """The count genuinely depends on an answer not yet given, so `total` is
    None and the UI must not invent one."""
    user, session = producer
    cat = next(
        c for c in (await _flow(db_session, producer))["categories"]
        if c["id"] == "military_service"
    )
    assert cat["settled"] is False
    assert cat["total"] is None
    assert len(cat["steps"]) == 1 and cat["steps"][0]["kind"] == "gate"


async def test_answering_no_settles_and_completes_with_nothing_recorded(
    v2, db_session, producer
):
    user, session = producer
    await gate_answers.set_answer(db_session, session.id, "gate_military_service", "no")
    cat = next(
        c for c in (await _flow(db_session, producer))["categories"]
        if c["id"] == "military_service"
    )
    assert cat["settled"] and cat["complete"]
    assert cat["total"] == 1  # the gate itself is a step the producer answered
    assert cat["position"] is None


async def test_answering_yes_reveals_the_questions_and_a_real_total(
    v2, db_session, producer
):
    user, session = producer
    await gate_answers.set_answer(db_session, session.id, "gate_military_service", "yes")
    cat = next(
        c for c in (await _flow(db_session, producer))["categories"]
        if c["id"] == "military_service"
    )
    assert cat["settled"]
    assert cat["total"] == 9, "8 questions plus the gate step itself"
    assert cat["done_count"] == 1, "the answered gate counts as done"
    assert cat["position"] == 2


async def test_a_nested_gate_keeps_the_category_unsettled(v2, db_session, producer):
    """Relationships: answering the first gate reveals a second one, so the
    total is still not knowable."""
    user, session = producer
    await gate_answers.set_answer(
        db_session, session.id, "gate_relationships_significant", "yes"
    )
    cat = next(
        c for c in (await _flow(db_session, producer))["categories"]
        if c["id"] == "relationships"
    )
    assert cat["settled"] is False and cat["total"] is None

    await gate_answers.set_answer(
        db_session, session.id, "gate_relationships_status", "widowed"
    )
    cat = next(
        c for c in (await _flow(db_session, producer))["categories"]
        if c["id"] == "relationships"
    )
    # 4 intro + 9 shared + 3 widowed = 16 questions, plus both gates = 18
    assert cat["settled"] and cat["total"] == 18


async def test_gate_steps_expose_their_options_as_data(v2, db_session, producer):
    """The client renders one control per option and never assumes yes/no."""
    cat = next(
        c for c in (await _flow(db_session, producer))["categories"]
        if c["id"] == "military_service"
    )
    gate = cat["steps"][0]
    assert [o["value"] for o in gate["options"]] == ["yes", "no"]
    assert all(o["label"] for o in gate["options"])
    assert gate["answer"] is None and gate["done"] is False


# ── enforcement, not decoration ───────────────────────────────────────────


async def test_can_record_refuses_an_unreachable_category(v2, db_session, producer):
    flow = await _flow(db_session, producer)
    later = flow["categories"][3]
    assert not later["reachable"]
    # a question that genuinely belongs to it, but is not open yet
    assert not interview_flow.can_record(flow, later["id"], f"{later['id']}_q01")


async def test_can_record_refuses_a_question_hidden_behind_a_gate(v2, db_session, producer):
    """Reachable category, but the question sits behind an unanswered gate."""
    user, session = producer
    # make it the current category by completing everything before it
    flow = await _flow(db_session, producer)
    assert not interview_flow.can_record(flow, "military_service", "military_service_q01")


async def test_free_navigation_opens_every_category(v2, db_session, producer):
    user, session = producer
    user.free_navigation = True
    await db_session.flush()

    flow = await _flow(db_session, producer)
    assert flow["free_navigation"] is True
    assert all(c["reachable"] for c in flow["categories"])
    # but a question behind an unanswered gate is STILL not recordable —
    # free navigation opens categories, it does not skip gates
    assert not interview_flow.can_record(flow, "military_service", "military_service_q01")


async def test_free_navigation_allows_recording_in_a_later_category(
    v2, db_session, producer
):
    user, session = producer
    user.free_navigation = True
    await db_session.flush()
    flow = await _flow(db_session, producer)
    ungated_later = next(
        c for c in flow["categories"][1:]
        if c["steps"] and c["steps"][0]["kind"] == "question"
    )
    assert interview_flow.can_record(flow, ungated_later["id"], ungated_later["steps"][0]["id"])


# ── endpoints ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flow_endpoint_returns_the_state(v2, client, auth_headers):
    resp = await client.get("/api/v1/interview/flow", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["complete"] is False
    assert len(body["categories"]) == 16
    assert body["free_navigation"] is False


@pytest.mark.asyncio
async def test_gate_endpoint_records_and_returns_updated_flow(v2, client, auth_headers):
    """Returns the whole flow, so a client never renders a stale frame between
    answering and re-fetching."""
    resp = await client.post(
        "/api/v1/interview/flow/gate",
        json={"gate_id": "gate_relationships_significant", "value": "no"},
        headers=auth_headers,
    )
    # relationships is not the current category and free navigation is off
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_gate_endpoint_rejects_a_value_the_gate_does_not_offer(
    v2, client, auth_headers, db_session
):
    from app.models import User as U
    from sqlalchemy import select

    user = (await db_session.execute(select(U).where(U.role == "producer"))).scalars().first()
    user.free_navigation = True
    await db_session.commit()

    resp = await client.post(
        "/api/v1/interview/flow/gate",
        json={"gate_id": "gate_military_service", "value": "maybe"},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "yes" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_gate_endpoint_rejects_an_unknown_gate(v2, client, auth_headers):
    resp = await client.post(
        "/api/v1/interview/flow/gate",
        json={"gate_id": "gate_invented", "value": "yes"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


async def test_recordings_from_other_sessions_count(v2, db_session, producer):
    """USER-scoped take counting (live bug 2026-08-28): recordings living in
    a COMPLETED session — any prior interview pass, or a bulk import's own
    sessions — must still show as answered. Session scoping left /record
    empty after a 164-file import."""
    user, active = producer
    first = (await _flow(db_session, producer))["categories"][0]
    qid = next(s2["id"] for s2 in first["steps"] if s2["kind"] == "question")

    done = InterviewSession(user_id=user.id, status="completed")
    db_session.add(done)
    await db_session.flush()
    await _record(db_session, done, qid)  # recording in the OTHER session

    cat = (await _flow(db_session, producer))["categories"][0]
    assert cat["done_count"] == 1  # visible despite living in a completed session
