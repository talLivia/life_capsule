"""Setting a relation by hand from the family tree.

docs/TREE_EDITING.md. The governing rule: a manual edit always wins — it
REPLACES what contradicts it rather than sitting alongside it. Two edges the
tree cannot both honour would leave it to pick one at render, which is exactly
what made a chosen parent look as though it had failed to save.
"""

import pytest
from sqlalchemy import select

from app.models import (
    Entity,
    EntityRelation,
    InterviewSession,
    RawSegment,
    RelationType,
    User,
)
from app.services import entity_store
from app.services.entity_extraction import ExtractedRelation

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def archive(db_session):
    user = User(
        id="u-tree", email="t@example.com", username="tree",
        hashed_password="x", role="producer", full_name="Root",
    )
    db_session.add(user)
    await db_session.flush()
    for rt, delta, sym, inv in [
        ("parent", -1, False, "child"),
        ("child", 1, False, "parent"),
        ("sibling", 0, True, None),
        ("spouse", 0, True, None),
    ]:
        db_session.add(
            RelationType(
                relation_type=rt, category="family", is_tree_edge=True,
                inverse_type=inv, is_symmetric=sym,
                label_en=rt, label_he=rt, generation_delta=delta,
            )
        )
    session = InterviewSession(user_id=user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    segment = RawSegment(
        interview_session_id=session.id, question_asked="q",
        question_index=0, status="ready",
    )
    db_session.add(segment)
    await db_session.flush()

    async def person(name, **kw):
        entity = Entity(
            producer_id=user.id, name=name, normalized_name=name.lower(),
            type="person", **kw,
        )
        db_session.add(entity)
        await db_session.flush()
        return entity

    people = {
        "root": await person("Root", is_self=True),
        "chen": await person("Chen"),
        "raz": await person("Raz"),
        "tzvi": await person("Tzvi"),
    }

    async def relate(src, rel, dst, origin="recording"):
        row = EntityRelation(
            from_entity_id=src.id, to_entity_id=dst.id, relation_type=rel,
            source_segment_id=segment.id, origin=origin,
        )
        db_session.add(row)
        await db_session.flush()
        return row

    return {"user": user, "segment": segment, "relate": relate, **people}


async def _edges(db, a, b):
    return (
        await db.execute(
            select(EntityRelation).where(
                (
                    (EntityRelation.from_entity_id == a.id)
                    & (EntityRelation.to_entity_id == b.id)
                )
                | (
                    (EntityRelation.from_entity_id == b.id)
                    & (EntityRelation.to_entity_id == a.id)
                )
            )
        )
    ).scalars().all()


async def test_a_manual_edit_leaves_other_pairs_alone(db_session, archive):
    """Chen is the producer's sibling. Saying Chen is Raz's child says nothing
    about the producer, so that edge is not touched — the replacement is
    scoped to the two people named."""
    await archive["relate"](archive["chen"], "sibling", archive["root"])

    result = await entity_store.set_relation_by_hand(
        db_session,
        producer_id=archive["user"].id,
        from_entity_id=archive["raz"].id,
        to_entity_id=archive["chen"].id,
        relation_type="parent",
    )

    assert result["replaced"] == []
    assert len(await _edges(db_session, archive["chen"], archive["root"])) == 1
    edges = await _edges(db_session, archive["raz"], archive["chen"])
    assert len(edges) == 1 and edges[0].relation_type == "parent"


async def test_a_contradicting_edge_between_the_same_pair_is_replaced(db_session, archive):
    """Raz and Chen recorded as siblings; the producer says Raz is Chen's
    parent. Same two people, different generation claim — the old edge goes."""
    await archive["relate"](archive["raz"], "sibling", archive["chen"])

    result = await entity_store.set_relation_by_hand(
        db_session,
        producer_id=archive["user"].id,
        from_entity_id=archive["raz"].id,
        to_entity_id=archive["chen"].id,
        relation_type="parent",
    )

    assert [r["relation_type"] for r in result["replaced"]] == ["sibling"]
    edges = await _edges(db_session, archive["raz"], archive["chen"])
    assert len(edges) == 1 and edges[0].relation_type == "parent"


async def test_an_equivalent_edge_stated_the_other_way_round_is_kept(db_session, archive):
    """Chen-child->Raz and Raz-parent->Chen are the same fact from two ends.
    Replacing a true statement with an equivalent one would throw away the
    recording it came from for nothing."""
    await archive["relate"](archive["chen"], "child", archive["raz"])

    result = await entity_store.set_relation_by_hand(
        db_session,
        producer_id=archive["user"].id,
        from_entity_id=archive["raz"].id,
        to_entity_id=archive["chen"].id,
        relation_type="parent",
    )

    assert result["replaced"] == []
    kinds = sorted(
        e.relation_type for e in await _edges(db_session, archive["raz"], archive["chen"])
    )
    assert kinds == ["child", "parent"]


async def test_a_manual_edge_has_no_recording_and_says_so(db_session, archive):
    await entity_store.set_relation_by_hand(
        db_session,
        producer_id=archive["user"].id,
        from_entity_id=archive["raz"].id,
        to_entity_id=archive["chen"].id,
        relation_type="parent",
    )
    edge = (await _edges(db_session, archive["raz"], archive["chen"]))[0]
    assert edge.source_segment_id is None
    assert edge.origin == entity_store.MANUAL_ORIGIN


async def test_reanalysis_does_not_resurrect_what_a_manual_edit_replaced(db_session, archive):
    """THE guard the plan called not-optional. write_segment_relations deletes
    only origin='recording' rows, so a manual edge survives a re-analysis — and
    without this the loop would re-add the very relation the producer replaced,
    leaving both and putting the contradiction back."""
    await entity_store.set_relation_by_hand(
        db_session,
        producer_id=archive["user"].id,
        from_entity_id=archive["raz"].id,
        to_entity_id=archive["chen"].id,
        relation_type="parent",
    )

    written = await entity_store.write_segment_relations(
        db_session,
        segment_id=archive["segment"].id,
        producer_id=archive["user"].id,
        relations=[
            ExtractedRelation(from_name="Chen", to_name="Raz", relation_type="sibling")
        ],
        self_marker="__SELF__",
    )

    assert written == 0, "the recording must not overrule a deliberate edit"
    kinds = [
        e.relation_type for e in await _edges(db_session, archive["raz"], archive["chen"])
    ]
    assert kinds == ["parent"]


async def test_reanalysis_is_unaffected_for_pairs_with_no_manual_edge(db_session, archive):
    """The guard must be narrow — ordinary re-analysis keeps working."""
    written = await entity_store.write_segment_relations(
        db_session,
        segment_id=archive["segment"].id,
        producer_id=archive["user"].id,
        relations=[
            ExtractedRelation(from_name="Tzvi", to_name="Chen", relation_type="parent")
        ],
        self_marker="__SELF__",
    )
    assert written == 1


async def test_placing_someone_settles_the_ask_once_questions(db_session, archive):
    """Otherwise the next recording asks where this person belongs, and
    answering would contradict the edit just made."""
    assert archive["chen"].parentage_asked_at is None

    await entity_store.mark_placement_asked(
        db_session, [archive["raz"].id, archive["chen"].id]
    )

    assert archive["chen"].parentage_asked_at is not None
    assert archive["chen"].side_asked_at is not None


async def test_a_relation_to_yourself_is_refused(db_session, archive):
    with pytest.raises(ValueError, match="two different people"):
        await entity_store.set_relation_by_hand(
            db_session,
            producer_id=archive["user"].id,
            from_entity_id=archive["chen"].id,
            to_entity_id=archive["chen"].id,
            relation_type="parent",
        )


async def test_an_unknown_relation_type_is_refused(db_session, archive):
    with pytest.raises(ValueError, match="not a relation"):
        await entity_store.set_relation_by_hand(
            db_session,
            producer_id=archive["user"].id,
            from_entity_id=archive["raz"].id,
            to_entity_id=archive["chen"].id,
            relation_type="brother-ish",
        )


async def test_someone_elses_person_is_refused(db_session, archive):
    other = User(
        id="u-other", email="o@example.com", username="other",
        hashed_password="x", role="producer",
    )
    db_session.add(other)
    await db_session.flush()
    theirs = Entity(
        producer_id=other.id, name="Stranger", normalized_name="stranger", type="person",
    )
    db_session.add(theirs)
    await db_session.flush()

    with pytest.raises(ValueError, match="in this archive"):
        await entity_store.set_relation_by_hand(
            db_session,
            producer_id=archive["user"].id,
            from_entity_id=archive["chen"].id,
            to_entity_id=theirs.id,
            relation_type="parent",
        )


async def test_the_tree_endpoint_serialises_a_hand_made_edge(client, db_session, archive):
    """The bug this pins cost a working tree page.

    Making source_segment_id nullable in the DB is only half the change: the
    RESPONSE schema still declared it a plain str, so the first hand-made edge
    made /entities/tree raise ResponseValidationError. And because an
    unhandled exception is caught by ServerErrorMiddleware, which sits OUTSIDE
    CORSMiddleware, the 500 carried no CORS headers — so the browser reported
    a CORS policy error and the real cause was invisible.

    Serialising the response is what the service-level tests never exercised.
    """
    from app.api.v1.users import create_access_token

    await entity_store.set_relation_by_hand(
        db_session,
        producer_id=archive["user"].id,
        from_entity_id=archive["raz"].id,
        to_entity_id=archive["chen"].id,
        relation_type="parent",
    )
    await db_session.commit()

    token = create_access_token({"sub": archive["user"].id})
    response = await client.get(
        "/api/v1/entities/tree", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    edges = response.json()["edges"]
    hand_made = [e for e in edges if e["source_segment_id"] is None]
    assert len(hand_made) == 1, "a hand-made edge has no recording, and must still serialise"
