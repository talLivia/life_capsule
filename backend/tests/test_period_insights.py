"""Derived timeline display data — docs/MEDIA_GALLERY.md §1.6–§1.8.

What matters here is the COST CONTRACT, not the sentences: a summary is
generated when the recordings behind a category change and never otherwise;
a title is generated when ITS OWN transcript (or the language) changes and
never otherwise; a failure serves the stale text rather than a blank.
"""

import json
from unittest.mock import AsyncMock

import pytest

from app import interview_config
from app.models import InterviewSession, PeriodSummary, RawSegment, User
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


def _title_calls(mock):
    return [c for c in mock.call_args_list if "title" in c.kwargs["system_prompt"]]


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


# ── moment titles: written once, retried on garbage ───────────────────────


async def test_a_moment_is_titled_once_and_the_payload_carries_it(
    db_session, archive, monkeypatch
):
    user, session = archive
    segment = await _record(db_session, session, _first_question_id())

    def dispatch(messages, system_prompt=None, **kwargs):
        if "title" in (system_prompt or ""):
            return json.dumps({segment.id: "הבית הראשון בטבריה"})
        return "משפט."

    mock = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    first = await timeline.build_timeline(db_session, user.id, "he")
    assert first["periods"][0]["recordings"][0]["title"] == "הבית הראשון בטבריה"
    await timeline.build_timeline(db_session, user.id, "he")
    # Written once, ever — the transcript never changes after ingest.
    assert len(_title_calls(mock)) == 1


async def test_an_unparseable_title_reply_leaves_null_and_retries(
    db_session, archive, monkeypatch
):
    user, session = archive
    await _record(db_session, session, _first_question_id())

    def dispatch(messages, system_prompt=None, **kwargs):
        if "title" in (system_prompt or ""):
            return "not json at all"
        return "משפט."

    mock = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    period = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]
    assert period["recordings"][0]["title"] is None
    await timeline.build_timeline(db_session, user.id, "he")
    assert len(_title_calls(mock)) == 2


async def test_an_untranscribed_moment_is_not_sent_for_titling(
    db_session, archive, monkeypatch
):
    """Nothing to title yet — it is picked up once the transcript lands."""
    user, session = archive
    await _record(db_session, session, _first_question_id(), transcript=None)
    mock = AsyncMock(return_value="משפט.")
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    await timeline.build_timeline(db_session, user.id, "he")
    assert _title_calls(mock) == []


async def test_a_changed_transcript_regenerates_the_title(
    db_session, archive, monkeypatch
):
    """The watermark is the transcript itself (migration 0024): a title must
    not outlive the words it named. An in-place re-analysis is the only way
    a recording's content changes — new and re-recorded takes are new rows —
    and nothing regenerates when nothing changed."""
    user, session = archive
    segment = await _record(db_session, session, _first_question_id())

    def dispatch(messages, system_prompt=None, **kwargs):
        if "title" in (system_prompt or ""):
            return json.dumps({segment.id: "כותרת"})
        return "משפט."

    mock = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    await timeline.build_timeline(db_session, user.id, "he")
    await timeline.build_timeline(db_session, user.id, "he")
    assert len(_title_calls(mock)) == 1

    segment.transcript = "תמלול חדש לגמרי אחרי ניתוח מחדש."
    await db_session.flush()
    await timeline.build_timeline(db_session, user.id, "he")
    assert len(_title_calls(mock)) == 2
    await timeline.build_timeline(db_session, user.id, "he")
    assert len(_title_calls(mock)) == 2
