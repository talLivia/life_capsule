"""
Step 3 of docs/INTERVIEW_RESTRUCTURE.md — gate-answer persistence.

`value` carries no FK or CHECK, because a gate's options live in
interview_questions.json and duplicating that vocabulary into the database
would need a migration every time a screening question gains an option. That
makes app/services/gate_answers.py the ONLY thing standing between a client
and an unresolvable answer, so its validation is tested harder than a column
constraint would need to be.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import interview_config as ic
from app.models import InterviewGateAnswer, InterviewSession, User
from app.services import gate_answers
from app.services.gate_answers import InvalidGateAnswer

V2_PATH = Path(__file__).resolve().parent.parent / "app" / "interview_questions_v2.json"


@pytest.fixture
def v2(monkeypatch):
    with open(V2_PATH, encoding="utf-8") as f:
        doc = json.load(f)
    monkeypatch.setattr(ic, "_load_all", lambda: doc)
    ic.cache_clear()
    yield doc
    ic.cache_clear()


@pytest.fixture
async def session(db_session):
    user = User(
        id="u-gate", email="g@example.com", username="gate",
        hashed_password="x", role="producer",
    )
    db_session.add(user)
    await db_session.flush()
    isession = InterviewSession(user_id=user.id, status="active")
    db_session.add(isession)
    await db_session.flush()
    return isession


async def test_records_and_reads_back_an_answer(v2, db_session, session):
    await gate_answers.set_answer(db_session, session.id, "gate_military_service", "no")
    assert await gate_answers.get_answers(db_session, session.id) == {
        "gate_military_service": "no"
    }


async def test_answers_come_back_as_the_shape_resolve_steps_takes(v2, db_session, session):
    """The store's output feeds interview_config directly — if the shape drifts
    the flow silently resolves nothing."""
    await gate_answers.set_answer(db_session, session.id, "gate_military_service", "yes")
    answers = await gate_answers.get_answers(db_session, session.id)
    cat = ic.get_category("he", "military_service")
    assert len(ic.resolve_questions(cat["steps"], answers)) == 8


async def test_re_answering_upserts_rather_than_duplicating(v2, db_session, session):
    """The unique key makes a second row impossible, but the store must not
    depend on hitting a constraint to notice."""
    await gate_answers.set_answer(db_session, session.id, "gate_military_service", "yes")
    await gate_answers.set_answer(db_session, session.id, "gate_military_service", "no")

    rows = (
        await db_session.execute(
            select(InterviewGateAnswer).where(
                InterviewGateAnswer.interview_session_id == session.id
            )
        )
    ).scalars().all()
    assert len(rows) == 1 and rows[0].value == "no"


async def test_changing_an_answer_never_deletes_recordings(v2, db_session, session):
    """INTERVIEW_RESTRUCTURE §8.3: footage is never destroyed by a navigation
    action. The store must touch nothing but its own row."""
    from app.models import RawSegment

    seg = RawSegment(
        interview_session_id=session.id,
        question_asked="whatever was asked",
        question_index=0,
        question_id="military_service_q01",
        status="ready",
    )
    db_session.add(seg)
    await db_session.flush()

    await gate_answers.set_answer(db_session, session.id, "gate_military_service", "yes")
    await gate_answers.set_answer(db_session, session.id, "gate_military_service", "no")

    surviving = (
        await db_session.execute(
            select(RawSegment).where(RawSegment.interview_session_id == session.id)
        )
    ).scalars().all()
    assert len(surviving) == 1, "a gate change must not remove footage"


async def test_rejects_a_value_the_gate_does_not_offer(v2, db_session, session):
    with pytest.raises(InvalidGateAnswer) as exc:
        await gate_answers.set_answer(
            db_session, session.id, "gate_military_service", "maybe"
        )
    # the message must name the real options, or a client bug is undiagnosable
    assert "yes" in str(exc.value) and "no" in str(exc.value)


async def test_rejects_a_question_id_used_as_a_gate(v2, db_session, session):
    """A question takes footage, not an answer. Accepting one here would store
    a row the flow can never resolve."""
    with pytest.raises(InvalidGateAnswer):
        await gate_answers.set_answer(
            db_session, session.id, "military_service_q01", "yes"
        )


async def test_rejects_an_unknown_gate(v2, db_session, session):
    with pytest.raises(InvalidGateAnswer):
        await gate_answers.set_answer(db_session, session.id, "gate_invented", "yes")


async def test_accepts_every_option_the_data_declares(v2, db_session, session):
    """Three-way gates go through the same path as yes/no ones — the store
    must not assume two options anywhere."""
    for value in ic.gate_option_values("gate_relationships_status"):
        await gate_answers.set_answer(
            db_session, session.id, "gate_relationships_status", value
        )
    answers = await gate_answers.get_answers(db_session, session.id)
    assert answers["gate_relationships_status"] in {
        "together", "widowed", "separated_divorced",
    }


async def test_clearing_returns_the_gate_to_unanswered(v2, db_session, session):
    await gate_answers.set_answer(db_session, session.id, "gate_military_service", "no")
    assert await gate_answers.clear_answer(db_session, session.id, "gate_military_service")
    assert await gate_answers.get_answers(db_session, session.id) == {}
    # clearing something absent is not an error
    assert not await gate_answers.clear_answer(
        db_session, session.id, "gate_military_service"
    )


async def test_reports_answers_the_question_set_no_longer_recognises(
    v2, db_session, session, monkeypatch
):
    """Sessions outlive question-set edits. resolve_steps already treats these
    as unanswered; this reports them without deleting anything on the strength
    of a file having changed."""
    await gate_answers.set_answer(db_session, session.id, "gate_military_service", "yes")
    assert await gate_answers.unresolvable_answers(db_session, session.id) == []

    # the gate loses the option that was chosen
    doc = json.loads(V2_PATH.read_text(encoding="utf-8"))
    for cat in doc["languages"]["he"]["categories"]:
        for step in cat["steps"]:
            if step.get("id") == "gate_military_service":
                step["options"] = [o for o in step["options"] if o["value"] != "yes"]
                step["options"].append({"value": "later", "label": "…", "steps": []})
    monkeypatch.setattr(ic, "_load_all", lambda: doc)
    ic.cache_clear()

    assert await gate_answers.unresolvable_answers(db_session, session.id) == [
        "gate_military_service"
    ]
    # and the row is still there — reporting, not repair
    assert "gate_military_service" in await gate_answers.get_answers(db_session, session.id)


async def test_answers_are_scoped_to_their_session(v2, db_session, session):
    other = InterviewSession(user_id=session.user_id, status="active")
    db_session.add(other)
    await db_session.flush()

    await gate_answers.set_answer(db_session, session.id, "gate_military_service", "yes")
    await gate_answers.set_answer(db_session, other.id, "gate_military_service", "no")

    assert (await gate_answers.get_answers(db_session, session.id))[
        "gate_military_service"
    ] == "yes"
    assert (await gate_answers.get_answers(db_session, other.id))[
        "gate_military_service"
    ] == "no"


async def test_free_navigation_defaults_to_off(db_session):
    """A new producer gets the guided experience; free navigation is opt-in."""
    user = User(
        id="u-nav", email="n@example.com", username="nav",
        hashed_password="x", role="producer",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    assert user.free_navigation is False
