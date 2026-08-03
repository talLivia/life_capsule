"""Phase 5 — the timeline.

The §3 correction is what most of this covers, and it can no longer be
reproduced against real data: the 16 recordings that produced "0 of 16 placed"
were deleted when the archive was reset, and every surviving recording answers
a live question. So the retired cases are SEEDED rather than observed. That is
the honest option — the alternative is claiming a live check that cannot be
run — but it is worth knowing these assert a shape rather than a measurement.
"""

import pytest

from app import interview_config
from app.models import Entity, EntityMention, InterviewSession, RawSegment, User
from app.services import timeline


@pytest.fixture
async def archive(db_session):
    user = User(
        id="u-tl", email="tl@example.com", username="tl",
        hashed_password="x", role="producer", recording_language="he",
    )
    db_session.add(user)
    await db_session.flush()
    session = InterviewSession(user_id=user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    return user, session


async def _record(db, session, question_id, index=0):
    segment = RawSegment(
        interview_session_id=session.id, question_asked="?",
        question_index=index, question_id=question_id, status="ready",
    )
    db.add(segment)
    await db.flush()
    return segment


def _live_ids(n=2):
    cats = interview_config.get_categories("he")
    return [(c["category"], c["question_ids"][0]) for c in cats[:n]]


# ── ordering and shape ────────────────────────────────────────────────────


async def test_periods_follow_the_files_order_not_the_recording_order(
    db_session, archive
):
    """The file's order IS the chronology. Recording out of order must not
    reorder a life."""
    user, session = archive
    (first_cat, first_q), (second_cat, second_q) = _live_ids(2)
    await _record(db_session, session, second_q, 1)   # recorded FIRST
    await _record(db_session, session, first_q, 0)

    result = await timeline.build_timeline(db_session, user.id, "he")
    assert [p["category"] for p in result["periods"]] == [first_cat, second_cat]


async def test_empty_periods_are_hidden_and_counted(db_session, archive):
    """Sixteen bubbles with eleven empty reads as a broken page. A producer
    who sees one bubble should still know how much of the interview is left."""
    user, session = archive
    _, question_id = _live_ids(1)[0]
    await _record(db_session, session, question_id)

    result = await timeline.build_timeline(db_session, user.id, "he")
    assert len(result["periods"]) == 1
    # Against every bucket, not every live category — the file carries
    # retired entries of its own, so the two counts differ.
    assert result["hidden_empty_periods"] == len(timeline._buckets("he")) - 1


async def test_takes_of_one_question_are_one_answered_question(db_session, archive):
    """Three takes is one question answered — the same rule /record uses."""
    user, session = archive
    _, question_id = _live_ids(1)[0]
    for _ in range(3):
        await _record(db_session, session, question_id)

    period = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]
    assert period["recording_count"] == 3
    assert period["question_count"] == 1


async def test_a_recording_with_no_question_id_is_counted_not_dropped(
    db_session, archive
):
    """Made before question_id existed, or uploaded outside the guided set.
    It belongs to no period, and saying so beats vanishing."""
    user, session = archive
    _, question_id = _live_ids(1)[0]
    await _record(db_session, session, question_id)
    await _record(db_session, session, None)

    result = await timeline.build_timeline(db_session, user.id, "he")
    assert result["unplaced_recordings"] == 1
    assert sum(p["recording_count"] for p in result["periods"]) == 1


# ── the §3 correction, seeded ─────────────────────────────────────────────


async def test_a_retired_question_still_lands_in_its_category(
    db_session, archive, monkeypatch
):
    """Cause 1. `question_ids` is live-only, so matching on it alone drops
    every recording of a withdrawn question — measured as 0 of 16."""
    user, session = archive
    live_cat, _ = _live_ids(1)[0]
    monkeypatch.setattr(
        interview_config, "get_retired",
        lambda: [{"id": "gone_q1", "category": live_cat, "text": "old"}],
    )
    await _record(db_session, session, "gone_q1")

    result = await timeline.build_timeline(db_session, user.id, "he")
    assert [p["category"] for p in result["periods"]] == [live_cat]
    assert result["periods"][0]["recording_count"] == 1
    assert result["periods"][0]["retired_only"] is False


