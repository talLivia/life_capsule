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
from app.services.entity_extraction import ExtractedEntity, ExtractedRelation
from app.services import entity_store as es
from app.services.entity_store import delete_orphaned_entities, write_segment_entities
from app.services.entity_names import normalize_entity_name

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


# ── Reads ───────────────────────────────────────────────────────────────────


async def _write(db, producer_id, segment_id, *entities):
    await write_segment_entities(
        db, segment_id=segment_id, producer_id=producer_id, entities=list(entities)
    )
    await db.commit()


async def test_bulk_lookup_returns_one_map_for_many_segments(
    db_session, test_user, segments
):
    """The function the migration was mostly for. The graph had no bulk form,
    so the entity map issued a round trip PER RECORDING — 45% of a turn and
    all of its latency variance."""
    await _write(
        db_session, test_user.id, segments[0].id,
        ExtractedEntity(name="גילה", type="person"),
        ExtractedEntity(name="טבריה", type="place"),
    )
    await _write(
        db_session, test_user.id, segments[1].id,
        ExtractedEntity(name="מונטריאול", type="place"),
    )

    result = await es.get_entity_names_for_segments(
        db_session, [s.id for s in segments], test_user.id
    )
    assert result[segments[0].id] == ["גילה", "טבריה"]
    assert result[segments[1].id] == ["מונטריאול"]


async def test_bulk_lookup_omits_segments_with_no_entities(
    db_session, test_user, segments
):
    """Absent, not mapped to an empty list — callers build maps from what is
    actually here."""
    await _write(db_session, test_user.id, segments[0].id,
                 ExtractedEntity(name="גילה", type="person"))
    result = await es.get_entity_names_for_segments(
        db_session, [s.id for s in segments], test_user.id
    )
    assert segments[1].id not in result


async def test_bulk_lookup_of_nothing_is_not_a_query(db_session, test_user):
    assert await es.get_entity_names_for_segments(db_session, [], test_user.id) == {}


async def test_segment_entities_carry_THIS_recordings_summary(
    db_session, test_user, segments
):
    """The extraction panel's whole job. Under the graph both recordings
    showed the entity's single consolidated summary; now each shows its own."""
    await _write(db_session, test_user.id, segments[0].id,
                 ExtractedEntity(name="מונטריאול", type="place", summary="לשם טס אחרי הצבא"))
    await _write(db_session, test_user.id, segments[1].id,
                 ExtractedEntity(name="מונטריאול", type="place", summary="שם למד תכנות"))

    first = await es.get_segment_entities(db_session, segments[0].id, test_user.id)
    second = await es.get_segment_entities(db_session, segments[1].id, test_user.id)
    assert first == [("מונטריאול", "place", "לשם טס אחרי הצבא")]
    assert second == [("מונטריאול", "place", "שם למד תכנות")]


async def test_find_segments_mentioning_matches_on_the_merge_key(
    db_session, test_user, segments
):
    """A real improvement, not a translation: the graph matched names exactly
    (case-insensitively), so a final-letter or spacing variant missed. Here
    the same key that merged the entity does the lookup."""
    await _write(db_session, test_user.id, segments[0].id,
                 ExtractedEntity(name="ירושלים", type="place"))

    assert await es.find_segments_mentioning(db_session, ["ירושלים"], test_user.id) == [
        segments[0].id
    ]
    # Final-letter variant and stray whitespace both still find it.
    assert await es.find_segments_mentioning(db_session, ["  ירושלימ "], test_user.id) == [
        segments[0].id
    ]


async def test_find_segments_mentioning_honours_exclusions(
    db_session, test_user, segments
):
    for segment in segments:
        await _write(db_session, test_user.id, segment.id,
                     ExtractedEntity(name="מונטריאול", type="place"))

    found = await es.find_segments_mentioning(
        db_session, ["מונטריאול"], test_user.id, exclude_ids=[segments[0].id]
    )
    assert found == [segments[1].id]


async def test_find_segments_mentioning_is_scoped_to_the_producer(
    db_session, test_user, segments
):
    from app.api.v1.users import get_password_hash
    from app.models import User

    other = User(email="o@example.com", username="o",
                 hashed_password=get_password_hash("x" * 12))
    db_session.add(other)
    await db_session.commit()

    await _write(db_session, test_user.id, segments[0].id,
                 ExtractedEntity(name="גילה", type="person"))
    assert await es.find_segments_mentioning(db_session, ["גילה"], other.id) == []


async def test_find_segments_mentioning_with_no_usable_names(db_session, test_user):
    assert await es.find_segments_mentioning(db_session, [], test_user.id) == []
    assert await es.find_segments_mentioning(db_session, ["   "], test_user.id) == []


async def test_scored_counts_distinct_shared_entities_and_ranks_by_it(
    db_session, test_user, segments
):
    """The graph's "score" was already COUNT(DISTINCT entity) ORDER BY count
    DESC — this is the same computation stated directly."""
    await _write(
        db_session, test_user.id, segments[0].id,
        ExtractedEntity(name="גילה", type="person"),
        ExtractedEntity(name="טבריה", type="place"),
    )
    await _write(db_session, test_user.id, segments[1].id,
                 ExtractedEntity(name="גילה", type="person"))

    scored = await es.find_segments_mentioning_scored(
        db_session, ["גילה", "טבריה"], test_user.id
    )
    assert scored[0] == {"segment_id": segments[0].id, "shared_entity_count": 2}
    assert scored[1] == {"segment_id": segments[1].id, "shared_entity_count": 1}


