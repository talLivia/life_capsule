"""Tests for the entity write path.

The cases here are the ones the migration plan says must hold: מונטריאול
mentioned by two recordings is ONE entity with TWO mentions, re-ingest
replaces rather than appends, deleting a recording leaves shared entities
alone, and the self-entity — which has no mentions by construction — is never
swept up as an orphan.
"""

import pytest
from sqlalchemy import select

from app.models import Entity, EntityMention, InterviewSession, RawSegment
from app.services.entity_extraction import ExtractedEntity
from app.services.entity_store import delete_orphaned_entities, write_segment_entities

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session_row(db_session, test_user):
    session = InterviewSession(user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    return session


def _make_segment(session, index):
    return RawSegment(
        interview_session_id=session.id,
        question_asked=f"Question {index}",
        question_index=index,
        video_key=f"segments/{session.id}/{index}/take.webm",
        transcript=f"transcript {index}",
        status="pending_analysis",
    )


@pytest.fixture
async def segments(db_session, session_row):
    """Two recordings — most of what matters here is cross-recording."""
    rows = [_make_segment(session_row, 0), _make_segment(session_row, 1)]
    db_session.add_all(rows)
    await db_session.commit()
    for row in rows:
        await db_session.refresh(row)
    return rows


async def _entities(db, producer_id):
    return list(
        (
            await db.execute(
                select(Entity)
                .where(Entity.producer_id == producer_id)
                .order_by(Entity.name)
            )
        )
        .scalars()
        .all()
    )


async def _mentions(db, entity_id):
    return list(
        (
            await db.execute(
                select(EntityMention).where(EntityMention.entity_id == entity_id)
            )
        )
        .scalars()
        .all()
    )


# ── The merge rule ──────────────────────────────────────────────────────────


async def test_one_entity_two_mentions_across_recordings(db_session, test_user, segments):
    """THE case the plan calls out by name. Two rows here would each look like
    they had a single mention, and the deletion safety check would then decide
    neither was shared and delete both."""
    for segment, summary in zip(segments, ["איפה שגר", "לאן עבר אחרי הצבא"]):
        await write_segment_entities(
            db_session,
            segment_id=segment.id,
            producer_id=test_user.id,
            entities=[ExtractedEntity(name="מונטריאול", type="place", summary=summary)],
        )
    await db_session.commit()

    entities = await _entities(db_session, test_user.id)
    assert len(entities) == 1
    mentions = await _mentions(db_session, entities[0].id)
    assert len(mentions) == 2
    assert {m.summary for m in mentions} == {"איפה שגר", "לאן עבר אחרי הצבא"}
    assert {m.raw_segment_id for m in mentions} == {s.id for s in segments}


async def test_merge_uses_the_normalised_key_not_the_raw_string(
    db_session, test_user, segments
):
    """Final-letter forms are the same letter in end position, so these are
    one entity — decided by entity_names, not by string equality here."""
    await write_segment_entities(
        db_session,
        segment_id=segments[0].id,
        producer_id=test_user.id,
        entities=[ExtractedEntity(name="ירושלים", type="place")],
    )
    await write_segment_entities(
        db_session,
        segment_id=segments[1].id,
        producer_id=test_user.id,
        entities=[ExtractedEntity(name="ירושלימ", type="place")],
    )
    await db_session.commit()

    entities = await _entities(db_session, test_user.id)
    assert len(entities) == 1
    # The FIRST spelling is kept — `name` is what the producer actually said.
    assert entities[0].name == "ירושלים"


async def test_genuinely_different_names_stay_separate(db_session, test_user, segments):
    await write_segment_entities(
        db_session,
        segment_id=segments[0].id,
        producer_id=test_user.id,
        entities=[
            ExtractedEntity(name="טל", type="person"),
            ExtractedEntity(name="תל", type="place"),
        ],
    )
    await db_session.commit()
    assert len(await _entities(db_session, test_user.id)) == 2


async def test_entities_are_scoped_per_producer(db_session, test_user, segments):
    """Two producers can each have a מונטריאול; they are not the same row."""
    from app.api.v1.users import get_password_hash
    from app.models import User

    other = User(
        email="other@example.com",
        username="other",
        hashed_password=get_password_hash("x" * 12),
    )
    db_session.add(other)
    await db_session.commit()

    for producer_id in (test_user.id, other.id):
        await write_segment_entities(
            db_session,
            segment_id=segments[0].id,
            producer_id=producer_id,
            entities=[ExtractedEntity(name="מונטריאול", type="place")],
        )
    await db_session.commit()

    assert len(await _entities(db_session, test_user.id)) == 1
    assert len(await _entities(db_session, other.id)) == 1


async def test_a_name_that_normalises_to_nothing_is_skipped(
    db_session, test_user, segments
):
    """Every blank name collides on the merge key, so one blank extraction
    would become the row every later blank extraction merged onto."""
    result = await write_segment_entities(
        db_session,
        segment_id=segments[0].id,
        producer_id=test_user.id,
        entities=[ExtractedEntity(name="   ", type="person")],
    )
    await db_session.commit()
    assert result.entities_created == 0
    assert await _entities(db_session, test_user.id) == []


# ── Re-ingest ───────────────────────────────────────────────────────────────


async def test_reingest_replaces_this_segments_mentions(db_session, test_user, segments):
    """Re-analysing must not double the mention count — otherwise "how many
    recordings mention this" silently becomes "how many times we ran the
    pipeline"."""
    entities = [ExtractedEntity(name="גילה", type="person", summary="סבתא של הדובר")]
    await write_segment_entities(
        db_session, segment_id=segments[0].id, producer_id=test_user.id, entities=entities
    )
    await write_segment_entities(
        db_session, segment_id=segments[0].id, producer_id=test_user.id, entities=entities
    )
    await db_session.commit()

    rows = await _entities(db_session, test_user.id)
    assert len(rows) == 1
    assert len(await _mentions(db_session, rows[0].id)) == 1


async def test_reingest_updates_the_summary_by_replacing_the_row(
    db_session, test_user, segments
):
    """A summary is never rewritten in place — the old row goes and a new one
    arrives, which is what makes a summary unable to go stale relative to the
    recording it describes."""
    await write_segment_entities(
        db_session,
        segment_id=segments[0].id,
        producer_id=test_user.id,
        entities=[ExtractedEntity(name="גילה", type="person", summary="שכנה")],
    )
    await write_segment_entities(
        db_session,
        segment_id=segments[0].id,
        producer_id=test_user.id,
        entities=[ExtractedEntity(name="גילה", type="person", summary="סבתא של הדובר")],
    )
    await db_session.commit()

    rows = await _entities(db_session, test_user.id)
    mentions = await _mentions(db_session, rows[0].id)
    assert [m.summary for m in mentions] == ["סבתא של הדובר"]


async def test_reingest_that_finds_nobody_leaves_nobody_behind(
    db_session, test_user, segments
):
    await write_segment_entities(
        db_session,
        segment_id=segments[0].id,
        producer_id=test_user.id,
        entities=[ExtractedEntity(name="גילה", type="person")],
    )
    await write_segment_entities(
        db_session, segment_id=segments[0].id, producer_id=test_user.id, entities=[]
    )
    await db_session.commit()

    assert await _entities(db_session, test_user.id) == []


async def test_reingest_does_not_touch_a_sibling_recordings_mentions(
    db_session, test_user, segments
):
    """Takes of one question are separate recordings. Re-analysing one must
    not disturb another — a take is only ever destroyed explicitly."""
    for segment in segments:
        await write_segment_entities(
            db_session,
            segment_id=segment.id,
            producer_id=test_user.id,
            entities=[ExtractedEntity(name="מונטריאול", type="place")],
        )
    await write_segment_entities(
        db_session, segment_id=segments[0].id, producer_id=test_user.id, entities=[]
    )
    await db_session.commit()

    rows = await _entities(db_session, test_user.id)
    assert len(rows) == 1
    mentions = await _mentions(db_session, rows[0].id)
    assert [m.raw_segment_id for m in mentions] == [segments[1].id]


# ── Types ───────────────────────────────────────────────────────────────────


async def test_an_unclassified_type_is_upgraded_by_a_later_recording(
    db_session, test_user, segments
):
    """'other' is the fallback for an extraction that could not classify, so
    a later recording that DOES classify is strictly more information."""
    await write_segment_entities(
        db_session,
        segment_id=segments[0].id,
        producer_id=test_user.id,
        entities=[ExtractedEntity(name="חיל האוויר", type="other")],
    )
    await write_segment_entities(
        db_session,
        segment_id=segments[1].id,
        producer_id=test_user.id,
        entities=[ExtractedEntity(name="חיל האוויר", type="organisation")],
    )
    await db_session.commit()

    assert (await _entities(db_session, test_user.id))[0].type == "organisation"


async def test_a_real_type_disagreement_keeps_the_first_answer(
    db_session, test_user, segments
):
    """Not resolved by whichever recording was ingested last — that is a
    question for the producer, and settling it silently would make the answer
    depend on ingestion order."""
    await write_segment_entities(
        db_session,
        segment_id=segments[0].id,
        producer_id=test_user.id,
        entities=[ExtractedEntity(name="הכפר הירוק", type="place")],
    )
    await write_segment_entities(
        db_session,
        segment_id=segments[1].id,
        producer_id=test_user.id,
        entities=[ExtractedEntity(name="הכפר הירוק", type="organisation")],
    )
    await db_session.commit()

    assert (await _entities(db_session, test_user.id))[0].type == "place"


async def test_torn_classifications_are_reported_for_confirmation(
    db_session, test_user, segments
):
    result = await write_segment_entities(
        db_session,
        segment_id=segments[0].id,
        producer_id=test_user.id,
        entities=[
            ExtractedEntity(name="ניר", type="person"),
            ExtractedEntity(
                name="הכפר הירוק", type="place", alternative_type="organisation"
            ),
        ],
    )
    await db_session.commit()
    assert [e.name for e in result.needs_confirmation] == ["הכפר הירוק"]


# ── Orphans ─────────────────────────────────────────────────────────────────


async def test_an_entity_another_recording_still_mentions_survives(
    db_session, test_user, segments
):
    """Graphiti's "drop only when the MENTIONS count is 1" rule, now enforced
    by the engine instead of by bookkeeping."""
    for segment in segments:
        await write_segment_entities(
            db_session,
            segment_id=segment.id,
            producer_id=test_user.id,
            entities=[ExtractedEntity(name="מונטריאול", type="place")],
        )
    await write_segment_entities(
        db_session, segment_id=segments[0].id, producer_id=test_user.id, entities=[]
    )
    await db_session.commit()

    assert [e.name for e in await _entities(db_session, test_user.id)] == ["מונטריאול"]


async def test_the_self_entity_is_never_swept_as_an_orphan(
    db_session, test_user, segments
):
    """The producer's own entity has NO mentions by construction — it exists
    so relations have a root ("I have four brothers" needs a node for the
    "I"). An orphan sweep that did not skip it would delete the family tree's
    root on the first re-ingest, invisibly."""
    self_entity = Entity(
        producer_id=test_user.id,
        name="Test User",
        normalized_name="test user",
        type="person",
        is_self=True,
    )
    db_session.add(self_entity)
    await db_session.commit()

    await write_segment_entities(
        db_session, segment_id=segments[0].id, producer_id=test_user.id, entities=[]
    )
    await db_session.commit()

    assert [e.name for e in await _entities(db_session, test_user.id)] == ["Test User"]


async def test_the_sweep_is_scoped_to_one_producer(db_session, test_user, segments):
    """The caller only ever knows that ITS producer's mentions are settled."""
    from app.api.v1.users import get_password_hash
    from app.models import User

    other = User(
        email="other@example.com",
        username="other",
        hashed_password=get_password_hash("x" * 12),
    )
    db_session.add(other)
    await db_session.commit()
    db_session.add(
        Entity(
            producer_id=other.id,
            name="גילה",
            normalized_name="גילה",
            type="person",
        )
    )
    await db_session.commit()

    removed = await delete_orphaned_entities(db_session, test_user.id)
    await db_session.commit()

    assert removed == 0
    assert len(await _entities(db_session, other.id)) == 1


async def test_losing_the_insert_race_re_reads_the_winners_row(
    db_session, test_user, segments, monkeypatch
):
    """Two recordings for one producer can be analysed at the same time and
    both name מונטריאול for the first time. The unique constraint decides;
    the loser must re-read the winner's row, not fail the ingest.

    Simulated by making the pre-insert lookup miss once — which is exactly
    what a concurrent transaction committing between the SELECT and the
    INSERT looks like from here.
    """
    import app.services.entity_store as store

    db_session.add(
        Entity(
            producer_id=test_user.id,
            name="מונטריאול",
            normalized_name="מונטריאול",
            type="place",
        )
    )
    await db_session.flush()

    real_find = store._find_entity
    calls = {"n": 0}

    async def find_missing_once(db, producer_id, normalized):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real_find(db, producer_id, normalized)

    monkeypatch.setattr(store, "_find_entity", find_missing_once)

    result = await store.write_segment_entities(
        db_session,
        segment_id=segments[0].id,
        producer_id=test_user.id,
        entities=[ExtractedEntity(name="מונטריאול", type="place", summary="איפה שגר")],
    )
    await db_session.commit()

    assert calls["n"] == 2  # missed, collided, re-read
    assert (result.entities_created, result.entities_matched) == (0, 1)
    rows = await _entities(db_session, test_user.id)
    assert len(rows) == 1
    assert [m.summary for m in await _mentions(db_session, rows[0].id)] == ["איפה שגר"]


# ── Transaction boundary ────────────────────────────────────────────────────


async def test_the_writer_does_not_commit(db_session, test_user, segments):
    """The caller commits, so the entities and the segment's status='ready'
    land together. A half-written entity set behind a segment marked ready is
    indistinguishable from a recording that mentioned nobody."""
    # Read the ids BEFORE the rollback: it expires every instance in the
    # session, and re-loading one afterwards is sync IO inside an async test.
    producer_id, segment_id = test_user.id, segments[0].id

    await write_segment_entities(
        db_session,
        segment_id=segment_id,
        producer_id=producer_id,
        entities=[ExtractedEntity(name="גילה", type="person")],
    )
    await db_session.rollback()

    assert await _entities(db_session, producer_id) == []


async def test_result_counts_report_what_happened(db_session, test_user, segments):
    first = await write_segment_entities(
        db_session,
        segment_id=segments[0].id,
        producer_id=test_user.id,
        entities=[
            ExtractedEntity(name="גילה", type="person"),
            ExtractedEntity(name="טבריה", type="place"),
        ],
    )
    assert (first.entities_created, first.entities_matched) == (2, 0)
    assert first.mentions_written == 2

    second = await write_segment_entities(
        db_session,
        segment_id=segments[1].id,
        producer_id=test_user.id,
        entities=[ExtractedEntity(name="גילה", type="person")],
    )
    assert (second.entities_created, second.entities_matched) == (0, 1)
    assert second.mentions_written == 1
    # טבריה is still mentioned by the first recording, so nothing is orphaned.
    assert second.orphans_removed == 0
