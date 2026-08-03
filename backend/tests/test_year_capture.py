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


def test_every_classifiable_type_can_carry_a_year():
    """A person has a birth year, a place a year you moved there, an
    organisation a year you joined it."""
    extracted = [
        {"name": "המלחמה", "type": "event"},
        {"name": "ניר", "type": "person"},
        {"name": "טבריה", "type": "place"},
        {"name": "חיל האוויר", "type": "organisation"},
    ]
    asked = {q["name"] for q in year_questions(extracted)}
    assert asked == {"המלחמה", "ניר", "טבריה", "חיל האוויר"}


def test_other_is_excluded():
    """`other` is the fallback for a name the extractor could not classify.
    Asking for the year of something we do not understand is noise on a screen
    whose value is only asking what genuinely needs an answer."""
    assert year_questions([{"name": "???", "type": "other"}]) == []
    assert "other" not in YEAR_QUESTION_TYPES


def test_an_entity_that_already_has_a_year_is_not_asked_again():
    """A question whose answer would be discarded is worse than no question —
    the lesson from the type answers that were silently dropped."""
    assert year_questions([{"name": "x", "type": "event", "year_start": 1973}]) == []
    assert len(year_questions([{"name": "x", "type": "event", "year_start": None}])) == 1


def test_a_name_already_asked_about_is_never_asked_again():
    """Skipping is a real answer — "I do not know" — and re-asking would
    ignore it. Without this, widening past `event` would put the same
    questions up on every recording until the producer clicked past the whole
    screen."""
    extracted = [{"name": "ניר", "type": "person"}]
    assert len(year_questions(extracted, settled=set())) == 1
    assert year_questions(extracted, settled={"ניר"}) == []


def test_settled_matching_uses_the_merge_key_not_the_raw_string():
    """A trailing space must not resurrect a question already answered."""
    assert year_questions([{"name": " ניר ", "type": "person"}], settled={"ניר"}) == []


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


# ── ask once, ever ────────────────────────────────────────────────────────


async def test_being_asked_is_recorded_even_when_skipped(db_session, segment):
    """The whole mechanism. Skipping leaves year_start NULL, so without a
    separate stamp the next recording could not tell "they said they don't
    know" from "nobody has asked" — and would ask again."""
    user, seg = segment
    await entity_store.write_segment_entities(
        db_session, segment_id=seg.id, producer_id=user.id,
        entities=[ExtractedEntity(name="ניר", type="person", year_asked=True)],
    )
    ent = (
        await db_session.execute(select(Entity).where(Entity.name == "ניר"))
    ).scalars().one()
    assert ent.year_start is None, "skipping stores no year"
    assert ent.year_asked_at is not None, "but it does record that we asked"


async def test_a_skipped_entity_is_never_offered_again(db_session, segment):
    """End to end: ask, skip, and the next recording mentioning the same
    person raises no year question at all."""
    user, seg = segment
    await entity_store.write_segment_entities(
        db_session, segment_id=seg.id, producer_id=user.id,
        entities=[ExtractedEntity(name="ניר", type="person", year_asked=True)],
    )

    settled = await entity_store.names_with_year_settled(db_session, user.id, ["ניר"])
    assert year_questions([{"name": "ניר", "type": "person"}], settled) == []


async def test_an_answered_entity_is_also_never_offered_again(db_session, segment):
    user, seg = segment
    await entity_store.write_segment_entities(
        db_session, segment_id=seg.id, producer_id=user.id,
        entities=[
            ExtractedEntity(name="ניר", type="person", year_start=1950, year_asked=True)
        ],
    )
    settled = await entity_store.names_with_year_settled(db_session, user.id, ["ניר"])
    assert settled == {"ניר"}
    assert year_questions([{"name": "ניר", "type": "person"}], settled) == []


async def test_an_untouched_entity_is_still_offered(db_session, segment):
    """Entities that predate the feature have year_asked_at NULL, so each gets
    exactly one offer the next time it is mentioned — not excluded forever."""
    user, seg = segment
    await entity_store.write_segment_entities(
        db_session, segment_id=seg.id, producer_id=user.id,
        entities=[ExtractedEntity(name="ניר", type="person")],  # never asked
    )
    settled = await entity_store.names_with_year_settled(db_session, user.id, ["ניר"])
    assert settled == set()
    assert len(year_questions([{"name": "ניר", "type": "person"}], settled)) == 1


async def test_the_asked_stamp_is_not_moved_by_a_later_recording(db_session, segment):
    """Set once. Re-stamping would be harmless today but makes "when were they
    asked" a lie."""
    user, seg = segment
    await entity_store.write_segment_entities(
        db_session, segment_id=seg.id, producer_id=user.id,
        entities=[ExtractedEntity(name="ניר", type="person", year_asked=True)],
    )
    first = (
        await db_session.execute(select(Entity).where(Entity.name == "ניר"))
    ).scalars().one().year_asked_at

    await entity_store.write_segment_entities(
        db_session, segment_id=seg.id, producer_id=user.id,
        entities=[ExtractedEntity(name="ניר", type="person", year_asked=True)],
    )
    again = (
        await db_session.execute(select(Entity).where(Entity.name == "ניר"))
    ).scalars().one().year_asked_at
    assert first == again


async def test_settled_lookup_is_scoped_to_one_producer(db_session, segment):
    """Another producer's answer must not silence this producer's question."""
    from app.models import User

    user, seg = segment
    other = User(
        id="u-other", email="o@example.com", username="other",
        hashed_password="x", role="producer",
    )
    db_session.add(other)
    await db_session.flush()

    await entity_store.write_segment_entities(
        db_session, segment_id=seg.id, producer_id=other.id,
        entities=[ExtractedEntity(name="ניר", type="person", year_asked=True)],
    )
    assert await entity_store.names_with_year_settled(db_session, user.id, ["ניר"]) == set()
    assert await entity_store.names_with_year_settled(db_session, other.id, ["ניר"]) == {"ניר"}
