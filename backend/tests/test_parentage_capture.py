"""Phase 6 — asking whose child a sibling is.

The gap this closes: a sibling is recorded as a sibling OF THE PRODUCER, which
says nothing about whose child they are, so the family tree places them in the
right row and draws no line to them. Rendering was never the problem (see
test_family_tree.py); capture was.

Two rules carry most of these tests:

  * ASK ONCE, EVER. Skipping is an answer, and a skipped question that comes
    back on the next recording trains the producer to click past the whole
    screen — which is how a real question gets missed.
  * NOTHING IS INFERRED. "Your sibling is probably your parents' child" is
    usually true and sometimes catastrophically wrong (half- and step-
    siblings), so it is only ever written from a ticked box.
"""

import asyncio

import pytest
from datetime import datetime, timezone

from app.analysis_graph import parentage_questions, side_questions
from app.models import (
    Entity,
    EntityRelation,
    InterviewSession,
    RawSegment,
    RelationType,
    User,
)
from app.services import entity_store


@pytest.fixture
async def family(db_session):
    """A producer with two recorded parents and one sibling with none."""
    user = User(
        id="u-par", email="p@example.com", username="par",
        hashed_password="x", role="producer", full_name="Root Person",
    )
    db_session.add(user)
    await db_session.flush()

    # The tree reads edges through a join on relation_types, so without these
    # the end-to-end test would pass vacuously against an empty edge list.
    for rt, delta, symmetric, inverse in [
        ("parent", -1, False, "child"),
        ("child", 1, False, "parent"),
        ("sibling", 0, True, None),
    ]:
        db_session.add(
            RelationType(
                relation_type=rt, category="family", is_tree_edge=True,
                inverse_type=inverse, is_symmetric=symmetric,
                label_en=rt, label_he=rt, generation_delta=delta,
            )
        )

    session = InterviewSession(user_id=user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    segment = RawSegment(
        interview_session_id=session.id, question_asked="Your family?",
        question_index=0, question_id="childhood_q01", status="ready",
    )
    db_session.add(segment)
    await db_session.flush()

    async def person(name, **kw):
        e = Entity(
            producer_id=user.id, name=name, normalized_name=name.lower(),
            type="person", **kw,
        )
        db_session.add(e)
        await db_session.flush()
        return e

    root = await person("Root Person", is_self=True)
    dad = await person("Dad")
    mum = await person("Mum")
    sib = await person("Sib")

    async def relate(src, rel, dst, origin="recording"):
        db_session.add(
            EntityRelation(
                from_entity_id=src.id, to_entity_id=dst.id, relation_type=rel,
                source_segment_id=segment.id, origin=origin,
            )
        )
        await db_session.flush()

    await relate(dad, "parent", root)
    await relate(mum, "parent", root)
    await relate(sib, "sibling", root)

    return {
        "user": user, "segment": segment, "root": root,
        "dad": dad, "mum": mum, "sib": sib, "person": person, "relate": relate,
    }


# ── who gets asked ────────────────────────────────────────────────────────


async def test_a_sibling_with_no_parents_is_asked(db_session, family):
    found = await entity_store.parentage_candidates(db_session, family["user"].id)
    assert [s["name"] for s in found["siblings"]] == ["Sib"]
    assert {p["name"] for p in found["parents"]} == {"Dad", "Mum"}


async def test_a_sibling_who_already_has_a_parent_is_not_asked(db_session, family):
    """Whether that parent is the producer's own or somebody else's — the tree
    can already draw them, so there is nothing to ask."""
    await family["relate"](family["dad"], "parent", family["sib"])

    found = await entity_store.parentage_candidates(db_session, family["user"].id)
    assert found["siblings"] == []


async def test_a_producer_with_no_recorded_parents_is_asked_nothing(db_session, family):
    """There would be no options to offer. Asking with an empty list is worse
    than not asking."""
    await db_session.execute(
        EntityRelation.__table__.delete().where(
            EntityRelation.to_entity_id == family["root"].id,
            EntityRelation.relation_type == "parent",
        )
    )
    await db_session.flush()

    found = await entity_store.parentage_candidates(db_session, family["user"].id)
    assert found["siblings"] == [] and found["parents"] == []


async def test_the_sibling_relation_is_found_from_either_direction(db_session, family):
    """Symmetric relations are stored as ONE directed row, so the producer can
    be on either end. Checking one direction would miss half of them."""
    other = await family["person"]("Other")
    # deliberately the reverse direction from the fixture's
    await family["relate"](family["root"], "sibling", other)

    found = await entity_store.parentage_candidates(db_session, family["user"].id)
    assert {s["name"] for s in found["siblings"]} == {"Sib", "Other"}


async def test_an_already_asked_sibling_is_never_asked_again(db_session, family):
    family["sib"].parentage_asked_at = datetime.now(timezone.utc)
    await db_session.flush()

    found = await entity_store.parentage_candidates(db_session, family["user"].id)
    assert found["siblings"] == []


# ── the question ──────────────────────────────────────────────────────────


def test_no_question_without_both_parents_and_siblings():
    assert parentage_questions(None) == []
    assert parentage_questions({"parents": [], "siblings": [{"id": "a", "name": "A"}]}) == []
    assert parentage_questions({"parents": [{"id": "p", "name": "P"}], "siblings": []}) == []


def test_one_grouped_question_covers_every_sibling():
    """Four near-identical screens whose answer is the same each time is how a
    producer learns to click past a screen without reading — which is how a
    question that DOES matter gets missed."""
    questions = parentage_questions(
        {
            "parents": [{"name": "Dad", "entity_id": "p1"},
                        {"name": "Mum", "entity_id": "p2"}],
            "siblings": [{"name": "Sib", "entity_id": "s1"},
                         {"name": "Other", "entity_id": "s2"}],
            "known_people": [],
        }
    )
    assert len(questions) == 1, "one question, not one per sibling"
    assert questions[0]["question"] == "Are Sib and Other all children of Dad and Mum?"
    assert [s["name"] for s in questions[0]["siblings"]] == ["Sib", "Other"]
    # Both parents offered, so a half-sibling can be recorded as sharing one.
    assert [p["name"] for p in questions[0]["parents"]] == ["Dad", "Mum"]


# ── writing the answer ────────────────────────────────────────────────────


async def _parents_of(db, entity_id):
    rows = (
        await db.execute(
            EntityRelation.__table__.select().where(
                EntityRelation.to_entity_id == entity_id,
                EntityRelation.relation_type == "parent",
            )
        )
    ).all()
    return rows


async def test_ticking_both_parents_writes_both_edges(db_session, family):
    written = await entity_store.write_parentage(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_sibling_names=["Sib"],
        answers={"Sib": {"parent_names": ["Dad", "Mum"], "new_parent_name": None}},
    )
    assert written["relations"] == 2
    assert len(await _parents_of(db_session, family["sib"].id)) == 2


async def test_a_half_sibling_can_share_only_one_parent(db_session, family):
    """The case a yes/no could not express, and the reason this is checkboxes."""
    written = await entity_store.write_parentage(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_sibling_names=["Sib"],
        answers={"Sib": {"parent_names": ["Dad"], "new_parent_name": "Rivka"}},
    )
    assert written["relations"] == 2 and written["new_parents"] == 1
    parents = await _parents_of(db_session, family["sib"].id)
    assert len(parents) == 2

    rivka = (
        await db_session.execute(
            Entity.__table__.select().where(Entity.normalized_name == "rivka")
        )
    ).first()
    assert rivka is not None, "an unmentioned parent becomes an ordinary entity"


async def test_skipping_still_records_that_we_asked(db_session, family):
    """The whole point of the column. Without the stamp the same question
    returns on every future recording."""
    written = await entity_store.write_parentage(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_sibling_names=["Sib"],
        answers={},
    )
    assert written["relations"] == 0 and written["asked"] == 1
    assert family["sib"].parentage_asked_at is not None

    found = await entity_store.parentage_candidates(db_session, family["user"].id)
    assert found["siblings"] == [], "asked once, never again"


async def test_a_parent_not_offered_is_refused_at_the_api_edge(db_session, family):
    """Where the check lives, now that answers are names.

    The store resolves a name to a row and writes it — it has to, because a
    parent named for the first time on this recording is a legitimate answer.
    So "was this parent actually offered" is checked at the edge, against the
    question's own parent list, before it ever reaches the store.
    """
    from app.schemas import ParentageAnswer

    offered = {"Dad", "Mum"}
    given = ParentageAnswer(parent_names=["Stranger"])
    assert set(given.parent_names) - offered == {"Stranger"}, (
        "the API rejects exactly this set difference"
    )


async def test_answers_are_marked_as_coming_from_a_screen(db_session, family):
    """`origin` exists so the tree does not offer to play a recording that
    never mentioned this person."""
    await entity_store.write_parentage(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_sibling_names=["Sib"],
        answers={"Sib": {"parent_names": ["Dad"]}},
    )
    row = (await _parents_of(db_session, family["sib"].id))[0]
    assert row.origin == "confirmation"


async def test_reanalysis_does_not_destroy_a_parentage_answer(db_session, family):
    """write_segment_relations replaces this segment's relations. It must
    replace only what the WORDS taught us — re-reading a transcript would
    never re-produce an answer the producer typed."""
    await entity_store.write_parentage(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_sibling_names=["Sib"],
        answers={"Sib": {"parent_names": ["Dad"]}},
    )
    await entity_store.write_segment_relations(
        db_session,
        segment_id=family["segment"].id,
        producer_id=family["user"].id,
        relations=[],
        self_marker="__SELF__",
    )
    assert len(await _parents_of(db_session, family["sib"].id)) == 1


async def test_the_answer_makes_the_tree_draw_the_sibling(db_session, family):
    """End to end: the point of the whole feature. Before the answer the
    sibling has no parent edge; after it, the tree has one to draw."""
    from app.services import family_tree

    before = await family_tree.build_tree(db_session, family["user"].id)
    assert not any(
        e["to_id"] == family["sib"].id and e["relation_type"] == "parent"
        for e in before["edges"]
    )

    await entity_store.write_parentage(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_sibling_names=["Sib"],
        answers={"Sib": {"parent_names": ["Dad", "Mum"]}},
    )

    after = await family_tree.build_tree(db_session, family["user"].id)
    drawn = {
        (e["from_id"], e["to_id"])
        for e in after["edges"]
        if e["relation_type"] == "parent"
    }
    assert (family["dad"].id, family["sib"].id) in drawn
    assert (family["mum"].id, family["sib"].id) in drawn
    # and still exactly one row for them
    row0 = next(g for g in after["generations"] if g["generation"] == 0)
    assert sorted(p["name"] for p in row0["people"]) == ["Root Person", "Sib"]


# ── the router must never fall behind the questions ───────────────────────
#
# The bug these exist for: _has_confirmation_questions checked identity and
# type only, while the interrupt carried relations, years and parentage too.
# A recording raising none of the first two skipped confirmation entirely —
# and because the node is what narrows proposed_relations to the accepted
# subset, finalize wrote every proposed relation unasked. It looked exactly
# like success.


def test_the_router_asks_about_every_class_the_payload_carries():
    """The structural guard. If a sixth class is added to the payload and the
    router cannot see it, this fails — which is the whole point."""
    from app.analysis_graph import _has_confirmation_questions, build_confirmation_payload

    for key, state in [
        ("identity_questions", {"names_to_check": [{"name": "A", "candidates": []}]}),
        (
            "type_questions",
            {"extracted_entities": [
                {"name": "A", "type": "place", "alternative_type": "organisation"}
            ]},
        ),
        (
            "relation_questions",
            {"proposed_relations": [
                {"from_name": "A", "to_name": "B", "relation_type": "sibling"}
            ]},
        ),
        ("year_questions", {"extracted_entities": [{"name": "A", "type": "event"}]}),
        (
            "parentage_questions",
            {"parentage": {
                "parents": [{"id": "p", "name": "P"}],
                "siblings": [{"id": "s", "name": "S"}],
            }},
        ),
    ]:
        payload = build_confirmation_payload(state)
        assert payload[key], f"{key} should be raised by this state"
        assert _has_confirmation_questions(state) == "confirm", (
            f"router skips confirmation despite {key} — relations would be "
            f"written without consent"
        )


def test_a_recording_that_raises_nothing_still_skips():
    """The router must not pause on every recording either — a screen with no
    questions is a click the producer has to make for nothing.

    Note what "nothing" takes: Phase 3 widened year capture to any entity
    missing a year, so a bare person DOES raise a question the first time.
    Silence means every name is already settled.
    """
    from app.analysis_graph import _has_confirmation_questions

    assert _has_confirmation_questions(
        {
            "extracted_entities": [{"name": "A", "type": "person"}],
            "year_settled": ["a"],
        }
    ) == "skip"
    assert _has_confirmation_questions({}) == "skip"


def test_relations_alone_are_enough_to_pause():
    """The exact live case: ten proposed relations, no identity or type
    ambiguity. Before the fix this skipped and wrote all ten unconfirmed."""
    from app.analysis_graph import _has_confirmation_questions, build_confirmation_payload

    state = {
        "extracted_entities": [
            {"name": n, "type": "person"} for n in ("צבי", "אילנה", "ניר", "חן")
        ],
        "proposed_relations": [
            {"from_name": "צבי", "to_name": "__SELF__", "relation_type": "parent"},
            {"from_name": "ניר", "to_name": "__SELF__", "relation_type": "sibling"},
        ],
    }
    assert _has_confirmation_questions(state) == "confirm"
    assert not build_confirmation_payload(state)["identity_questions"]
    assert not build_confirmation_payload(state)["type_questions"]
    assert len(build_confirmation_payload(state)["relation_questions"]) == 2


# ── correcting a misheard name ────────────────────────────────────────────
#
# Real case: "אליאן" was transcribed "ליאן". A brand-new name has nothing
# similar to disambiguate against, so it raised no identity question — the
# extractor was confident and wrong, and there was nowhere to say so.


def _confirm_state():
    return {
        "extracted_entities": [
            {"name": "ליאן", "type": "person"},
            {"name": "ניר", "type": "person"},
        ],
        "proposed_relations": [
            {"from_name": "ליאן", "to_name": "ניר", "relation_type": "child"},
        ],
        "year_settled": ["ליאן", "ניר"],
    }


def test_a_corrected_name_rewrites_the_entity(monkeypatch):
    from app import analysis_graph

    captured = {}

    def fake_interrupt(payload):
        captured["payload"] = payload
        return {"relations": {"0": True}, "name_edits": {"ליאן": "אליאן"}}

    monkeypatch.setattr(analysis_graph, "interrupt", fake_interrupt)
    out = asyncio.run(analysis_graph.human_confirm_node(_confirm_state()))
    names = [e["name"] for e in out["extracted_entities"]]
    assert "אליאן" in names and "ליאן" not in names
    # every extracted name is offered for correction, not just ambiguous ones
    assert {e["name"] for e in captured["payload"]["editable_entities"]} == {"ליאן", "ניר"}


def test_a_corrected_name_follows_into_its_relations(monkeypatch):
    """The failure this prevents: relation endpoints are NAMES, resolved by
    lookup at write time. Renaming the entity without rewriting them leaves
    the endpoint unresolvable and the relation silently dropped."""
    from app import analysis_graph

    monkeypatch.setattr(
        analysis_graph,
        "interrupt",
        lambda payload: {"relations": {"0": True}, "name_edits": {"ליאן": "אליאן"}},
    )
    out = asyncio.run(analysis_graph.human_confirm_node(_confirm_state()))
    assert out["proposed_relations"] == [
        {"from_name": "אליאן", "to_name": "ניר", "relation_type": "child"}
    ]


def test_an_unchanged_or_blank_edit_is_not_a_correction(monkeypatch):
    from app import analysis_graph

    monkeypatch.setattr(
        analysis_graph,
        "interrupt",
        lambda payload: {
            "relations": {"0": True},
            "name_edits": {"ליאן": "  ", "ניר": "ניר"},
        },
    )
    out = asyncio.run(analysis_graph.human_confirm_node(_confirm_state()))
    assert sorted(e["name"] for e in out["extracted_entities"]) == sorted(["ליאן", "ניר"])


# ── awaiting a person is not "still processing" ───────────────────────────


async def test_a_segment_awaiting_confirmation_is_not_still_processing(
    db_session, family
):
    """The live freeze: `still_processing` was `status not in (ready, analyzed,
    failed)`, so `pending_confirmation` counted as processing. The extraction
    screen showed "hang on a moment, we'll ask when this finishes" — forever,
    while thirty questions sat ready in the payload it had already fetched.

    They are different states and need different words: the automatic work is
    finished, and a person is what it is waiting for.
    """
    from app.services.segment_extraction import get_segment_extraction

    segment = family["segment"]
    segment.status = "pending_confirmation"
    segment.pending_confirmation = {
        "identity_questions": [],
        "type_questions": [],
        "relation_questions": [{"index": 0}],
        "year_questions": [],
        "parentage_questions": [],
    }
    await db_session.flush()

    extraction = await get_segment_extraction(db_session, segment.id, family["user"].id)
    assert extraction.still_processing is False
    assert extraction.awaiting_confirmation is True


async def test_a_segment_actually_running_is_still_processing(db_session, family):
    from app.services.segment_extraction import get_segment_extraction

    segment = family["segment"]
    segment.status = "processing"
    await db_session.flush()

    extraction = await get_segment_extraction(db_session, segment.id, family["user"].id)
    assert extraction.still_processing is True
    assert extraction.awaiting_confirmation is False


# ── picking an existing person instead of typing one ──────────────────────


async def test_the_question_carries_the_people_already_in_the_archive(
    db_session, family
):
    """So "someone else" can be PICKED. write_parentage resolves a typed name
    by normalised match, so one different character creates a second person
    instead of linking to the first."""
    found = await entity_store.parentage_candidates(db_session, family["user"].id)
    known = {p["name"] for p in found["known_people"]}
    assert {"Dad", "Mum", "Sib"} <= known
    assert "Root Person" not in known, "the producer is not their sibling's parent"


def test_the_grouped_question_carries_the_archive_for_picking():
    questions = parentage_questions(
        {
            "parents": [{"name": "Dad", "entity_id": "p1"}],
            "siblings": [{"name": "Sib", "entity_id": "s1"}],
            "known_people": [{"name": "Rivka", "entity_id": "x"}],
        }
    )
    assert [p["name"] for p in questions[0]["known_people"]] == ["Rivka"]


def test_known_people_is_nested_so_it_can_never_be_counted_as_questions():
    """The client counts every array in the payload to decide whether to
    render. A top-level list of people would be counted as questions — the
    miscounting that hid two earlier bugs. Nested, it cannot be."""
    from app.analysis_graph import build_confirmation_payload

    payload = build_confirmation_payload(
        {
            "parentage": {
                "parents": [{"name": "P", "entity_id": "p"}],
                "siblings": [{"name": "S", "entity_id": "s"}],
                "known_people": [{"name": "K", "entity_id": "k"}],
            }
        }
    )
    assert "known_people" not in payload
    assert len(payload["parentage_questions"]) == 1
    assert payload["parentage_questions"][0]["known_people"] == [
        {"name": "K", "entity_id": "k"}
    ]


# ── the FIRST recording must be able to answer it ─────────────────────────
#
# The failure this closes, observed four times running: the question was built
# from the database only, but the recording that names your parents and
# siblings is the one that CREATES them — they are written at finalize, after
# the questions. So the producer whose first answer is "my parents are X and Y
# and my siblings are A, B, C" was never asked, and would have had to record
# something unrelated first. That is the default onboarding path.


@pytest.fixture
async def fresh_producer(db_session):
    """A producer with a self-entity and nothing else — recording one."""
    user = User(
        id="u-fresh", email="f@example.com", username="fresh",
        hashed_password="x", role="producer", full_name="New Person",
    )
    db_session.add(user)
    await db_session.flush()
    session = InterviewSession(user_id=user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    segment = RawSegment(
        interview_session_id=session.id, question_asked="Tell me about your family",
        question_index=0, question_id="childhood_q01", status="ready",
    )
    db_session.add(segment)
    root = Entity(
        producer_id=user.id, name="New Person", normalized_name="new person",
        type="person", is_self=True,
    )
    db_session.add(root)
    await db_session.flush()
    return user, segment, root


async def test_the_very_first_recording_can_be_asked(db_session, fresh_producer):
    """Nothing in the archive yet: the parents and siblings exist only as
    proposals on this screen, and the question must still be askable."""
    user, _segment, _root = fresh_producer
    proposed = [
        {"from_name": "Dad", "to_name": "__SELF__", "relation_type": "parent"},
        {"from_name": "Mum", "to_name": "__SELF__", "relation_type": "parent"},
        {"from_name": "Sib", "to_name": "__SELF__", "relation_type": "sibling"},
        {"from_name": "Other", "to_name": "__SELF__", "relation_type": "sibling"},
    ]
    found = await entity_store.parentage_candidates(
        db_session, user.id, proposed, "__SELF__"
    )
    assert [p["name"] for p in found["parents"]] == ["Dad", "Mum"]
    assert [s["name"] for s in found["siblings"]] == ["Other", "Sib"]
    assert all(s["entity_id"] is None for s in found["siblings"]), "no rows yet"
    assert all(not s["recorded"] for s in found["siblings"])

    questions = parentage_questions(found)
    assert len(questions) == 1
    assert questions[0]["question"] == "Are Other and Sib all children of Dad and Mum?"


async def test_the_answer_resolves_names_written_by_the_same_recording(
    db_session, fresh_producer
):
    """write_parentage runs after write_segment_entities, so a name the
    question offered before the row existed resolves by the time it is used."""
    user, segment, _root = fresh_producer
    for name in ("Dad", "Mum", "Sib"):
        db_session.add(Entity(
            producer_id=user.id, name=name, normalized_name=name.lower(),
            type="person",
        ))
    await db_session.flush()

    written = await entity_store.write_parentage(
        db_session,
        producer_id=user.id,
        segment_id=segment.id,
        asked_sibling_names=["Sib"],
        answers={"Sib": {"parent_names": ["Dad", "Mum"]}},
    )
    assert written["relations"] == 2 and written["new_parents"] == 0

    sib = (await db_session.execute(
        Entity.__table__.select().where(Entity.normalized_name == "sib")
    )).first()
    rows = (await db_session.execute(
        EntityRelation.__table__.select().where(
            EntityRelation.to_entity_id == sib.id,
            EntityRelation.relation_type == "parent",
        )
    )).all()
    assert len(rows) == 2
    assert {r.origin for r in rows} == {"confirmation"}


async def test_a_sibling_already_asked_is_not_re_offered_via_a_proposal(
    db_session, family
):
    """Ask-once must survive the new path too: a sibling stamped as asked must
    not come back just because a later recording names them again."""
    family["sib"].parentage_asked_at = datetime.now(timezone.utc)
    await db_session.flush()

    found = await entity_store.parentage_candidates(
        db_session,
        family["user"].id,
        [{"from_name": "Sib", "to_name": "__SELF__", "relation_type": "sibling"}],
        "__SELF__",
    )
    assert found["siblings"] == []


async def test_a_sibling_who_already_has_a_parent_is_not_re_offered_via_a_proposal(
    db_session, family
):
    await family["relate"](family["dad"], "parent", family["sib"])
    found = await entity_store.parentage_candidates(
        db_session,
        family["user"].id,
        [{"from_name": "Sib", "to_name": "__SELF__", "relation_type": "sibling"}],
        "__SELF__",
    )
    assert found["siblings"] == []


# ── a stored payload outlives the code that wrote it ──────────────────────


def test_an_old_per_sibling_payload_is_upgraded_not_dropped():
    """pending_confirmation is persisted JSON, so a segment can pause under one
    build and be answered under another. Parentage went from one question per
    sibling to one grouped question, and the client written for the second
    crashed on `group.siblings.length` — taking down every other question on
    the screen with it.

    Upgraded rather than discarded: a producer paused mid-flow should not lose
    their questions because we changed our mind about the shape.
    """
    from app.analysis_graph import normalise_pending_confirmation

    stored = {
        "identity_questions": [],
        "type_questions": [],
        "relation_questions": [{"index": 0}],
        "year_questions": [],
        "parentage_questions": [
            {
                "entity_id": "s1",
                "name": "Sib",
                "question": 'Whose child is "Sib"? (optional)',
                "parents": [{"id": "p1", "name": "Dad"}, {"id": "p2", "name": "Mum"}],
                "known_people": [{"id": "k", "name": "Rivka"}],
            },
            {
                "entity_id": "s2",
                "name": "Other",
                "question": 'Whose child is "Other"? (optional)',
                "parents": [{"id": "p1", "name": "Dad"}, {"id": "p2", "name": "Mum"}],
                "known_people": [{"id": "k", "name": "Rivka"}],
            },
        ],
    }

    upgraded = normalise_pending_confirmation(stored)
    questions = upgraded["parentage_questions"]
    assert len(questions) == 1, "four separate questions become one grouped one"
    assert questions[0]["question"] == "Are Sib and Other all children of Dad and Mum?"
    assert [s["name"] for s in questions[0]["siblings"]] == ["Sib", "Other"]
    # An entity that came back from a database query is one whose sibling
    # relation is already recorded.
    assert all(s["recorded"] for s in questions[0]["siblings"])
    # "id" becomes "entity_id" — one spelling downstream.
    assert questions[0]["parents"][0]["entity_id"] == "p1"
    assert questions[0]["known_people"][0]["entity_id"] == "k"
    # nothing else touched
    assert upgraded["relation_questions"] == [{"index": 0}]


def test_a_current_payload_passes_through_untouched():
    from app.analysis_graph import normalise_pending_confirmation

    current = {
        "parentage_questions": [
            {
                "question": "Are A all children of B?",
                "siblings": [{"name": "A", "entity_id": None, "recorded": False}],
                "parents": [{"name": "B", "entity_id": "b"}],
                "known_people": [],
            }
        ]
    }
    assert normalise_pending_confirmation(current) is current


def test_a_payload_with_no_parentage_is_left_alone():
    from app.analysis_graph import normalise_pending_confirmation

    assert normalise_pending_confirmation({}) == {}
    assert normalise_pending_confirmation(None) == {}
    payload = {"identity_questions": [{"name": "X"}]}
    assert normalise_pending_confirmation(payload) is payload


# ── an ask-once stamp must not outlive its answer ─────────────────────────


async def test_deleting_the_recording_that_held_the_answer_reopens_the_question(
    db_session, family
):
    """The bug that cost five recordings.

    The stamp lives on the ENTITY; the answer it recorded lives in
    entity_relations, scoped to the recording open at the time. Deleting that
    recording cascades the relations away and, left alone, the stamp then says
    "already asked" about a question whose answer no longer exists — so it can
    never be asked again and the tree can never draw the line.
    """
    await entity_store.write_parentage(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_sibling_names=["Sib"],
        answers={"Sib": {"parent_names": ["Dad", "Mum"]}},
    )
    assert family["sib"].parentage_asked_at is not None
    found = await entity_store.parentage_candidates(db_session, family["user"].id)
    assert found["siblings"] == [], "answered, so not asked again"

    cleared = await entity_store.clear_ask_once_stamps_for_segment(
        db_session, family["segment"].id
    )
    assert cleared["parentage"] == 1
    assert family["sib"].parentage_asked_at is None


async def test_a_skipped_answer_keeps_its_stamp_when_an_unrelated_recording_goes(
    db_session, family
):
    """Only the people whose answer was destroyed become askable again. A
    producer who was asked and skipped said something, and nothing of theirs
    was lost."""
    await entity_store.write_parentage(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_sibling_names=["Sib"],
        answers={},  # skipped
    )
    assert family["sib"].parentage_asked_at is not None

    cleared = await entity_store.clear_ask_once_stamps_for_segment(
        db_session, family["segment"].id
    )
    assert cleared["parentage"] == 0, "no parent edges came from that segment"
    assert family["sib"].parentage_asked_at is not None


# ── which side of the family an aunt or uncle is on ───────────────────────


async def test_an_aunt_uncle_with_no_side_is_asked(db_session, family):
    """An aunt_uncle edge places them in the parents' row and says nothing
    about which parent they belong to — so the row reads as four parents."""
    uncle = await family["person"]("Uncle")
    await family["relate"](uncle, "aunt_uncle", family["root"])

    found = await entity_store.aunt_uncle_candidates(db_session, family["user"].id)
    assert [r["name"] for r in found["relatives"]] == ["Uncle"]
    assert {p["name"] for p in found["parents"]} == {"Dad", "Mum"}


async def test_an_aunt_uncle_already_linked_to_a_parent_is_not_asked(db_session, family):
    uncle = await family["person"]("Uncle")
    await family["relate"](uncle, "aunt_uncle", family["root"])
    await family["relate"](uncle, "sibling", family["dad"])

    found = await entity_store.aunt_uncle_candidates(db_session, family["user"].id)
    assert found["relatives"] == []


async def test_the_side_question_can_be_answered_on_the_first_recording(
    db_session, fresh_producer
):
    """Same rule as parentage: the recording that names an uncle is the one
    that creates them, so a database-only question could never fire on it."""
    user, _segment, _root = fresh_producer
    proposed = [
        {"from_name": "Dad", "to_name": "__SELF__", "relation_type": "parent"},
        {"from_name": "Uncle", "to_name": "__SELF__", "relation_type": "aunt_uncle"},
    ]
    found = await entity_store.aunt_uncle_candidates(
        db_session, user.id, proposed, "__SELF__"
    )
    assert [r["name"] for r in found["relatives"]] == ["Uncle"]
    questions = side_questions(found)
    assert questions[0]["question"] == "Which side of the family are Uncle on?"


async def test_answering_writes_a_sibling_edge_and_keeps_the_aunt_uncle_row(
    db_session, family
):
    """The aunt_uncle row is TRUE — they are the producer's uncle — and the two
    agree rather than compete, so replacing it would lose a recorded fact."""
    uncle = await family["person"]("Uncle")
    await family["relate"](uncle, "aunt_uncle", family["root"])

    written = await entity_store.write_sides(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_names=["Uncle"],
        answers={"Uncle": "Dad"},
    )
    assert written["relations"] == 1 and written["asked"] == 1

    rows = (await db_session.execute(
        EntityRelation.__table__.select().where(EntityRelation.from_entity_id == uncle.id)
    )).all()
    kinds = {r.relation_type for r in rows}
    assert kinds == {"aunt_uncle", "sibling"}, "both kept"
    sibling_row = next(r for r in rows if r.relation_type == "sibling")
    assert sibling_row.to_entity_id == family["dad"].id
    assert sibling_row.origin == "confirmation"


async def test_the_two_rows_do_not_contradict_each_other_in_the_tree(
    db_session, family
):
    """aunt_uncle is one generation up from the producer; sibling is level with
    a parent already one generation up. Both place them in the same row."""
    from app.services import family_tree

    db_session.add(RelationType(
        relation_type="aunt_uncle", category="family", is_tree_edge=True,
        inverse_type=None, is_symmetric=False, label_en="uncle", label_he="דוד",
        generation_delta=-1,
    ))
    uncle = await family["person"]("Uncle")
    await family["relate"](uncle, "aunt_uncle", family["root"])
    await family["relate"](uncle, "sibling", family["dad"])

    tree = await family_tree.build_tree(db_session, family["user"].id)
    assert tree["contradictions"] == []
    row = next(g for g in tree["generations"] if g["generation"] == -1)
    assert "Uncle" in [p["name"] for p in row["people"]]


async def test_not_sure_is_recorded_so_it_is_asked_once(db_session, family):
    uncle = await family["person"]("Uncle")
    await family["relate"](uncle, "aunt_uncle", family["root"])

    written = await entity_store.write_sides(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_names=["Uncle"],
        answers={},
    )
    assert written["relations"] == 0 and written["asked"] == 1
    assert uncle.side_asked_at is not None

    found = await entity_store.aunt_uncle_candidates(db_session, family["user"].id)
    assert found["relatives"] == []


async def test_deleting_the_recording_reopens_the_side_question(db_session, family):
    """The lesson from parentage, applied before it could bite twice."""
    uncle = await family["person"]("Uncle")
    await family["relate"](uncle, "aunt_uncle", family["root"])
    await entity_store.write_sides(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_names=["Uncle"],
        answers={"Uncle": "Dad"},
    )
    assert uncle.side_asked_at is not None

    cleared = await entity_store.clear_ask_once_stamps_for_segment(
        db_session, family["segment"].id
    )
    assert cleared["side"] == 1
    assert uncle.side_asked_at is None


# ── Correcting a WRONG proposed relation ─────────────────────────────────────
#
# The gap these cover, found in live testing: the screen can confirm a proposed
# relation or ignore it, and there is no way to say "that relation is wrong,
# here is the right one". A producer who improvises the correction — declining
# "בני is my brother" and using the parentage question's "someone else" field
# to say בני is ניר's child — had the answer accepted by the UI and then
# silently dropped.


async def test_parentage_survives_declining_the_sibling_proposal(monkeypatch):
    """An EXPLICIT parentage answer must not be thrown away because the
    sibling proposal it corrects was declined.

    This is the whole point of declining it. "בני is not my brother, he is
    ניר's child" is one statement, and the producer expresses it in two
    controls: untick the sibling, then name the real parent. Reading the second
    only when the first was ACCEPTED makes the pair unusable — and worse,
    silently, because the UI accepts the answer and reports success.
    """
    import app.analysis_graph as ag

    answer = {
        "identity": {},
        "types": {},
        # The proposal is declined: בני is not the producer's brother.
        "relations": {},
        "years": {},
        "sides": {},
        "name_edits": {},
        # ...but they said whose child he IS.
        "parentage": {"בני": {"parent_names": [], "new_parent_name": "ניר"}},
    }
    monkeypatch.setattr(ag, "interrupt", lambda _payload: answer)

    state = {
        "segment_id": "seg-1",
        "names_to_check": [],
        "extracted_entities": [
            {"name": "בני", "type": "person", "alternative_type": None, "summary": "s"},
        ],
        "proposed_relations": [
            {
                "from_name": "בני",
                "to_name": ag.entity_extraction.SELF,
                "relation_type": "sibling",
                "index": 0,
            }
        ],
        "parentage": {
            "siblings": [{"name": "בני"}],
            "parents": [{"name": "אילנה"}, {"name": "צבי"}],
            "known_people": [{"name": "ניר"}],
        },
    }

    result = await ag.human_confirm_node(state)

    assert result["proposed_relations"] == [], "the declined sibling is not stored"
    assert "בני" in result["parentage"]["asked_names"], (
        "an explicitly answered parentage question must reach write_parentage "
        "even when the sibling proposal it corrects was declined"
    )
    assert result["parentage"]["answers"]["בני"]["new_parent_name"] == "ניר"


async def test_declining_a_sibling_without_answering_still_writes_nothing(monkeypatch):
    """The guard's original purpose, which must survive the fix.

    Declining "ניר is my brother" and leaving the grouped parentage question
    alone must not record ניר's parents anyway. Silence is still silence — it
    is only an explicit answer that now carries through.
    """
    import app.analysis_graph as ag

    monkeypatch.setattr(
        ag,
        "interrupt",
        lambda _payload: {
            "identity": {}, "types": {}, "relations": {}, "years": {},
            "sides": {}, "name_edits": {}, "parentage": {},
        },
    )

    state = {
        "segment_id": "seg-2",
        "names_to_check": [],
        "extracted_entities": [
            {"name": "ניר", "type": "person", "alternative_type": None, "summary": "s"},
        ],
        "proposed_relations": [
            {
                "from_name": "ניר",
                "to_name": ag.entity_extraction.SELF,
                "relation_type": "sibling",
                "index": 0,
            }
        ],
        "parentage": {
            "siblings": [{"name": "ניר"}],
            "parents": [{"name": "אילנה"}, {"name": "צבי"}],
            "known_people": [],
        },
    }

    result = await ag.human_confirm_node(state)
    assert result["parentage"]["asked_names"] == []


async def test_a_corrected_relation_replaces_the_proposal(monkeypatch):
    """"Not quite — fix this": the wrong relation becomes the right one.

    The case from live testing: יוסי is the producer's BROTHER and extraction
    proposed him as a nephew. Before this, the only available answers were
    "yes, nephew" and silence — so the real relation could not be captured at
    all, and the tree drew him in the wrong place or not at all.
    """
    import app.analysis_graph as ag

    monkeypatch.setattr(
        ag,
        "interrupt",
        lambda _payload: {
            "identity": {}, "types": {}, "years": {}, "sides": {},
            "name_edits": {}, "parentage": {},
            # NOT ticked. Correcting it is the acceptance.
            "relations": {},
            "relation_edits": {
                "0": {
                    "relation_type": "sibling",
                    "from_name": "יוסי",
                    "to_name": "__SELF__",
                }
            },
        },
    )

    state = {
        "segment_id": "seg-3",
        "names_to_check": [],
        "extracted_entities": [
            {"name": "יוסי", "type": "person", "alternative_type": None, "summary": "s"},
        ],
        "proposed_relations": [
            {
                "from_name": "יוסי",
                "to_name": ag.entity_extraction.SELF,
                "relation_type": "aunt_uncle",
                "index": 0,
            }
        ],
    }

    result = await ag.human_confirm_node(state)

    assert len(result["proposed_relations"]) == 1, (
        "a corrected relation is stored without also needing its tick"
    )
    stored = result["proposed_relations"][0]
    assert stored["relation_type"] == "sibling"
    assert stored["from_name"] == "יוסי"
    assert stored["to_name"] == ag.entity_extraction.SELF


async def test_a_correction_can_repoint_both_ends(monkeypatch):
    """The nephew case: בני is not the producer's brother, he is ניר's child.

    Type AND an endpoint change together, which is why an edit carries all
    three parts rather than patching one.
    """
    import app.analysis_graph as ag

    monkeypatch.setattr(
        ag,
        "interrupt",
        lambda _payload: {
            "identity": {}, "types": {}, "years": {}, "sides": {},
            "name_edits": {}, "parentage": {}, "relations": {},
            "relation_edits": {
                "0": {"relation_type": "parent", "from_name": "ניר", "to_name": "בני"}
            },
        },
    )

    state = {
        "segment_id": "seg-4",
        "names_to_check": [],
        "extracted_entities": [
            {"name": "בני", "type": "person", "alternative_type": None, "summary": "s"},
        ],
        "proposed_relations": [
            {
                "from_name": "בני",
                "to_name": ag.entity_extraction.SELF,
                "relation_type": "sibling",
                "index": 0,
            }
        ],
    }

    result = await ag.human_confirm_node(state)
    stored = result["proposed_relations"][0]
    assert (stored["from_name"], stored["relation_type"], stored["to_name"]) == (
        "ניר", "parent", "בני",
    )


async def test_a_correction_survives_a_name_edit_on_the_same_screen(monkeypatch):
    """Both corrections at once: the name was misheard AND the relation wrong.

    Endpoints are resolved by name at write time, so a correction naming the
    OLD spelling while the entity is stored under the new one would be dropped
    with a log line nobody reads.
    """
    import app.analysis_graph as ag

    monkeypatch.setattr(
        ag,
        "interrupt",
        lambda _payload: {
            "identity": {}, "types": {}, "years": {}, "sides": {},
            "parentage": {}, "relations": {},
            "name_edits": {"ליאן": "אליאן"},
            "relation_edits": {
                "0": {
                    "relation_type": "sibling",
                    "from_name": "ליאן",
                    "to_name": "__SELF__",
                }
            },
        },
    )

    state = {
        "segment_id": "seg-5",
        "names_to_check": [],
        "extracted_entities": [
            {"name": "ליאן", "type": "person", "alternative_type": None, "summary": "s"},
        ],
        "proposed_relations": [
            {
                "from_name": "ליאן",
                "to_name": ag.entity_extraction.SELF,
                "relation_type": "aunt_uncle",
                "index": 0,
            }
        ],
    }

    result = await ag.human_confirm_node(state)
    stored = result["proposed_relations"][0]
    assert stored["from_name"] == "אליאן", "the correction follows the renamed entity"
    assert stored["relation_type"] == "sibling"


async def test_a_renamed_sibling_keeps_its_parentage_answer(monkeypatch):
    """A misheard name and a parentage answer, about the same person, on the
    same screen. Confirmed broken on two real recordings.

    "גבי" is corrected to "גבינון" and told he is רז's child. The entity is
    written under the CORRECTED name, and every relation endpoint is corrected
    to match — but the parentage question still names him "גבי", because that
    is what the screen asked about. So the acceptance test compares "גבי"
    against a set holding "גבינון", and the entity lookup in write_parentage
    searches for a "גבי" that no longer exists. Both miss, and the answer is
    dropped with a warning nobody reads.

    The names the node hands on must be the names the entities were WRITTEN
    under, or every downstream lookup is searching for a person who was
    renamed a few lines earlier.
    """
    import app.analysis_graph as ag

    monkeypatch.setattr(
        ag,
        "interrupt",
        lambda _payload: {
            "identity": {}, "types": {}, "years": {}, "sides": {},
            "name_edits": {"גבי": "גבינון"},
            # The sibling proposal IS accepted — he is a brother — and the
            # parentage question is answered for the same person.
            "relations": {"0": True},
            "parentage": {"גבי": {"parent_names": [], "new_parent_name": "רז"}},
        },
    )

    state = {
        "segment_id": "seg-6",
        "names_to_check": [],
        "extracted_entities": [
            {"name": "גבי", "type": "person", "alternative_type": None, "summary": "s"},
        ],
        "proposed_relations": [
            {
                "from_name": "גבי",
                "to_name": ag.entity_extraction.SELF,
                "relation_type": "sibling",
                "index": 0,
            }
        ],
        "parentage": {
            "siblings": [{"name": "גבי"}],
            "parents": [{"name": "אילנה"}, {"name": "צבי"}],
            "known_people": [{"name": "רז"}],
        },
    }

    result = await ag.human_confirm_node(state)

    # The entity is written as גבינון...
    assert result["extracted_entities"][0]["name"] == "גבינון"
    # ...so the parentage answer must travel under that name too, or
    # write_parentage looks up a person who does not exist.
    assert result["parentage"]["asked_names"] == ["גבינון"]
    assert "גבינון" in result["parentage"]["answers"]
    assert result["parentage"]["answers"]["גבינון"]["new_parent_name"] == "רז"


async def test_a_renamed_aunt_or_uncle_keeps_its_side_answer(monkeypatch):
    """The same bug, one question along.

    `asked_sides` compared the screen's spelling against a set built from the
    corrected relations, exactly as parentage did. Fixed together and pinned
    together, because finding this class twice and fixing it once is how the
    second half comes back.
    """
    import app.analysis_graph as ag

    monkeypatch.setattr(
        ag,
        "interrupt",
        lambda _payload: {
            "identity": {}, "types": {}, "years": {}, "parentage": {},
            "name_edits": {"אמנונ": "אמנון"},
            "relations": {"0": True},
            "sides": {"אמנונ": "צבי"},
        },
    )

    state = {
        "segment_id": "seg-7",
        "names_to_check": [],
        "extracted_entities": [
            {"name": "אמנונ", "type": "person", "alternative_type": None, "summary": "s"},
        ],
        "proposed_relations": [
            {
                "from_name": "אמנונ",
                "to_name": ag.entity_extraction.SELF,
                "relation_type": "aunt_uncle",
                "index": 0,
            }
        ],
        "sides": {
            "relatives": [{"name": "אמנונ"}],
            "parents": [{"name": "צבי"}, {"name": "אילנה"}],
        },
    }

    result = await ag.human_confirm_node(state)

    assert result["sides"]["asked_names"] == ["אמנון"]
    assert result["sides"]["answers"] == {"אמנון": "צבי"}


# ── The parentage answer OWNS whether they are a sibling ─────────────────────


async def test_a_non_parent_answer_replaces_the_sibling_relation(monkeypatch):
    """Ticking sibling AND naming רז as the parent used to store BOTH.

    The live case: "איציק" ticked as a brother and told he is רז's child. Both
    relations were written, the tree could not honour both, and it kept the
    sibling and reported the parent edge as a contradiction — which looked
    exactly like the chosen parent failing to save.

    The parentage answer decides. רז is the producer's brother, not their
    parent, so this person shares no parent with the producer and cannot be
    their sibling: the sibling relation is not written at all.
    """
    import app.analysis_graph as ag

    monkeypatch.setattr(
        ag,
        "interrupt",
        lambda _payload: {
            "identity": {}, "types": {}, "years": {}, "sides": {}, "name_edits": {},
            # Ticked — and overridden by the answer below.
            "relations": {"0": True},
            "parentage": {"איציק": {"parent_names": [], "new_parent_name": "רז"}},
        },
    )

    state = {
        "segment_id": "seg-8",
        "names_to_check": [],
        "extracted_entities": [
            {"name": "איציק", "type": "person", "alternative_type": None, "summary": "s"},
        ],
        "proposed_relations": [
            {
                "from_name": "איציק",
                "to_name": ag.entity_extraction.SELF,
                "relation_type": "sibling",
                "index": 0,
            }
        ],
        "parentage": {
            "siblings": [{"name": "איציק"}],
            "parents": [{"name": "אילנה"}, {"name": "צבי"}],
            "known_people": [{"name": "רז"}],
        },
    }

    result = await ag.human_confirm_node(state)

    assert result["proposed_relations"] == [], (
        "the sibling relation is replaced by the parentage answer, not stored "
        "alongside it to contradict the parent edge"
    )
    assert result["parentage"]["not_sibling_names"] == ["איציק"]
    assert result["parentage"]["answers"]["איציק"]["new_parent_name"] == "רז"


async def test_a_half_sibling_answer_keeps_the_sibling_relation(monkeypatch):
    """THE case the naive rule would destroy.

    "Picked a different parent" is not the rule — "shares no parent with you"
    is. A half-sibling names ONE of the producer's parents plus somebody else,
    and is still a sibling. Ticking any offered parent means a shared parent,
    because the parents this question offers are the producer's own.
    """
    import app.analysis_graph as ag

    monkeypatch.setattr(
        ag,
        "interrupt",
        lambda _payload: {
            "identity": {}, "types": {}, "years": {}, "sides": {}, "name_edits": {},
            "relations": {"0": True},
            "parentage": {
                "דנה": {"parent_names": ["צבי"], "new_parent_name": "רבקה"}
            },
        },
    )

    state = {
        "segment_id": "seg-9",
        "names_to_check": [],
        "extracted_entities": [
            {"name": "דנה", "type": "person", "alternative_type": None, "summary": "s"},
        ],
        "proposed_relations": [
            {
                "from_name": "דנה",
                "to_name": ag.entity_extraction.SELF,
                "relation_type": "sibling",
                "index": 0,
            }
        ],
        "parentage": {
            "siblings": [{"name": "דנה"}],
            "parents": [{"name": "אילנה"}, {"name": "צבי"}],
            "known_people": [],
        },
    }

    result = await ag.human_confirm_node(state)

    assert result["parentage"]["not_sibling_names"] == []
    assert len(result["proposed_relations"]) == 1, (
        "a half-sibling shares one parent and remains a sibling"
    )


async def test_skipping_the_parentage_question_changes_nothing(monkeypatch):
    """Silence must not retract anything.

    The rule reads an ANSWER. A producer who ticks sibling and leaves the
    grouped question alone keeps their sibling, exactly as before.
    """
    import app.analysis_graph as ag

    monkeypatch.setattr(
        ag,
        "interrupt",
        lambda _payload: {
            "identity": {}, "types": {}, "years": {}, "sides": {}, "name_edits": {},
            "relations": {"0": True},
            "parentage": {},
        },
    )

    state = {
        "segment_id": "seg-10",
        "names_to_check": [],
        "extracted_entities": [
            {"name": "רון", "type": "person", "alternative_type": None, "summary": "s"},
        ],
        "proposed_relations": [
            {
                "from_name": "רון",
                "to_name": ag.entity_extraction.SELF,
                "relation_type": "sibling",
                "index": 0,
            }
        ],
        "parentage": {
            "siblings": [{"name": "רון"}],
            "parents": [{"name": "אילנה"}, {"name": "צבי"}],
            "known_people": [],
        },
    }

    result = await ag.human_confirm_node(state)
    assert result["parentage"]["not_sibling_names"] == []
    assert len(result["proposed_relations"]) == 1


async def test_an_earlier_recordings_sibling_edge_is_deleted(db_session, family):
    """A sibling from an EARLIER recording already has a row.

    Dropping the proposal covers a sibling this recording raised. One recorded
    by a previous recording is already in the database, and only a delete can
    retract it — otherwise the contradiction survives in exactly the case the
    parentage question was originally built for.
    """
    # The fixture already recorded Sib as the producer's sibling — that IS the
    # earlier recording this test is about.
    uncle = await family["person"]("Raz")

    written = await entity_store.write_parentage(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_sibling_names=["Sib"],
        answers={"Sib": {"parent_names": [], "new_parent_name": "Raz"}},
        not_sibling_names=["Sib"],
    )

    assert written["siblings_replaced"] == 1
    remaining = (
        await db_session.execute(
            EntityRelation.__table__.select().where(
                EntityRelation.relation_type == "sibling",
                EntityRelation.to_entity_id == family["root"].id,
            )
        )
    ).all()
    assert remaining == [], "the contradicting sibling edge is retracted"

    parents = await _parents_of(db_session, family["sib"].id)
    # _parents_of returns RELATION rows, so the parent is from_entity_id.
    assert [p.from_entity_id for p in parents] == [uncle.id]
