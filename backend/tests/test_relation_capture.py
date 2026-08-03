"""
Phase 2 of docs/FAMILY_TREE_TIMELINE.md — relation capture.

Two properties get the most attention, because both fail silently:

  * DIRECTION. `from` is the subject: "צבי is my father" must store
    (צבי, parent, self), meaning צבי is the parent OF the speaker. Backwards,
    every generation in the tree inverts and the page still renders perfectly
    with all the right names in it — which is why the plan insisted this be
    asserted on the stored ROW rather than on "a parent row exists".
  * NOTHING IS APPLIED WITHOUT CONFIRMATION. A proposal that nobody accepted
    must leave the archive exactly as it was.
"""

import pytest
from sqlalchemy import select

from app.models import Entity, EntityRelation, InterviewSession, RawSegment, User
from app.services import entity_extraction as ex
from app.services import entity_store
from app.services.entity_extraction import ExtractedEntity, ExtractedRelation

FAMILY = ["parent", "child", "sibling", "spouse", "grandparent", "grandchild"]


# ── parsing ───────────────────────────────────────────────────────────────


def _reply(entities_json: str, relations_json: str) -> str:
    return f"{entities_json}\nRELATIONS:\n{relations_json}"


def test_the_two_arrays_do_not_swallow_each_other():
    """The entity regex is greedy. Before the split, a reply with a second
    array parsed as neither and returned ZERO entities from a good
    extraction — silently, since an empty extraction is a legitimate result."""
    raw = _reply(
        '[{"name":"ניר","type":"person","summary":"אח"}]',
        '[{"from":"ניר","to":"__SELF__","type":"sibling"}]',
    )
    entities = ex.parse_extracted_entities(raw)
    assert [e.name for e in entities] == ["ניר"]
    assert len(ex.parse_extracted_relations(raw, ["ניר"], FAMILY)) == 1


def test_direction_is_preserved_exactly_as_stated():
    raw = _reply(
        '[{"name":"צבי","type":"person"},{"name":"מיה","type":"person"}]',
        '[{"from":"צבי","to":"__SELF__","type":"parent","evidence":"צבי הוא אבא שלי"},'
        ' {"from":"מיה","to":"__SELF__","type":"child"}]',
    )
    rels = {r.from_name: r for r in ex.parse_extracted_relations(raw, ["צבי", "מיה"], FAMILY)}
    # צבי is the PARENT OF the speaker — not the speaker's child
    assert rels["צבי"].relation_type == "parent"
    assert rels["צבי"].to_name == ex.SELF
    assert rels["מיה"].relation_type == "child"


def test_drops_a_relation_whose_endpoint_was_not_extracted():
    """It would point at an entity row that never gets created."""
    raw = _reply(
        '[{"name":"ניר","type":"person"}]',
        '[{"from":"מישהו","to":"__SELF__","type":"sibling"}]',
    )
    assert ex.parse_extracted_relations(raw, ["ניר"], FAMILY) == []


def test_drops_a_type_outside_the_offered_vocabulary():
    """The FK would reject it anyway; failing here names the value in a log
    instead of aborting a write."""
    raw = _reply(
        '[{"name":"ניר","type":"person"}]',
        '[{"from":"ניר","to":"__SELF__","type":"brother-ish"}]',
    )
    assert ex.parse_extracted_relations(raw, ["ניר"], FAMILY) == []


def test_drops_self_loops_and_duplicates():
    raw = _reply(
        '[{"name":"ניר","type":"person"}]',
        '[{"from":"__SELF__","to":"__SELF__","type":"sibling"},'
        ' {"from":"ניר","to":"__SELF__","type":"sibling"},'
        ' {"from":"ניר ","to":"__SELF__","type":"sibling"}]',
    )
    rels = ex.parse_extracted_relations(raw, ["ניר"], FAMILY)
    assert len(rels) == 1, "self-loop dropped, and the whitespace variant is the same relation"


def test_endpoints_match_on_the_merge_key_not_the_raw_string():
    """A trailing space must still resolve, or the relation would be dropped
    while the entity it names is written fine."""
    raw = _reply(
        '[{"name":"ניר","type":"person"}]',
        '[{"from":" ניר ","to":"__SELF__","type":"sibling"}]',
    )
    assert len(ex.parse_extracted_relations(raw, ["ניר"], FAMILY)) == 1


def test_no_relations_array_is_not_an_error():
    """Entities-only replies are the norm for most recordings."""
    raw = '[{"name":"ניר","type":"person"}]'
    assert ex.parse_extracted_relations(raw, ["ניר"], FAMILY) == []


def test_prompt_carries_the_vocabulary_and_is_unchanged_without_it():
    assert ex.build_extraction_prompt([]) == ex._ENTITY_EXTRACTION_SYSTEM_PROMPT
    prompt = ex.build_extraction_prompt(FAMILY)
    for t in FAMILY:
        assert t in prompt
    assert "__SELF__" in prompt


# ── writing ───────────────────────────────────────────────────────────────