async def test_a_retired_only_category_is_appended_after_the_live_ones(
    db_session, archive, monkeypatch
):
    """Cause 2. A category with no equivalent in the new set is never yielded
    at all, so its recordings have nowhere to appear even once cause 1 is
    fixed. It has no position in the new chronology, so it goes last."""
    user, session = archive
    live_cat, live_q = _live_ids(1)[0]
    monkeypatch.setattr(
        interview_config, "get_retired",
        lambda: [{"id": "pm_q1", "category": "post_military",
                  "category_label": "אחרי הצבא", "text": "old"}],
    )
    await _record(db_session, session, live_q)
    await _record(db_session, session, "pm_q1")

    periods = (await timeline.build_timeline(db_session, user.id, "he"))["periods"]
    assert [p["category"] for p in periods] == [live_cat, "post_military"]
    assert periods[-1]["retired_only"] is True
    # Its own label, not the raw key — a snake_case bucket beside Hebrew ones
    # is a bug on a page whose job is being legible to a family.
    assert periods[-1]["category_label"] == "אחרי הצבא"


async def test_live_question_ids_never_gain_retired_ones(monkeypatch):
    """The field must keep meaning one thing. Folding retired ids into
    `question_ids` would let a withdrawn question be offered to a producer."""
    monkeypatch.setattr(
        interview_config, "get_retired",
        lambda: [{"id": "gone_q1", "category": interview_config.get_categories("he")[0]["category"]}],
    )
    buckets = timeline._buckets("he")
    assert "gone_q1" in buckets[0]["retired_question_ids"]
    assert "gone_q1" not in buckets[0]["question_ids"]


# ── sub-bubbles ───────────────────────────────────────────────────────────


async def test_people_are_listed_by_how_often_this_period_mentions_them(
    db_session, archive
):
    user, session = archive
    _, question_id = _live_ids(1)[0]
    one = await _record(db_session, session, question_id)
    two = await _record(db_session, session, question_id)

    rare = Entity(producer_id=user.id, name="Rare", normalized_name="rare", type="person")
    often = Entity(producer_id=user.id, name="Often", normalized_name="often",
                   type="person", year_start=1948)
    db_session.add_all([rare, often])
    await db_session.flush()
    db_session.add_all([
        EntityMention(entity_id=rare.id, raw_segment_id=one.id),
        EntityMention(entity_id=often.id, raw_segment_id=one.id),
        EntityMention(entity_id=often.id, raw_segment_id=two.id),
    ])
    await db_session.flush()

    people = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]["people"]
    assert [p["name"] for p in people] == ["Often", "Rare"]
    # A year decorates; it never orders. See the module header.
    assert people[0]["year_start"] == 1948 and people[1]["year_start"] is None


async def test_someone_in_two_periods_appears_in_both(db_session, archive):
    """A person is not owned by one life stage, and that needs no special
    case — it falls out of matching per period."""
    user, session = archive
    (cat_a, q_a), (cat_b, q_b) = _live_ids(2)
    first = await _record(db_session, session, q_a)
    second = await _record(db_session, session, q_b, 1)
    person = Entity(producer_id=user.id, name="Both", normalized_name="both", type="person")
    db_session.add(person)
    await db_session.flush()
    db_session.add_all([
        EntityMention(entity_id=person.id, raw_segment_id=first.id),
        EntityMention(entity_id=person.id, raw_segment_id=second.id),
    ])
    await db_session.flush()

    periods = (await timeline.build_timeline(db_session, user.id, "he"))["periods"]
    assert [p["people"][0]["name"] for p in periods] == ["Both", "Both"]
    assert [p["category"] for p in periods] == [cat_a, cat_b]