async def test_entity_candidates_rank_the_exact_match_first(
    db_session, test_user, segments
):
    await _write(
        db_session, test_user.id, segments[0].id,
        ExtractedEntity(name="גילה", type="person"),
        ExtractedEntity(name="מונטריאול", type="place"),
    )
    candidates = await es.get_entity_candidates(db_session, "גילה", test_user.id)
    assert candidates[0]["name"] == "גילה"
    assert candidates[0]["uuid"]


async def test_entity_candidates_keep_the_no_floor_contract(
    db_session, test_user, segments
):
    """Deliberately unchanged from the graph version: candidates are RANKED,
    never filtered by a minimum similarity. An unrelated name still comes back
    and the caller's names_are_similar gate is what rejects it — confirmed
    live that this is what pg_trgm does too."""
    from app.services.entity_names import names_are_similar

    await _write(db_session, test_user.id, segments[0].id,
                 ExtractedEntity(name="גילה", type="person"))

    candidates = await es.get_entity_candidates(db_session, "דן כהן", test_user.id)
    assert candidates, "no floor — an unrelated name is still returned"
    assert [c for c in candidates if names_are_similar("דן כהן", c["name"])] == []


async def test_entity_candidates_exclude_the_self_entity(
    db_session, test_user, segments
):
    """The producer is not a disambiguation candidate for names in their own
    transcripts — offering "is this you?" for every name would be noise."""
    db_session.add(
        Entity(producer_id=test_user.id, name="Test User",
               normalized_name="test user", type="person", is_self=True)
    )
    await db_session.commit()
    candidates = await es.get_entity_candidates(db_session, "Test User", test_user.id)
    assert candidates == []


async def test_entity_candidates_carry_a_summary_for_the_question(
    db_session, test_user, segments
):
    """"Which Gila is this" is answered by what was said about her."""
    await _write(db_session, test_user.id, segments[0].id,
                 ExtractedEntity(name="גילה", type="person", summary="סבתא של הדובר"))
    candidates = await es.get_entity_candidates(db_session, "גילה", test_user.id)
    assert candidates[0]["summary"] == "סבתא של הדובר"


async def test_entity_candidates_for_a_blank_name(db_session, test_user):
    assert await es.get_entity_candidates(db_session, "   ", test_user.id) == []


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


# ── The producer is never an entity in their own archive ─────────────────────


async def _self_and_producer(db_session):
    from app.models import User

    user = User(
        id="u-self", email="s@example.com", username="selfy",
        hashed_password="x", role="producer", full_name="Root Person",
    )
    db_session.add(user)
    await db_session.flush()
    me = Entity(
        producer_id=user.id, name="טל", normalized_name=normalize_entity_name("טל"),
        type="person", is_self=True,
    )
    db_session.add(me)
    await db_session.flush()
    return user, me


async def test_the_speaker_is_dropped_from_their_own_extraction(db_session):
    """Seen on the live archive: "אנחנו חמישה: אני טל, עדי…" extracted טל as a
    person, giving the producer a second, disconnected copy of themselves
    beside the is_self row — one that collects relations belonging on the
    tree's root."""
    user, _ = await _self_and_producer(db_session)

    kept, relations = await es.fold_speaker_into_self(
        db_session,
        user.id,
        [
            ExtractedEntity(name="טל", type="person", summary="הדובר"),
            ExtractedEntity(name="עדי", type="person", summary="אחות"),
        ],
        [],
        "__SELF__",
    )
    assert [e.name for e in kept] == ["עדי"], "the producer is not in their own archive"
    assert relations == []


async def test_a_relation_to_the_speaker_survives_as_SELF(db_session):
    """Re-pointed, not dropped with the entity — the relation is real, it just
    names the producer by name instead of by marker."""
    user, _ = await _self_and_producer(db_session)

    kept, relations = await es.fold_speaker_into_self(
        db_session,
        user.id,
        [
            ExtractedEntity(name="טל", type="person", summary="הדובר"),
            ExtractedEntity(name="עדי", type="person", summary="אחות"),
        ],
        [ExtractedRelation(from_name="עדי", to_name="טל", relation_type="sibling")],
        "__SELF__",
    )
    assert [e.name for e in kept] == ["עדי"]
    assert len(relations) == 1
    assert (relations[0].from_name, relations[0].to_name) == ("עדי", "__SELF__")


async def test_a_relation_that_becomes_a_self_loop_is_dropped(db_session):
    """ck_entity_relations_not_self forbids it, and "טל is their own sibling"
    is not a fact worth keeping."""
    user, _ = await _self_and_producer(db_session)

    _, relations = await es.fold_speaker_into_self(
        db_session,
        user.id,
        [ExtractedEntity(name="טל", type="person", summary="הדובר")],
        [ExtractedRelation(from_name="טל", to_name="__SELF__", relation_type="sibling")],
        "__SELF__",
    )
    assert relations == []


async def test_everyone_else_is_untouched(db_session):
    user, _ = await _self_and_producer(db_session)
    entities = [ExtractedEntity(name="עדי", type="person", summary="s")]
    rels = [ExtractedRelation(from_name="עדי", to_name="__SELF__", relation_type="sibling")]

    kept, relations = await es.fold_speaker_into_self(
        db_session, user.id, entities, rels, "__SELF__"
    )
    assert [e.name for e in kept] == ["עדי"] and len(relations) == 1
