"""Phase 5 — the timeline.

The §3 correction is what most of this covers, and it can no longer be
reproduced against real data: the 16 recordings that produced "0 of 16 placed"
were deleted when the archive was reset, and every surviving recording answers
a live question. So the retired cases are SEEDED rather than observed. That is
the honest option — the alternative is claiming a live check that cannot be
run — but it is worth knowing these assert a shape rather than a measurement.
"""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app import interview_config
from app.models import Entity, EntityMention, InterviewSession, RawSegment, User
from app.services import period_insights, timeline


@pytest.fixture(autouse=True)
def _canned_llm(monkeypatch):
    """build_timeline derives summaries and moment titles via period_insights.

    These tests are about the timeline itself, so the model is canned — its
    real behavior (staleness, storage, watermarks) is covered in
    test_period_insights.py. The canned reply is not JSON, so title parsing
    stores nothing and titles stay NULL, which these tests never read.
    """
    monkeypatch.setattr(
        period_insights.llm_service,
        "generate_response",
        AsyncMock(return_value="משפט סיכום אחד."),
    )


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


async def _record(db, session, question_id, index=0, question_asked="?",
                  video_url=None, importance=None, created_at=None, topic_tags=None):
    segment = RawSegment(
        interview_session_id=session.id, question_asked=question_asked,
        question_index=index, question_id=question_id, status="ready",
        video_url=video_url, importance_score=importance, topic_tags=topic_tags,
        # SQLite's CURRENT_TIMESTAMP is second-resolution, so rows created in
        # one test tie; anything asserting chronology passes explicit times.
        created_at=created_at,
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


async def test_a_period_with_no_named_entities_shows_its_recordings(
    db_session, archive
):
    """The G bug, docs/MEDIA_GALLERY.md §1. A childhood-hobbies answer names
    nobody — correctly — and the period must still show its real, playable
    recording rather than rendering empty."""
    user, session = archive
    _, question_id = _live_ids(1)[0]
    segment = await _record(
        db_session, session, question_id,
        question_asked="מה אהבת לעשות בתור ילד?", video_url="https://cdn/x.mp4",
    )

    period = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]
    assert [r["segment_id"] for r in period["recordings"]] == [segment.id]
    assert period["recordings"][0]["video_url"] == "https://cdn/x.mp4"
    # With the card static, the catch-all bubble is the route in — it must
    # exist and carry this recording even though nothing tagged it.
    moments = period["groups"][0]
    assert moments["key"] == "more"
    assert moments["segment_ids"] == [segment.id]
    assert [h["segment_id"] for h in moments["highlights"]] == [segment.id]
    # Its caption is None — nobody was named, the title carries the content.
    assert moments["highlights"][0]["caption"] is None


async def test_takes_are_numbered_within_their_question(db_session, archive):
    """Three takes of one question are takes 1..3 of 3; a single take of a
    different question in the same period is 1 of 1. `created_at` order alone
    separates takes — the CLAUDE.md rule, there is no take column."""
    user, session = archive
    cat = interview_config.get_categories("he")[0]
    repeated, single = cat["question_ids"][0], cat["question_ids"][1]
    for _ in range(3):
        await _record(db_session, session, repeated)
    await _record(db_session, session, single)

    period = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]
    takes = [r for r in period["recordings"] if r["question_id"] == repeated]
    assert sorted(t["take_index"] for t in takes) == [1, 2, 3]
    assert {t["take_count"] for t in takes} == {3}
    (other,) = [r for r in period["recordings"] if r["question_id"] == single]
    assert (other["take_index"], other["take_count"]) == (1, 1)


async def test_bubbles_partition_the_recordings_no_overlap(db_session, archive):
    """The compact card, docs/MEDIA_GALLERY.md §1.8. Bubbles are REAL tag
    content, and they PARTITION the recordings: a segment carrying both
    משפחה and טבריה must appear under exactly one of them — the same
    recording under two bubbles reads as a duplication bug. Tags are chosen
    greedily by how many still-unassigned recordings they cover."""
    user, session = archive
    _, question_id = _live_ids(1)[0]
    only_family = await _record(db_session, session, question_id,
                                topic_tags=["משפחה"])
    both = await _record(db_session, session, question_id,
                         topic_tags=["משפחה", "טבריה"])
    tveria_a = await _record(db_session, session, question_id,
                             topic_tags=["טבריה"])
    tveria_b = await _record(db_session, session, question_id,
                             topic_tags=["טבריה"])

    groups = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]["groups"]
    # טבריה covers 3 unassigned, so it claims `both`; משפחה keeps only what
    # is left. Counts are exclusive and sum to the period's recordings.
    assert [g["label"] for g in groups] == ["טבריה", "משפחה"]
    assert groups[0]["segment_ids"] == [both.id, tveria_a.id, tveria_b.id]
    assert groups[1]["segment_ids"] == [only_family.id]
    assert [g["count"] for g in groups] == [3, 1]
    seen = [sid for g in groups for sid in g["segment_ids"]]
    assert len(seen) == len(set(seen))


async def test_bubbles_are_capped_and_a_covered_tag_is_not_repeated(
    db_session, archive
):
    """Cap at five, and a tag whose recordings were all claimed by an
    earlier bubble never renders as an empty echo — measured on the live
    archive 43 of 47 distinct tags appear once, and a bubble per one-off is
    the density bug in bubble form."""
    user, session = archive
    _, question_id = _live_ids(1)[0]
    await _record(db_session, session, question_id,
                  topic_tags=["טבריה", "ילדות", "משחקי חצר"])
    await _record(db_session, session, question_id,
                  topic_tags=["טבריה", "ילדות", "בתי ספר"])
    await _record(db_session, session, question_id,
                  topic_tags=["טבריה", "אוכל משפחתי", "סבתא", "ארוחת שישי"])

    groups = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]["groups"]
    # טבריה claims all three recordings; every other tag then covers nothing
    # unassigned, so ONE bubble renders — not seven, and not empty echoes.
    assert [g["label"] for g in groups] == ["טבריה"]
    assert groups[0]["count"] == 3


