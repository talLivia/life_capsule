"""Derived timeline display data — docs/MEDIA_GALLERY.md §1.6.

What matters here is the COST CONTRACT, not the sentences: a summary is
generated when the recordings behind a category change and never otherwise,
a failure serves the stale sentence rather than a blank, and an organisation
is sent to the model once — with 'other' and NULL meaning different things,
the same asked-vs-never-asked split as the *_asked_at columns.
"""

import json
from unittest.mock import AsyncMock

import pytest

from app import interview_config
from app.models import Entity, EntityMention, InterviewSession, PeriodSummary, RawSegment, User
from app.services import period_insights, timeline


@pytest.fixture
async def archive(db_session):
    user = User(
        id="u-pi", email="pi@example.com", username="pi",
        hashed_password="x", role="producer", recording_language="he",
    )
    db_session.add(user)
    await db_session.flush()
    session = InterviewSession(user_id=user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    return user, session


async def _record(db, session, question_id, transcript="היה קיץ ארוך."):
    segment = RawSegment(
        interview_session_id=session.id, question_asked="?", question_index=0,
        question_id=question_id, status="ready", transcript=transcript,
    )
    db.add(segment)
    await db.flush()
    return segment


def _first_question_id():
    return interview_config.get_categories("he")[0]["question_ids"][0]


def _summary_calls(mock):
    """The generate_response calls that were summaries, not classifications."""
    return [c for c in mock.call_args_list if "summarize" in c.kwargs["system_prompt"]]


def _classify_calls(mock):
    return [c for c in mock.call_args_list if "Classify" in c.kwargs["system_prompt"]]


# ── summaries: the cost contract ──────────────────────────────────────────


async def test_a_summary_is_generated_once_then_served_from_the_store(
    db_session, archive, monkeypatch
):
    user, session = archive
    await _record(db_session, session, _first_question_id())
    mock = AsyncMock(return_value="ילדות על שפת הכנרת.")
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    first = await timeline.build_timeline(db_session, user.id, "he")
    second = await timeline.build_timeline(db_session, user.id, "he")

    assert first["periods"][0]["summary"] == "ילדות על שפת הכנרת."
    assert second["periods"][0]["summary"] == "ילדות על שפת הכנרת."
    # The whole point of the store: the second view costs zero LLM calls.
    assert len(_summary_calls(mock)) == 1


async def test_a_new_recording_makes_the_summary_stale(db_session, archive, monkeypatch):
    user, session = archive
    await _record(db_session, session, _first_question_id())
    mock = AsyncMock(return_value="משפט.")
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    await timeline.build_timeline(db_session, user.id, "he")
    await _record(db_session, session, _first_question_id())
    await timeline.build_timeline(db_session, user.id, "he")

    assert len(_summary_calls(mock)) == 2


async def test_a_deleted_recording_makes_the_summary_stale(db_session, archive, monkeypatch):
    """Deletion changes the watermark too — a sentence must never describe
    footage that is gone."""
    user, session = archive
    keep = await _record(db_session, session, _first_question_id())
    drop = await _record(db_session, session, _first_question_id())
    mock = AsyncMock(return_value="משפט.")
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    await timeline.build_timeline(db_session, user.id, "he")
    await db_session.delete(drop)
    await db_session.flush()
    await timeline.build_timeline(db_session, user.id, "he")

    assert keep is not None
    assert len(_summary_calls(mock)) == 2


async def test_a_language_change_is_staleness(db_session, archive, monkeypatch):
    user, session = archive
    await _record(db_session, session, _first_question_id())
    mock = AsyncMock(return_value="A sentence.")
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    await timeline.build_timeline(db_session, user.id, "he")
    await timeline.build_timeline(db_session, user.id, "en")

    assert len(_summary_calls(mock)) == 2


async def test_a_failure_serves_the_stale_sentence_and_retries_next_read(
    db_session, archive, monkeypatch
):
    """Yesterday's true sentence beats a blank card — and the watermark stays
    unchanged, so the next read tries again rather than accepting it."""
    user, session = archive
    await _record(db_session, session, _first_question_id())
    good = AsyncMock(return_value="הסיפור הישן.")
    monkeypatch.setattr(period_insights.llm_service, "generate_response", good)
    await timeline.build_timeline(db_session, user.id, "he")

    await _record(db_session, session, _first_question_id())
    down = AsyncMock(side_effect=RuntimeError("model down"))
    monkeypatch.setattr(period_insights.llm_service, "generate_response", down)
    stale = await timeline.build_timeline(db_session, user.id, "he")
    assert stale["periods"][0]["summary"] == "הסיפור הישן."

    recovered = AsyncMock(return_value="הסיפור המעודכן.")
    monkeypatch.setattr(period_insights.llm_service, "generate_response", recovered)
    fresh = await timeline.build_timeline(db_session, user.id, "he")
    assert fresh["periods"][0]["summary"] == "הסיפור המעודכן."


async def test_a_failure_with_nothing_stored_shows_no_summary(
    db_session, archive, monkeypatch
):
    user, session = archive
    await _record(db_session, session, _first_question_id())
    monkeypatch.setattr(
        period_insights.llm_service, "generate_response",
        AsyncMock(side_effect=RuntimeError("model down")),
    )

    period = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]
    assert period["summary"] is None
    assert (await db_session.execute(
        PeriodSummary.__table__.select()
    )).all() == []


# ── subtypes: asked once, and 'other' is an answer ────────────────────────


async def _org(db, user, session, name, subtype=None):
    entity = Entity(
        producer_id=user.id, name=name, normalized_name=name,
        type="organisation", subtype=subtype,
    )
    db.add(entity)
    await db.flush()
    segment = await _record(db, session, _first_question_id())
    db.add(EntityMention(entity_id=entity.id, raw_segment_id=segment.id))
    await db.flush()
    return entity


async def test_an_organisation_is_classified_once(db_session, archive, monkeypatch):
    user, session = archive
    org = await _org(db_session, user, session, "הכפר הירוק")

    def dispatch(messages, system_prompt=None, **kwargs):
        if "Classify" in (system_prompt or ""):
            return json.dumps({org.id: "school"})
        return "משפט."

    mock = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    await timeline.build_timeline(db_session, user.id, "he")
    assert org.subtype == "school"
    await timeline.build_timeline(db_session, user.id, "he")
    assert len(_classify_calls(mock)) == 1


async def test_other_is_an_answer_and_is_not_reasked(db_session, archive, monkeypatch):
    user, session = archive
    org = await _org(db_session, user, session, "משהו עמום")

    def dispatch(messages, system_prompt=None, **kwargs):
        if "Classify" in (system_prompt or ""):
            return json.dumps({org.id: "other"})
        return "משפט."

    mock = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    await timeline.build_timeline(db_session, user.id, "he")
    assert org.subtype == "other"
    await timeline.build_timeline(db_session, user.id, "he")
    assert len(_classify_calls(mock)) == 1


async def test_an_invented_label_leaves_null_so_the_next_read_retries(
    db_session, archive, monkeypatch
):
    """Coercing garbage to 'other' would stamp asked-and-unknown on an entity
    the model never actually judged — the stamp is what stops retries, so it
    must only be written for a real answer."""
    user, session = archive
    org = await _org(db_session, user, session, "המפעל")

    def dispatch(messages, system_prompt=None, **kwargs):
        if "Classify" in (system_prompt or ""):
            return json.dumps({org.id: "castle"})
        return "משפט."

    mock = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    await timeline.build_timeline(db_session, user.id, "he")
    assert org.subtype is None
    await timeline.build_timeline(db_session, user.id, "he")
    assert len(_classify_calls(mock)) == 2
