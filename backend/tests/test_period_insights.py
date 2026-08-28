"""Derived timeline display data — docs/MEDIA_GALLERY.md §1.6–§1.10.

What matters here is the COST CONTRACT, not the sentences: a summary is
generated when the recordings behind a category change and never otherwise;
a failure serves the stale text rather than a blank. Moment titles are no
longer generated here at all — they are written once at save time by
extract_topics_node (§1.10), tested in test_analysis_graph.py.
"""

from unittest.mock import AsyncMock

import pytest

from app import interview_config
from app.models import InterviewSession, PeriodSummary, RawSegment, User
from app.services import period_insights, timeline


@pytest.fixture(autouse=True)
def _inline_refresh(monkeypatch, test_engine):
    """The timeline now serves STORED summaries and refreshes in a
    fire-and-forget task (2026-08-28). For deterministic tests the seam is
    patched to await inline against the test engine, so a load performs its
    refresh before returning — i.e. the OLD timing, with the NEW one-load
    lag captured explicitly where a test cares."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(timeline, "AsyncSessionLocal", factory)

    monkeypatch.setattr(timeline, "_schedule_refresh", lambda coro: _pending.append(coro))
    yield
    _pending.clear()


_pending = []


async def _drain():
    while _pending:
        await _pending.pop(0)


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


# ── summaries: the cost contract ──────────────────────────────────────────


async def test_a_summary_is_generated_once_then_served_from_the_store(
    db_session, archive, monkeypatch
):
    user, session = archive
    await _record(db_session, session, _first_question_id())
    mock = AsyncMock(return_value="ילדות על שפת הכנרת.")
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    first = await timeline.build_timeline(db_session, user.id, "he")
    # Deferred contract (2026-08-28): the load itself never blocks on the
    # LLM — the first view has no sentence yet, the refresh runs after.
    assert first["periods"][0]["summary"] is None
    await _drain()
    second = await timeline.build_timeline(db_session, user.id, "he")
    await _drain()
    assert second["periods"][0]["summary"] == "ילדות על שפת הכנרת."
    # The whole point of the store: later views cost zero LLM calls.
    assert len(_summary_calls(mock)) == 1


async def test_a_new_recording_makes_the_summary_stale(db_session, archive, monkeypatch):
    user, session = archive
    await _record(db_session, session, _first_question_id())
    mock = AsyncMock(return_value="משפט.")
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    await timeline.build_timeline(db_session, user.id, "he")
    await _drain()
    await _record(db_session, session, _first_question_id())
    await timeline.build_timeline(db_session, user.id, "he")
    await _drain()
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
    await _drain()
    await db_session.delete(drop)
    await db_session.flush()
    await timeline.build_timeline(db_session, user.id, "he")
    await _drain()
    assert keep is not None
    assert len(_summary_calls(mock)) == 2


async def test_a_language_change_is_staleness(db_session, archive, monkeypatch):
    user, session = archive
    await _record(db_session, session, _first_question_id())
    mock = AsyncMock(return_value="A sentence.")
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    await timeline.build_timeline(db_session, user.id, "he")
    await _drain()
    await timeline.build_timeline(db_session, user.id, "en")
    await _drain()
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
    await _drain()

    await _record(db_session, session, _first_question_id())
    down = AsyncMock(side_effect=RuntimeError("model down"))
    monkeypatch.setattr(period_insights.llm_service, "generate_response", down)
    stale = await timeline.build_timeline(db_session, user.id, "he")
    await _drain()  # the deferred refresh fails; stored sentence survives
    assert stale["periods"][0]["summary"] == "הסיפור הישן."

    recovered = AsyncMock(return_value="הסיפור המעודכן.")
    monkeypatch.setattr(period_insights.llm_service, "generate_response", recovered)
    await timeline.build_timeline(db_session, user.id, "he")
    await _drain()  # retried and succeeded; the NEXT view shows it
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


async def test_the_timeline_never_generates_titles(db_session, archive, monkeypatch):
    """§1.10: one generation point, at save. The timeline reads the stored
    title as-is — a read must never trigger a title call, even for a
    recording that has none (generation failed at save; the screen falls
    back to the take label)."""
    user, session = archive
    await _record(db_session, session, _first_question_id())
    mock = AsyncMock(return_value="משפט.")
    monkeypatch.setattr(period_insights.llm_service, "generate_response", mock)

    period = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]
    assert period["recordings"][0]["title"] is None
    assert all("title" not in c.kwargs["system_prompt"] for c in mock.call_args_list)
