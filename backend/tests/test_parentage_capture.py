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

from app.analysis_graph import parentage_questions
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


def test_the_question_offers_every_recorded_parent():
    questions = parentage_questions(
        {
            "parents": [{"id": "p1", "name": "Dad"}, {"id": "p2", "name": "Mum"}],
            "siblings": [{"id": "s1", "name": "Sib"}],
        }
    )
    assert len(questions) == 1
    assert questions[0]["entity_id"] == "s1"
    # Both, so a half-sibling can be recorded as sharing only one.
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
        asked_sibling_ids=[family["sib"].id],
        answers={
            family["sib"].id: {
                "parent_ids": [family["dad"].id, family["mum"].id],
                "new_parent_name": None,
            }
        },
    )
    assert written["relations"] == 2
    assert len(await _parents_of(db_session, family["sib"].id)) == 2


async def test_a_half_sibling_can_share_only_one_parent(db_session, family):
    """The case a yes/no could not express, and the reason this is checkboxes."""
    written = await entity_store.write_parentage(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_sibling_ids=[family["sib"].id],
        answers={
            family["sib"].id: {
                "parent_ids": [family["dad"].id],
                "new_parent_name": "Rivka",
            }
        },
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
        asked_sibling_ids=[family["sib"].id],
        answers={},
    )
    assert written["relations"] == 0 and written["asked"] == 1
    assert family["sib"].parentage_asked_at is not None

    found = await entity_store.parentage_candidates(db_session, family["user"].id)
    assert found["siblings"] == [], "asked once, never again"


async def test_a_parent_id_that_was_never_offered_is_ignored(db_session, family):
    """Defence in depth — the API rejects this outright. The store must not
    attach a stranger as somebody's parent even if it is called directly."""
    stranger = await family["person"]("Stranger")
    written = await entity_store.write_parentage(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_sibling_ids=[family["sib"].id],
        answers={family["sib"].id: {"parent_ids": [stranger.id]}},
    )
    assert written["relations"] == 0
    assert await _parents_of(db_session, family["sib"].id) == []


async def test_answers_are_marked_as_coming_from_a_screen(db_session, family):
    """`origin` exists so the tree does not offer to play a recording that
    never mentioned this person."""
    await entity_store.write_parentage(
        db_session,
        producer_id=family["user"].id,
        segment_id=family["segment"].id,
        asked_sibling_ids=[family["sib"].id],
        answers={family["sib"].id: {"parent_ids": [family["dad"].id]}},
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
        asked_sibling_ids=[family["sib"].id],
        answers={family["sib"].id: {"parent_ids": [family["dad"].id]}},
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
        asked_sibling_ids=[family["sib"].id],
        answers={
            family["sib"].id: {"parent_ids": [family["dad"].id, family["mum"].id]}
        },
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


def test_a_sibling_is_not_offered_as_their_own_parent():
    questions = parentage_questions(
        {
            "parents": [{"id": "p1", "name": "Dad"}],
            "siblings": [{"id": "s1", "name": "Sib"}, {"id": "s2", "name": "Other"}],
            "known_people": [
                {"id": "s1", "name": "Sib"},
                {"id": "s2", "name": "Other"},
                {"id": "x", "name": "Rivka"},
            ],
        }
    )
    by_name = {q["name"]: q for q in questions}
    assert [p["name"] for p in by_name["Sib"]["known_people"]] == ["Other", "Rivka"]
    assert [p["name"] for p in by_name["Other"]["known_people"]] == ["Sib", "Rivka"]


def test_known_people_is_nested_so_it_can_never_be_counted_as_questions():
    """The client counts every array in the payload to decide whether to
    render. A top-level list of people would be counted as questions — the
    miscounting that hid two earlier bugs. Nested, it cannot be."""
    from app.analysis_graph import build_confirmation_payload

    payload = build_confirmation_payload(
        {
            "parentage": {
                "parents": [{"id": "p", "name": "P"}],
                "siblings": [{"id": "s", "name": "S"}],
                "known_people": [{"id": "k", "name": "K"}],
            }
        }
    )
    assert "known_people" not in payload
    assert len(payload["parentage_questions"]) == 1
    assert payload["parentage_questions"][0]["known_people"] == [{"id": "k", "name": "K"}]