async def test_an_untagged_period_falls_back_to_a_generic_moments_bubble(
    db_session, archive
):
    """No tags yet (mid-processing, or a pre-topics archive) must not mean no
    way in — the card is static, so a bubble must always exist. As the ONLY
    bubble it is labelled רגעים, not עוד — "more" than nothing reads wrong."""
    user, session = archive
    _, question_id = _live_ids(1)[0]
    segment = await _record(db_session, session, question_id)

    groups = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]["groups"]
    assert [g["key"] for g in groups] == ["more"]
    assert groups[0]["label"] == "רגעים"
    assert groups[0]["segment_ids"] == [segment.id]


async def test_leftovers_land_in_a_catch_all_bubble_nothing_is_stranded(
    db_session, archive
):
    """Bubbles are the SOLE route into a period (the period-wide "all
    moments" screen is gone), so the partition must be TOTAL: recordings
    whose tags lost the cap, and untagged recordings, land in עוד — and
    every recording belongs to exactly one bubble."""
    user, session = archive
    _, question_id = _live_ids(1)[0]
    winners = [
        await _record(db_session, session, question_id, topic_tags=[f"תג{i}"])
        for i in range(5)
    ]
    capped_out = await _record(db_session, session, question_id, topic_tags=["תג נדיר"])
    untagged = await _record(db_session, session, question_id)

    groups = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]["groups"]
    assert [g["label"] for g in groups] == ["תג0", "תג1", "תג2", "תג3", "תג4", "עוד"]
    assert groups[-1]["key"] == "more"
    assert groups[-1]["segment_ids"] == [capped_out.id, untagged.id]
    # The invariant the whole model rests on: exactly one bubble per
    # recording, none stranded, none duplicated.
    seen = [sid for g in groups for sid in g["segment_ids"]]
    assert sorted(seen) == sorted(s.id for s in winners + [capped_out, untagged])
    assert len(seen) == len(set(seen))


async def test_highlights_are_capped_diversified_and_chronological(
    db_session, archive
):
    """The view one click deep has a constant shape: capped at 4, ranked by
    the stored importance score, but diversified across QUESTIONS — three
    takes of one answer are near-duplicates, so the only take of another
    question beats a higher-scored repeat. Presentation is chronological:
    the cap decides WHAT is shown, recording order decides where."""
    user, session = archive
    cat = interview_config.get_categories("he")[0]
    repeated, other = cat["question_ids"][0], cat["question_ids"][1]
    takes = [
        await _record(db_session, session, repeated, importance=9 - i,
                      created_at=datetime(2026, 1, 1 + i))
        for i in range(5)
    ]
    other_take = await _record(db_session, session, other, importance=1,
                               created_at=datetime(2026, 1, 10))

    groups = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]["groups"]
    picked = [h["segment_id"] for h in groups[0]["highlights"]]
    # Four slots: the top-ranked takes of the repeated question, plus the
    # other question's only take despite its low score — chronologically.
    assert picked == [takes[0].id, takes[1].id, takes[2].id, other_take.id]


async def test_highlight_captions_quote_what_the_recording_said(db_session, archive):
    user, session = archive
    _, question_id = _live_ids(1)[0]
    segment = await _record(db_session, session, question_id)
    brother = Entity(producer_id=user.id, name="ניר", normalized_name="ניר", type="person")
    db_session.add(brother)
    await db_session.flush()
    db_session.add(
        EntityMention(entity_id=brother.id, raw_segment_id=segment.id,
                      summary="אח של הדובר, הקטן מבין הארבעה")
    )
    await db_session.flush()

    groups = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]["groups"]
    assert groups[0]["highlights"][0]["caption"] == "אח של הדובר, הקטן מבין הארבעה"


async def test_every_period_carries_its_summary_sentence(db_session, archive):
    """Wiring only — the store/staleness rules live in test_period_insights."""
    user, session = archive
    _, question_id = _live_ids(1)[0]
    await _record(db_session, session, question_id)

    period = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]
    assert period["summary"] == "משפט סיכום אחד."


async def test_captions_quote_the_most_mentioned_person(db_session, archive):
    """The payload carries no people list any more (§1.9) — what remains of
    the mention data is the caption rule: a highlight quotes what the
    recording said about its MOST-mentioned person, so the mentions-desc
    ordering inside _people_in is still load-bearing."""
    user, session = archive
    _, question_id = _live_ids(1)[0]
    one = await _record(db_session, session, question_id)
    two = await _record(db_session, session, question_id)

    rare = Entity(producer_id=user.id, name="Rare", normalized_name="rare", type="person")
    often = Entity(producer_id=user.id, name="Often", normalized_name="often", type="person")
    db_session.add_all([rare, often])
    await db_session.flush()
    db_session.add_all([
        EntityMention(entity_id=rare.id, raw_segment_id=one.id, summary="הנדיר"),
        EntityMention(entity_id=often.id, raw_segment_id=one.id, summary="השכיח"),
        EntityMention(entity_id=often.id, raw_segment_id=two.id),
    ])
    await db_session.flush()

    period = (await timeline.build_timeline(db_session, user.id, "he"))["periods"][0]
    assert "people" not in period
    captions = {h["segment_id"]: h["caption"] for h in period["groups"][0]["highlights"]}
    assert captions[one.id] == "השכיח"
