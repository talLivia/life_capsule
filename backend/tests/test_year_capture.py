"""
Phase 3 of docs/FAMILY_TREE_TIMELINE.md — year capture.

The governing rule is "refuse rather than guess". A wrong year silently
reorders a life on the timeline and nothing about the page looks broken, so
anything needing a judgement call must come back to the producer instead of
being rounded into a number. Most of these feed the parser things it should
REFUSE, because the tempting failure is accepting them.
"""

import pytest
from datetime import date
from sqlalchemy import select

from app.analysis_graph import YEAR_QUESTION_TYPES, year_questions
from app.models import Entity
from app.services import entity_store
from app.services.entity_extraction import ExtractedEntity
from app.services.year_parsing import MIN_YEAR, parse_year

TODAY = date(2026, 8, 3)


# ── parsing ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1973", 1973),
        ("  1973  ", 1973),
        ("in 1973", 1973),
        ("בערך 1973", 1973),
        ("אני חושב שזה היה ב-1968", 1968),
        # Century forced by the calendar, not chosen: 2073 has not happened.
        ("73", 1973),
    ],
)
def test_accepts_an_unambiguous_year(text, expected):
    assert parse_year(text, today=TODAY).year == expected


@pytest.mark.parametrize(
    "text",
    [
        "early 70s",       # a span
        "late 60s",
        "mid 1970s",       # contains a 4-digit year, still a span
        "70s",
        "שנות ה-70",
        "תחילת שנות השישים",
        "1973-1975",       # two years, neither of them chosen
        "20",              # 1920 or 2020 — both real answers
        "when I was young",
        "",
        "   ",
        "1700",            # before MIN_YEAR
        "3000",            # far future
    ],
)
def test_refuses_rather_than_guessing(text):
    result = parse_year(text, today=TODAY)
    assert not result.ok, f"{text!r} should not resolve to a year"
    assert result.reason, "a refusal must say why, or the producer cannot fix it"


def test_a_range_is_refused_even_when_it_contains_a_real_year():
    """The trap: "mid 1970s" holds a perfectly valid 4-digit year. Reading it
    would turn a decade the producer named into a specific year they did not."""
    assert parse_year("mid 1970s", today=TODAY).ok is False
    assert parse_year("1970", today=TODAY).year == 1970


def test_next_year_is_allowed_but_the_far_future_is_not():
    assert parse_year("2027", today=TODAY).year == 2027
    assert not parse_year("2030", today=TODAY).ok


def test_the_lower_bound_is_inclusive():
    assert parse_year(str(MIN_YEAR), today=TODAY).year == MIN_YEAR
    assert not parse_year(str(MIN_YEAR - 1), today=TODAY).ok


# ── which entities are asked ──────────────────────────────────────────────


def test_only_types_that_carry_a_year_are_asked():
    """Asking about every name would train the producer to click through
    without reading — the failure alternative_type exists to avoid."""
    extracted = [
        {"name": "המלחמה", "type": "event"},
        {"name": "ניר", "type": "person"},
        {"name": "טבריה", "type": "place"},
        {"name": "חיל האוויר", "type": "organisation"},
    ]
    asked = {q["name"] for q in year_questions(extracted)}
    assert asked == {"המלחמה"}
    assert YEAR_QUESTION_TYPES == ("event",)


def test_an_entity_that_already_has_a_year_is_not_asked_again():
    """A question whose answer would be discarded is worse than no question —
    the lesson from the type answers that were silently dropped."""
    assert year_questions([{"name": "x", "type": "event", "year_start": 1973}]) == []
    assert len(year_questions([{"name": "x", "type": "event", "year_start": None}])) == 1


# ── writing ───────────────────────────────────────────────────────────────


@pytest.fixture
async def segment(db_session):
    from app.models import InterviewSession, RawSegment, User

    user = User(
        id="u-year", email="y@example.com", username="year",
        hashed_password="x", role="producer",
    )
    db_session.add(user)
    await db_session.flush()
    s = InterviewSession(user_id=user.id, status="active")
    db_session.add(s)
    await db_session.flush()
    seg = RawSegment(
        interview_session_id=s.id, question_asked="q",
        question_index=0, question_id="childhood_q01", status="ready",
    )
    db_session.add(seg)
    await db_session.flush()
    return user, seg


async def test_a_confirmed_year_is_stored(db_session, segment):
    user, seg = segment
    await entity_store.write_segment_entities(
        db_session, segment_id=seg.id, producer_id=user.id,
        entities=[ExtractedEntity(name="המלחמה", type="event", year_start=1973)],
    )
    ent = (
        await db_session.execute(select(Entity).where(Entity.name == "המלחמה"))
    ).scalars().one()
    assert ent.year_start == 1973


async def test_a_later_recording_does_not_overwrite_an_existing_year(db_session, segment):
    """Ingest order must not re-decide a year the producer already gave."""
    user, seg = segment
    await entity_store.write_segment_entities(
        db_session, segment_id=seg.id, producer_id=user.id,
        entities=[ExtractedEntity(name="המלחמה", type="event", year_start=1973)],
    )
    await entity_store.write_segment_entities(
        db_session, segment_id=seg.id, producer_id=user.id,
        entities=[ExtractedEntity(name="המלחמה", type="event", year_start=1967)],
    )
    ent = (
        await db_session.execute(select(Entity).where(Entity.name == "המלחמה"))
    ).scalars().one()
    assert ent.year_start == 1973


async def test_no_year_leaves_the_column_null(db_session, segment):
    """Skipping is a real answer — it must not write anything."""
    user, seg = segment
    await entity_store.write_segment_entities(
        db_session, segment_id=seg.id, producer_id=user.id,
        entities=[ExtractedEntity(name="המלחמה", type="event")],
    )
    ent = (
        await db_session.execute(select(Entity).where(Entity.name == "המלחמה"))
    ).scalars().one()
    assert ent.year_start is None


def test_the_year_survives_the_state_round_trip():
    """It crosses a checkpoint boundary and may sit there for days."""
    e = ExtractedEntity(name="x", type="event", year_start=1973)
    assert ExtractedEntity.from_dict(e.as_dict()).year_start == 1973
    assert ExtractedEntity.from_dict(ExtractedEntity(name="x").as_dict()).year_start is None
