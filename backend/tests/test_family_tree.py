"""
Phase 4 of docs/FAMILY_TREE_TIMELINE.md — the family tree.

"The tree never guesses" is the rule under test. Every case here is one where
guessing would produce a page that looks perfectly fine and is wrong: a person
parked in the middle row because nothing said where they belong, a generation
quietly moved because two recordings disagreed, an edge drawn from a relation
type nobody gave an offset for.
"""

import pytest
from datetime import datetime, timezone

from app.models import (
    Entity,
    EntityMention,
    EntityRelation,
    InterviewSession,
    RawSegment,
    RelationType,
    User,
)
from app.services import family_tree


@pytest.fixture
async def archive(db_session):
    """A producer with a self-entity, a recording, and the tree vocabulary."""
    user = User(
        id="u-tree", email="t@example.com", username="tree",
        hashed_password="x", role="producer", full_name="Root Person",
    )
    db_session.add(user)
    await db_session.flush()

    for rt, delta, symmetric, inverse in [
        ("parent", -1, False, "child"),
        ("child", 1, False, "parent"),
        ("grandparent", -2, False, "grandchild"),
        ("grandchild", 2, False, "grandparent"),
        ("sibling", 0, True, None),
        ("spouse", 0, True, None),
        # A tree edge with NO offset — the "cannot place it" case.
        ("step_parent", None, False, None),
        # Not a tree edge at all; must never appear.
        ("friend", None, True, None),
    ]:
        db_session.add(
            RelationType(
                relation_type=rt, category="family", is_tree_edge=(rt != "friend"),
                inverse_type=inverse, is_symmetric=symmetric,
                label_en=rt, label_he=rt, generation_delta=delta,
            )
        )

    session = InterviewSession(user_id=user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    segment = RawSegment(
        interview_session_id=session.id, question_asked="Tell me about your family",
        question_index=0, question_id="childhood_q01", status="ready",
        video_url="http://example/v.webm", transcript="…",
    )
    db_session.add(segment)

    root = Entity(
        id="e-root", producer_id=user.id, name="Root Person",
        normalized_name="root person", type="person", is_self=True,
    )
    db_session.add(root)
    await db_session.flush()
    return user, segment, root


async def _person(db, user, name, **kw):
    e = Entity(
        producer_id=user.id, name=name, normalized_name=name.lower(),
        type="person", **kw,
    )
    db.add(e)
    await db.flush()
    return e


async def _relate(db, src, rel, dst, segment):
    db.add(
        EntityRelation(
            from_entity_id=src.id, to_entity_id=dst.id,
            relation_type=rel, source_segment_id=segment.id,
        )
    )
    await db.flush()


def _row(tree, generation):
    return next(
        (g["people"] for g in tree["generations"] if g["generation"] == generation), []
    )


# ── placement ─────────────────────────────────────────────────────────────


async def test_an_archive_with_no_relations_is_just_the_producer(db_session, archive):
    """An empty tree, not an error — the page says so and explains where
    family comes from."""
    user, _, root = archive
    tree = await family_tree.build_tree(db_session, user.id)
    assert tree["root_id"] == root.id
    assert [p["name"] for p in _row(tree, 0)] == ["Root Person"]
    assert tree["unplaced"] == []


async def test_parents_go_up_and_children_go_down(db_session, archive):
    """The direction test, at the layout level: getting it backwards renders a
    perfectly good-looking tree that is upside down."""
    user, segment, root = archive
    dad = await _person(db_session, user, "Dad")
    kid = await _person(db_session, user, "Kid")
    await _relate(db_session, dad, "parent", root, segment)
    await _relate(db_session, kid, "child", root, segment)

    tree = await family_tree.build_tree(db_session, user.id)
    assert [p["name"] for p in _row(tree, -1)] == ["Dad"]
    assert [p["name"] for p in _row(tree, 1)] == ["Kid"]


async def test_siblings_and_spouses_share_the_producers_row(db_session, archive):
    user, segment, root = archive
    sib = await _person(db_session, user, "Sib")
    spouse = await _person(db_session, user, "Spouse")
    await _relate(db_session, sib, "sibling", root, segment)
    await _relate(db_session, spouse, "spouse", root, segment)

    tree = await family_tree.build_tree(db_session, user.id)
    assert {p["name"] for p in _row(tree, 0)} == {"Root Person", "Sib", "Spouse"}


async def test_edges_are_walked_in_both_directions(db_session, archive):
    """The schema stores ONE directed row and derives the inverse at read
    time, so a walk that only followed from->to would never reach a parent
    from their child — and every ancestor would come back unplaced."""
    user, segment, root = archive
    gran = await _person(db_session, user, "Gran")
    await _relate(db_session, gran, "grandparent", root, segment)

    tree = await family_tree.build_tree(db_session, user.id)
    assert [p["name"] for p in _row(tree, -2)] == ["Gran"]
    assert tree["unplaced"] == []


async def test_a_chain_places_people_by_their_shortest_path(db_session, archive):
    user, segment, root = archive
    dad = await _person(db_session, user, "Dad")
    grandad = await _person(db_session, user, "Grandad")
    await _relate(db_session, dad, "parent", root, segment)
    await _relate(db_session, grandad, "parent", dad, segment)

    tree = await family_tree.build_tree(db_session, user.id)
    assert [p["name"] for p in _row(tree, -1)] == ["Dad"]
    assert [p["name"] for p in _row(tree, -2)] == ["Grandad"]


# ── the honest cases ──────────────────────────────────────────────────────


async def test_someone_with_no_family_path_is_unplaced_not_dropped(db_session, archive):
    """Real today: איציק כהן and רוני כהן are mentioned in the archive with no
    family relation. Dropping them hides someone the producer talked about;
    putting them in row 0 draws a family that does not exist."""
    user, _, _ = archive
    await _person(db_session, user, "A Friend")

    tree = await family_tree.build_tree(db_session, user.id)
    assert [p["name"] for p in tree["unplaced"]] == ["A Friend"]
    assert all(p["generation"] is None for p in tree["unplaced"])
    assert "A Friend" not in [p["name"] for row in tree["generations"] for p in row["people"]]


async def test_a_tree_type_with_no_offset_leaves_its_person_unplaced(db_session, archive):
    """Marked as a tree edge but nobody said how far it moves. Assuming zero
    would put a step-parent in the producer's own row."""
    user, segment, root = archive
    step = await _person(db_session, user, "Step")
    await _relate(db_session, step, "step_parent", root, segment)

    tree = await family_tree.build_tree(db_session, user.id)
    assert [p["name"] for p in tree["unplaced"]] == ["Step"]
    assert tree["missing_generation_delta"] == ["step_parent"]


async def test_a_non_tree_relation_never_places_anyone(db_session, archive):
    """is_tree_edge is authoritative: a friend is a real relation worth
    storing and not a branch of the family tree."""
    user, segment, root = archive
    friend = await _person(db_session, user, "Mate")
    await _relate(db_session, friend, "friend", root, segment)

    tree = await family_tree.build_tree(db_session, user.id)
    assert [p["name"] for p in tree["unplaced"]] == ["Mate"]
    assert tree["edges"] == []


async def test_contradicting_recordings_are_reported_not_silently_resolved(
    db_session, archive
):
    """Two recordings that cannot both be true. Redrawing on the later one
    would move somebody's generation with nothing to show for it.

    WHICH of the two wins is deliberately not asserted. Both rows are written
    in one transaction, so `created_at` is identical and `_load_edges` falls
    through to ordering by `id` — a fresh uuid4 on every run. Pinning the
    winner made this test a coin flip that passed or failed depending on how
    the ids sorted that day. What matters, and what is guaranteed, is that the
    disagreement is REPORTED and the person is drawn once instead of twice.
    """
    user, segment, root = archive
    other = await _person(db_session, user, "Ambiguous")
    await _relate(db_session, other, "parent", root, segment)
    await _relate(db_session, other, "child", root, segment)

    tree = await family_tree.build_tree(db_session, user.id)
    assert len(tree["contradictions"]) == 1
    conflict = tree["contradictions"][0]
    # one path says a generation up, the other says a generation down
    assert {conflict["kept_generation"], conflict["implied_generation"]} == {-1, 1}
    # whichever was kept is the row the person actually sits in
    assert [p["name"] for p in _row(tree, conflict["kept_generation"])] == ["Ambiguous"]
    # and the person is still drawn exactly once
    placed = [p["name"] for row in tree["generations"] for p in row["people"]]
    assert placed.count("Ambiguous") == 1


async def test_a_cycle_terminates(db_session, archive):
    """A -> B -> A would loop forever on a naive walk."""
    user, segment, root = archive
    a = await _person(db_session, user, "A")
    b = await _person(db_session, user, "B")
    await _relate(db_session, a, "sibling", root, segment)
    await _relate(db_session, a, "sibling", b, segment)
    await _relate(db_session, b, "sibling", a, segment)

    tree = await family_tree.build_tree(db_session, user.id)
    assert {p["name"] for p in _row(tree, 0)} == {"Root Person", "A", "B"}


async def test_a_producer_with_no_self_entity_gets_an_empty_tree(db_session):
    """Generation is meaningful only relative to somebody. With no root there
    is nothing to measure from, so everyone is unplaced rather than guessed."""
    user = User(
        id="u-rootless", email="r@example.com", username="rootless",
        hashed_password="x", role="producer",
    )
    db_session.add(user)
    await db_session.flush()
    await _person(db_session, user, "Someone")

    tree = await family_tree.build_tree(db_session, user.id)
    assert tree["root_id"] is None
    assert tree["generations"] == []
    assert [p["name"] for p in tree["unplaced"]] == ["Someone"]


async def test_only_people_are_in_the_tree(db_session, archive):
    user, _, _ = archive
    db_session.add(
        Entity(
            producer_id=user.id, name="Tiberias", normalized_name="tiberias",
            type="place",
        )
    )
    await db_session.flush()
    tree = await family_tree.build_tree(db_session, user.id)
    everyone = [p["name"] for row in tree["generations"] for p in row["people"]]
    assert "Tiberias" not in everyone + [p["name"] for p in tree["unplaced"]]


# ── siblings and their parents ────────────────────────────────────────────
#
# The question these answer: does a sibling whose parent is recorded — whether
# that is the producer's own parent or somebody else's — already place and draw
# correctly, with no special handling? It does. Nothing below is new code being
# tested; it is the existing walk being pinned down so the answer stays true.


async def test_a_sibling_sharing_the_producers_parents_is_placed_by_both_paths(
    db_session, archive
):
    """Two routes to the same row — sibling-of-me, and child-of-my-parent. They
    must agree, or the shared parentage shows up as a contradiction."""
    user, segment, root = archive
    dad = await _person(db_session, user, "Dad")
    mum = await _person(db_session, user, "Mum")
    sib = await _person(db_session, user, "Sib")
    await _relate(db_session, dad, "parent", root, segment)
    await _relate(db_session, mum, "parent", root, segment)
    await _relate(db_session, sib, "sibling", root, segment)
    # the same two parents, now recorded for the sibling as well
    await _relate(db_session, dad, "parent", sib, segment)
    await _relate(db_session, mum, "parent", sib, segment)

    tree = await family_tree.build_tree(db_session, user.id)
    assert {p["name"] for p in _row(tree, 0)} == {"Root Person", "Sib"}
    assert {p["name"] for p in _row(tree, -1)} == {"Dad", "Mum"}
    assert tree["contradictions"] == [], "the two routes must not disagree"
    # Both children hang off the same pair, which is what lets the page draw
    # one trunk with two drops rather than two unrelated descents.
    pairs = {(e["from_id"], e["to_id"]) for e in tree["edges"]}
    assert (dad.id, sib.id) in pairs and (mum.id, sib.id) in pairs
    assert (dad.id, root.id) in pairs and (mum.id, root.id) in pairs


async def test_a_half_sibling_with_a_different_recorded_parent_places_that_parent(
    db_session, archive
):
    """The case worth being sure about: a sibling whose other parent is someone
    else entirely. That parent is reached THROUGH the sibling, so it only works
    because the walk goes both ways along every edge."""
    user, segment, root = archive
    dad = await _person(db_session, user, "Dad")
    other = await _person(db_session, user, "Rivka")
    half = await _person(db_session, user, "Half")
    await _relate(db_session, dad, "parent", root, segment)
    await _relate(db_session, half, "sibling", root, segment)
    await _relate(db_session, dad, "parent", half, segment)
    await _relate(db_session, other, "parent", half, segment)

    tree = await family_tree.build_tree(db_session, user.id)
    assert {p["name"] for p in _row(tree, -1)} == {"Dad", "Rivka"}
    assert {p["name"] for p in _row(tree, 0)} == {"Root Person", "Half"}
    assert tree["unplaced"] == [], "Rivka is reachable via the half-sibling"
    assert tree["contradictions"] == []


async def test_a_sibling_with_no_recorded_parent_is_still_placed_but_undrawn(
    db_session, archive
):
    """Today's live archive: siblings are recorded as siblings OF THE PRODUCER
    and nothing says whose children they are. They belong in the row — that is
    recorded — but there is no parent edge, so the page draws no line to them.
    Inferring one would be inventing a fact about who someone's parents are."""
    user, segment, root = archive
    dad = await _person(db_session, user, "Dad")
    sib = await _person(db_session, user, "Sib")
    await _relate(db_session, dad, "parent", root, segment)
    await _relate(db_session, sib, "sibling", root, segment)

    tree = await family_tree.build_tree(db_session, user.id)
    assert {p["name"] for p in _row(tree, 0)} == {"Root Person", "Sib"}
    # No edge connects Dad to Sib, so nothing can be drawn between them.
    assert not any(
        e["from_id"] == dad.id and e["to_id"] == sib.id for e in tree["edges"]
    )


# ── moments ───────────────────────────────────────────────────────────────


async def test_clicking_a_person_returns_their_recordings(db_session, archive):
    """Every edge carries source_segment_id, so a relation links back to the
    recording where it was said — that is the property worth designing around."""
    user, segment, _ = archive
    sib = await _person(db_session, user, "Sib")
    db_session.add(
        EntityMention(
            entity_id=sib.id, raw_segment_id=segment.id, summary="the speaker's sibling"
        )
    )
    await db_session.flush()

    moments = await family_tree.get_entity_moments(db_session, user.id, sib.id)
    assert len(moments) == 1
    assert moments[0]["question_asked"] == "Tell me about your family"
    assert moments[0]["video_url"] == "http://example/v.webm"
    assert moments[0]["summary"] == "the speaker's sibling"


async def test_moments_are_scoped_to_the_producer(db_session, archive):
    """An entity id is unguessable, but authorisation must not rest on that."""
    user, segment, _ = archive
    other = User(
        id="u-other-tree", email="o@example.com", username="othertree",
        hashed_password="x", role="producer",
    )
    db_session.add(other)
    await db_session.flush()
    sib = await _person(db_session, user, "Sib")
    db_session.add(EntityMention(entity_id=sib.id, raw_segment_id=segment.id))
    await db_session.flush()

    assert await family_tree.get_entity_moments(db_session, other.id, sib.id) == []
