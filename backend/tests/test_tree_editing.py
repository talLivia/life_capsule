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
        ("aunt_uncle", -1, False, "niece_nephew"),
        ("grandparent", -2, False, "grandchild"),
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


async def test_a_contradiction_with_a_DIFFERENT_pair_is_replaced(db_session, archive):
    """The bug that made this look broken on the live archive.

    Told "יוסף is רז's child", the edges that disagree are יוסף's SIBLING edge
    to the producer and his parent edges from אילנה and צבי — none of which
    involve רז. A replacement scoped to the two people named finds nothing, so
    nothing is removed, the tree keeps its first placement and the edit appears
    to have silently failed.
    """
    # Raz is the producer's sibling, so Raz sits at generation 0.
    await archive["relate"](archive["raz"], "sibling", archive["root"])
    # Chen is placed at generation 0 too — as a sibling AND via a parent.
    await archive["relate"](archive["chen"], "sibling", archive["root"])
    await archive["relate"](archive["tzvi"], "parent", archive["chen"])
    await archive["relate"](archive["tzvi"], "parent", archive["root"])

    result = await entity_store.set_relation_by_hand(
        db_session,
        producer_id=archive["user"].id,
        from_entity_id=archive["chen"].id,
        to_entity_id=archive["raz"].id,
        relation_type="child",
    )

    replaced = {r["relation_type"] for r in result["replaced"]}
    assert "sibling" in replaced, "the sibling edge to the producer put Chen a generation too high"
    assert "parent" in replaced, "so did the parent edge from Tzvi"

    # And Tzvi's OWN placement is untouched — the edit was about Chen.
    from app.services import family_tree

    generations = await family_tree.generations_for(db_session, archive["user"].id)
    assert generations[archive["chen"].id] == 1, "Chen is now Raz's child, a generation below"
    assert generations[archive["tzvi"].id] == -1


async def test_saving_the_same_relation_twice_does_not_duplicate_it(db_session, archive):
    """Clicking Save again when nothing appeared to happen is exactly what a
    producer does — the live archive ended up with three identical rows."""
    for _ in range(3):
        await entity_store.set_relation_by_hand(
            db_session,
            producer_id=archive["user"].id,
            from_entity_id=archive["chen"].id,
            to_entity_id=archive["raz"].id,
            relation_type="child",
        )

    edges = await _edges(db_session, archive["chen"], archive["raz"])
    assert len(edges) == 1, "one statement, one row, however many times it is saved"


async def test_an_agreeing_edge_with_a_different_delta_is_kept(db_session, archive):
    """THE SIGN REGRESSION. gen(from) = gen(to) + delta is how the tree walks
    an edge, and the replacement math had it backwards — computing a child's
    target a generation ABOVE the parent. It stayed hidden because a same-delta
    replacement cancels the flip, and the mixed-delta cases the other tests
    cover happened to disagree on both conventions. The first AGREEING
    mixed-delta neighbourhood — a sibling, a spouse — was deleted wholesale
    the moment somebody was placed as their parent's child.
    """
    # Tzvi is the producer's parent; Chen is the producer's sibling with a
    # spouse. Placing Chen as Tzvi's child agrees with everything already
    # recorded, so NOTHING may be replaced.
    await archive["relate"](archive["tzvi"], "parent", archive["root"])
    await archive["relate"](archive["chen"], "sibling", archive["root"])
    await archive["relate"](archive["raz"], "spouse", archive["chen"])

    result = await entity_store.set_relation_by_hand(
        db_session,
        producer_id=archive["user"].id,
        from_entity_id=archive["chen"].id,
        to_entity_id=archive["tzvi"].id,
        relation_type="child",
    )

    assert result["replaced"] == [], "agreeing sibling and spouse edges must survive"
    assert len(await _edges(db_session, archive["chen"], archive["root"])) == 1
    assert len(await _edges(db_session, archive["raz"], archive["chen"])) == 1

    from app.services import family_tree

    generations = await family_tree.generations_for(db_session, archive["user"].id)
    assert generations[archive["chen"].id] == 0, "a parent's child sits a generation BELOW them"


# ── the side-of-family question, asked from the tree ─────────────────────────
#
# The manual twin of the questionnaire's side question (side_questions /
# write_sides): an aunt_uncle or grandparent edge places somebody in a row and
# says nothing about which parent they attach to. The endpoint accepts
# side_parent_id and writes the attaching edge the questionnaire would have.