@pytest.fixture
async def seeded_relation_types(db_session):
    """Seed relation_types the way migration 0012 does.

    Tests build the schema with Base.metadata.create_all, which creates the
    table but runs none of the migration's seed data — so without this the
    vocabulary is empty and relation capture is silently off. Only the family
    types Phase 2 uses, plus one non-family row to prove the category filter
    actually filters.
    """
    from app.models import RelationType

    rows = [
        ("parent", "family", True, "child", False),
        ("child", "family", True, "parent", False),
        ("sibling", "family", True, None, True),
        ("spouse", "family", True, None, True),
        ("grandparent", "family", True, "grandchild", False),
        ("grandchild", "family", True, "grandparent", False),
        ("commander", "professional", False, "subordinate", False),
    ]
    for rt, cat, tree, inverse, symmetric in rows:
        db_session.add(
            RelationType(
                relation_type=rt, category=cat, is_tree_edge=tree,
                inverse_type=inverse, is_symmetric=symmetric,
                label_en=rt, label_he=rt,
            )
        )
    await db_session.flush()


@pytest.fixture
async def archive(db_session):
    """A producer with a self-entity and one recording."""
    user = User(
        id="u-rel", email="r@example.com", username="rel",
        hashed_password="x", role="producer", full_name="Tal",
    )
    db_session.add(user)
    await db_session.flush()
    await entity_store.ensure_self_entity(db_session, user)

    session = InterviewSession(user_id=user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    segment = RawSegment(
        interview_session_id=session.id, question_asked="q",
        question_index=0, question_id="childhood_q01", status="ready",
    )
    db_session.add(segment)
    await db_session.flush()
    return user, segment


async def _write(db, user, segment, names, relations):
    await entity_store.write_segment_entities(
        db,
        segment_id=segment.id,
        producer_id=user.id,
        entities=[ExtractedEntity(name=n, type="person") for n in names],
    )
    return await entity_store.write_segment_relations(
        db,
        segment_id=segment.id,
        producer_id=user.id,
        relations=relations,
        self_marker=ex.SELF,
    )


async def test_a_stored_relation_points_the_way_it_was_stated(db_session, archive):
    """The direction test the plan demanded — asserted on the ROW, resolved to
    real entities, not merely on a parent row existing."""
    user, segment = archive
    await _write(
        db_session, user, segment, ["צבי"],
        [ExtractedRelation(from_name="צבי", to_name=ex.SELF, relation_type="parent")],
    )

    row = (await db_session.execute(select(EntityRelation))).scalars().one()
    src = await db_session.get(Entity, row.from_entity_id)
    dst = await db_session.get(Entity, row.to_entity_id)

    assert row.relation_type == "parent"
    assert src.name == "צבי", "the PARENT must be the from-end"
    assert dst.is_self, "the speaker must be the to-end"
    assert row.source_segment_id == segment.id


async def test_two_siblings_are_two_rows(db_session, archive):
    """'ניר ורז הם אחים שלי' — two proposals, indistinguishable by name+type,
    which is why the confirmation screen keys them by index."""
    user, segment = archive
    n = await _write(
        db_session, user, segment, ["ניר", "רז"],
        [
            ExtractedRelation(from_name="ניר", to_name=ex.SELF, relation_type="sibling"),
            ExtractedRelation(from_name="רז", to_name=ex.SELF, relation_type="sibling"),
        ],
    )
    assert n == 2
    assert len((await db_session.execute(select(EntityRelation))).scalars().all()) == 2


async def test_only_one_directed_row_is_stored(db_session, archive):
    """No mirror row. The inverse is derived at read time from
    relation_types.inverse_type; two rows would need keeping in sync and would
    eventually disagree."""
    user, segment = archive
    await _write(
        db_session, user, segment, ["צבי"],
        [ExtractedRelation(from_name="צבי", to_name=ex.SELF, relation_type="parent")],
    )
    rows = (await db_session.execute(select(EntityRelation))).scalars().all()
    assert len(rows) == 1


async def test_re_analysis_replaces_rather_than_appends(db_session, archive):
    """Same shape as mentions: a relation must not outlive the sentence that
    established it."""
    user, segment = archive
    rel = [ExtractedRelation(from_name="ניר", to_name=ex.SELF, relation_type="sibling")]
    await _write(db_session, user, segment, ["ניר"], rel)
    await _write(db_session, user, segment, ["ניר"], rel)
    assert len((await db_session.execute(select(EntityRelation))).scalars().all()) == 1


async def test_an_unresolvable_endpoint_is_skipped_not_fatal(db_session, archive):
    """The recording and its entities are already saved; losing one proposed
    relation costs less than losing the segment."""
    user, segment = archive
    n = await _write(
        db_session, user, segment, ["ניר"],
        [
            ExtractedRelation(from_name="ניר", to_name=ex.SELF, relation_type="sibling"),
            ExtractedRelation(from_name="לא-קיים", to_name=ex.SELF, relation_type="sibling"),
        ],
    )
    assert n == 1


def test_the_relation_fk_is_declared_to_cascade():
    """A relation must not outlive the recording that established it.

    Asserted on the DECLARATION, not by deleting a row: the test database is
    SQLite without `PRAGMA foreign_keys=ON`, so it does not enforce ON DELETE
    at all and a passing delete-test would prove nothing. The live constraint
    was verified directly against Postgres when migration 0012 was applied.
    """
    fk = next(iter(EntityRelation.__table__.c.source_segment_id.foreign_keys))
    assert fk.ondelete == "CASCADE"


async def test_the_vocabulary_comes_from_the_table(db_session, seeded_relation_types):
    """Not a hardcoded list — relation_types is the source, and the FK on
    entity_relations is what enforces it."""
    vocab = await entity_store.get_relation_vocabulary(db_session, "family")
    assert "sibling" in vocab and "parent" in vocab
    assert "commander" not in vocab, "Phase 2 is family-only"
    assert "brother-ish" not in vocab


async def test_an_unseeded_vocabulary_is_reported_not_silent(db_session, caplog):
    """A create_all database has no relation_types rows, which turns relation
    capture off with nothing to show for it. That has to be visible."""
    import logging

    with caplog.at_level(logging.WARNING):
        assert await entity_store.get_relation_vocabulary(db_session, "family") == []
    assert any("relation capture is disabled" in r.message for r in caplog.records)


# ── confirmed type answers (the silent no-op found on real input) ─────────


async def test_a_producer_answer_overrides_an_existing_type(db_session, archive):
    """Observed live: the screen asked "place or organisation?", the producer
    chose place, the entity stayed organisation, and nothing said so.

    The rule kept an existing type against a LATER EXTRACTION, which is right —
    ingest order must not decide. But its own reason was that a disagreement
    "is a question for the producer", and this is the producer answering it.
    """
    user, segment = archive
    await entity_store.write_segment_entities(
        db_session, segment_id=segment.id, producer_id=user.id,
        entities=[ExtractedEntity(name="הכפר הירוק", type="organisation")],
    )

    result = await entity_store.write_segment_entities(
        db_session, segment_id=segment.id, producer_id=user.id,
        entities=[
            ExtractedEntity(name="הכפר הירוק", type="place", type_confirmed=True)
        ],
    )

    ent = (
        await db_session.execute(select(Entity).where(Entity.name == "הכפר הירוק"))
    ).scalars().one()
    assert ent.type == "place", "the producer's answer must take effect"
    assert result.type_changes == [("הכפר הירוק", "organisation", "place")]


async def test_an_extractor_guess_still_does_not_override(db_session, archive):
    """Unchanged: whichever recording was ingested last must not decide."""
    user, segment = archive
    await entity_store.write_segment_entities(
        db_session, segment_id=segment.id, producer_id=user.id,
        entities=[ExtractedEntity(name="הכפר הירוק", type="organisation")],
    )
    result = await entity_store.write_segment_entities(
        db_session, segment_id=segment.id, producer_id=user.id,
        entities=[ExtractedEntity(name="הכפר הירוק", type="place")],  # not confirmed
    )
    ent = (
        await db_session.execute(select(Entity).where(Entity.name == "הכפר הירוק"))
    ).scalars().one()
    assert ent.type == "organisation"
    assert result.type_changes == [], "nothing changed, so nothing to report"


async def test_a_confirmed_answer_can_never_retype_the_self_entity(db_session, archive):
    """ck_entities_self_is_person would reject it, and a transcript naming the
    producer must not be able to turn them into a place however the answer
    arrived."""
    user, segment = archive
    result = await entity_store.write_segment_entities(
        db_session, segment_id=segment.id, producer_id=user.id,
        entities=[ExtractedEntity(name="Tal", type="place", type_confirmed=True)],
    )
    self_ent = (
        await db_session.execute(
            select(Entity).where(Entity.producer_id == user.id, Entity.is_self)
        )
    ).scalars().one()
    assert self_ent.type == "person"
    assert result.type_changes == []


async def test_filling_in_other_is_reported_too(db_session, archive):
    """Not a conflict, but still a change the producer may want to see."""
    user, segment = archive
    await entity_store.write_segment_entities(
        db_session, segment_id=segment.id, producer_id=user.id,
        entities=[ExtractedEntity(name="טבריה", type="other")],
    )
    result = await entity_store.write_segment_entities(
        db_session, segment_id=segment.id, producer_id=user.id,
        entities=[ExtractedEntity(name="טבריה", type="place")],
    )
    assert result.type_changes == [("טבריה", "other", "place")]


def test_the_confirmed_flag_survives_the_state_round_trip():
    """It crosses a checkpoint boundary as a dict and may sit there for days —
    losing it would silently restore the discard."""
    e = ExtractedEntity(name="x", type="place", type_confirmed=True)
    assert ExtractedEntity.from_dict(e.as_dict()).type_confirmed is True
    assert ExtractedEntity.from_dict(ExtractedEntity(name="x").as_dict()).type_confirmed is False