async def _post_relation(client, archive, entity, body):
    from app.api.v1.users import create_access_token

    token = create_access_token({"sub": archive["user"].id})
    return await client.post(
        f"/api/v1/entities/{entity.id}/relations",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_setting_an_uncle_by_hand_can_name_the_side(client, db_session, archive):
    """"Raz is your uncle, on Tzvi's side" writes BOTH edges the questionnaire
    would: the aunt_uncle edge, and the sibling edge that attaches Raz beside
    Tzvi in the parents' row — the whole reason the side question exists."""
    await archive["relate"](archive["tzvi"], "parent", archive["root"])
    await db_session.commit()

    response = await _post_relation(
        client, archive, archive["raz"],
        {
            "other_entity_id": archive["root"].id,
            "relation_type": "aunt_uncle",
            "side_parent_id": archive["tzvi"].id,
        },
    )

    assert response.status_code == 200, response.text
    uncle_edges = await _edges(db_session, archive["raz"], archive["root"])
    assert [e.relation_type for e in uncle_edges] == ["aunt_uncle"]
    side_edges = await _edges(db_session, archive["raz"], archive["tzvi"])
    assert [e.relation_type for e in side_edges] == ["sibling"], (
        "the side answer is the sibling edge to that parent — and the "
        "aunt_uncle edge just written must survive it (the sign regression "
        "made the second write delete the first)"
    )


async def test_setting_a_grandparent_by_hand_attaches_them_as_the_parents_parent(
    client, db_session, archive
):
    await archive["relate"](archive["tzvi"], "parent", archive["root"])
    await db_session.commit()

    response = await _post_relation(
        client, archive, archive["raz"],
        {
            "other_entity_id": archive["root"].id,
            "relation_type": "grandparent",
            "side_parent_id": archive["tzvi"].id,
        },
    )

    assert response.status_code == 200, response.text
    side_edges = await _edges(db_session, archive["raz"], archive["tzvi"])
    assert [e.relation_type for e in side_edges] == ["parent"], (
        "a grandparent on Tzvi's side is Tzvi's PARENT — same mapping as write_sides"
    )


async def test_the_side_must_be_a_recorded_parent(client, db_session, archive):
    """Chen is not a recorded parent of the producer, so attaching an uncle to
    'Chen's side' would invent a branch nobody stated. Refused, and the main
    relation is not half-written either — the request fails as a whole."""
    await db_session.commit()

    response = await _post_relation(
        client, archive, archive["raz"],
        {
            "other_entity_id": archive["root"].id,
            "relation_type": "aunt_uncle",
            "side_parent_id": archive["chen"].id,
        },
    )

    assert response.status_code == 400
    assert "recorded parent" in response.json()["detail"]


async def test_a_side_makes_no_sense_for_a_sibling(client, db_session, archive):
    await archive["relate"](archive["tzvi"], "parent", archive["root"])
    await db_session.commit()

    response = await _post_relation(
        client, archive, archive["raz"],
        {
            "other_entity_id": archive["root"].id,
            "relation_type": "sibling",
            "side_parent_id": archive["tzvi"].id,
        },
    )

    assert response.status_code == 400


# ── removing one relation from a person's card ───────────────────────────────


async def test_removing_a_relation_deletes_exactly_that_edge(client, db_session, archive):
    """The way out for an edge that is simply WRONG — set_relation can only
    replace a contradiction with a different claim, never retract one."""
    kept = await archive["relate"](archive["chen"], "sibling", archive["root"])
    wrong = await archive["relate"](archive["raz"], "parent", archive["chen"])
    await db_session.commit()

    from app.api.v1.users import create_access_token

    token = create_access_token({"sub": archive["user"].id})
    response = await client.delete(
        f"/api/v1/entities/{archive['raz'].id}/relations/{wrong.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["removed"]["relation_type"] == "parent"
    assert await _edges(db_session, archive["raz"], archive["chen"]) == []
    assert len(await _edges(db_session, archive["chen"], archive["root"])) == 1, (
        f"the unrelated edge {kept.id} must survive"
    )


async def test_removing_a_relation_requires_the_edge_to_touch_the_person(
    client, db_session, archive
):
    """A leaked relation id must not delete an edge between two strangers —
    the entity in the URL is part of the authorisation, not decoration."""
    edge = await archive["relate"](archive["chen"], "sibling", archive["root"])
    await db_session.commit()

    from app.api.v1.users import create_access_token

    token = create_access_token({"sub": archive["user"].id})
    response = await client.delete(
        f"/api/v1/entities/{archive['raz'].id}/relations/{edge.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert len(await _edges(db_session, archive["chen"], archive["root"])) == 1
