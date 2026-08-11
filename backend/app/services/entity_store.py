"""Entities in Postgres — the write path, and the reads that replaced the graph.

Replaces `graph_memory` entirely: `add_episode` for the write, and the four
read functions the seven call sites used. Everything here is a plain select,
insert or delete against `entities` and `entity_mentions`; there is no graph
engine, no second store, and no LLM.

The whole audit reduced to TWO read primitives, because `max_hops` was 1 at
every call site — making the Cypher `RELATES_TO*0..0`, which matches only the
origin node. No traversal happened anywhere, so nothing here has to replace
any:

  * "entity names for segment X"      -> `get_segment_entity_names`
  * "segments mentioning entity Y"    -> `find_segments_mentioning[_scored]`

plus two smaller ones: fuzzy candidates for disambiguation
(`get_entity_candidates`, pg_trgm instead of hybrid vector search) and
name+summary for the extraction panel (`get_segment_entities`).

THE ONE RULE THIS ENFORCES: an entity is identified by
UNIQUE (producer_id, normalized_name). A second recording mentioning
מונטריאול adds a MENTION, never a second entity row. Without that, each copy
would look like it had exactly one mention and the deletion safety check
would conclude neither was shared and delete both.

RE-INGEST IS A REPLACE, NOT AN APPEND. Writing a segment's entities first
deletes the mentions it wrote last time. That is what makes re-analysing a
recording idempotent, and it is the same delete-then-insert shape
`create_transcript_chunks_node` already uses for chunks. It also means the
mention count is always "how many recordings mention this", never "how many
times we happened to run the pipeline".

NOTHING HERE COMMITS. The caller commits, so that the entity write and the
segment's `status = 'ready'` land in ONE transaction. A half-written entity
set sitting behind a segment marked ready is precisely the drift this
migration exists to remove — it would be indistinguishable from a recording
that genuinely mentioned nobody.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Entity,
    EntityMention,
    EntityRelation,
    InterviewSession,
    RawSegment,
    RelationType,
    User,
)
from app.services.entity_extraction import ExtractedEntity, ExtractedRelation
from app.services.entity_names import normalize_entity_name

logger = logging.getLogger(__name__)


@dataclass
class EntityWriteResult:
    entities_created: int = 0
    entities_matched: int = 0
    mentions_written: int = 0
    orphans_removed: int = 0
    # Entities that STILL carry an unanswered type question by the time they
    # are written. Expected to be EMPTY on the normal path: human_confirm runs
    # before finalize_ingest and clears `alternative_type` on everything it
    # asked about, so anything here means a torn classification reached storage
    # without the producer being asked — the failure this reports is that the
    # question was skipped, not that it exists. Logged rather than raised: a
    # wrong `type` is a visibly wrong label the producer can correct, not a
    # reason to fail an ingest whose transcript is already saved.
    needs_confirmation: List[ExtractedEntity] = field(default_factory=list)
    # (name, was, now) for every type the PRODUCER's answer actually changed.
    # Surfaced back through the confirm endpoint so the answer visibly takes
    # effect — the gap this exists to close is an answer that silently did
    # nothing.
    type_changes: List[Tuple[str, str, str]] = field(default_factory=list)


async def get_relation_vocabulary(db: AsyncSession, category: str = "family") -> List[str]:
    """The relation types the extractor may propose, from the TABLE.

    `relation_types` is the source — `entity_relations.relation_type` has a FK
    to it — so the prompt is filled from here rather than carrying its own
    list. Adding a relation type is then a data change, and the FK is the
    backstop: an invented type fails loudly instead of being stored.

    Scoped to one category because Phase 2 proposes family relations only
    (INTERVIEW-adjacent decision 2.2). Widening it later is passing a different
    category, not a code change here.
    """
    rows = (
        await db.execute(
            select(RelationType.relation_type)
            .where(RelationType.category == category)
            .order_by(RelationType.relation_type)
        )
    ).scalars().all()
    if not rows:
        # An empty vocabulary disables relation proposal entirely, and does it
        # SILENTLY — the prompt simply reverts to entities-only and nothing
        # ever looks wrong. The rows are seeded by migration 0012, so this
        # means a database built by Base.metadata.create_all instead of by
        # migrations (a fresh local dev DB, or a test fixture that forgot to
        # seed). Say so rather than letting the feature quietly not exist.
        logger.warning(
            f"relation_types has no rows for category={category!r} — relation "
            f"capture is disabled. Seeded by migration 0012; a create_all "
            f"database will not have it."
        )
    return list(rows)


async def names_with_year_settled(
    db: AsyncSession, producer_id: str, names: Sequence[str]
) -> set:
    """Normalised names that must NOT be asked for a year again.

    Two distinct reasons, both meaning "settled": the entity already HAS a
    year, or the producer was already ASKED and did not give one. The second
    is why year_asked_at exists — skipping is an answer ("I do not know"), and
    a NULL year alone cannot tell it apart from never having been asked.
    """
    keys = {normalize_entity_name(n) for n in names}
    keys.discard("")
    if not keys:
        return set()
    rows = (
        await db.execute(
            select(Entity.normalized_name)
            .where(
                Entity.producer_id == producer_id,
                Entity.normalized_name.in_(keys),
                (Entity.year_start.isnot(None)) | (Entity.year_asked_at.isnot(None)),
            )
        )
    ).scalars().all()
    return set(rows)


async def write_segment_relations(
    db: AsyncSession,
    *,
    segment_id: str,
    producer_id: str,
    relations: Sequence[ExtractedRelation],
    self_marker: str,
) -> int:
    """Record the family relations this recording established. Returns the count.

    Only ever called with relations the producer CONFIRMED — nothing here
    decides whether to store one. A silent wrong relation is worse than an
    unanswered one, which is why proposal and application are separate steps.

    Replace, don't append, exactly as mentions do: a re-analysis that no longer
    states a relation must not leave the old one behind, or a relation would
    outlive the sentence that established it. `source_segment_id` cascades, so
    deleting the recording removes these too.

    Endpoints are resolved by the SAME merge key the entities were written
    under, so a confirmed rename lands on the row that was actually created.
    An endpoint that does not resolve is skipped with a warning rather than
    failing the ingest — the recording and its entities are already saved, and
    losing one proposed relation costs less than losing the segment.
    """
    # Scoped to origin="recording" on purpose. A re-analysis re-derives what
    # the WORDS said, so it must replace those; it must NOT destroy a parentage
    # answer the producer typed on the confirmation screen, which no amount of
    # re-reading the transcript would ever produce again.
    await db.execute(
        delete(EntityRelation).where(
            EntityRelation.source_segment_id == segment_id,
            EntityRelation.origin == "recording",
        )
    )
    await db.flush()
    if not relations:
        return 0

    self_entity = (
        await db.execute(
            select(Entity).where(Entity.producer_id == producer_id, Entity.is_self)
        )
    ).scalars().first()

    written = 0
    for rel in relations:
        endpoints = []
        for name in (rel.from_name, rel.to_name):
            if name == self_marker:
                endpoints.append(self_entity)
                continue
            normalized = normalize_entity_name(name)
            endpoints.append(
                await _find_entity(db, producer_id, normalized) if normalized else None
            )

        source, target = endpoints
        if source is None or target is None:
            # The self-entity is the usual culprit: a producer created before
            # the signup hook has no root, so every relation to them is
            # unresolvable. scripts/backfill_self_entities.py repairs that.
            logger.warning(
                f"Skipping relation {rel.from_name!r} -{rel.relation_type}-> "
                f"{rel.to_name!r} on segment {segment_id}: endpoint not found"
            )
            continue
        if source.id == target.id:
            continue  # ck_entity_relations_not_self

        # A HAND-MADE EDGE OUTRANKS THE RECORDING, and this is the guard
        # without which the tree editor silently undoes itself: the delete
        # above is scoped to origin="recording", so a manual edge survives a
        # re-analysis — and then this loop would happily re-add the very
        # relation the producer replaced, leaving both. The producer's later,
        # deliberate statement wins over the words that prompted it.
        if await _manual_edge_exists(db, source.id, target.id):
            logger.info(
                f"Not re-adding {rel.from_name!r} -{rel.relation_type}-> "
                f"{rel.to_name!r}: the producer set this pair by hand"
            )
            continue

        db.add(
            EntityRelation(
                from_entity_id=source.id,
                to_entity_id=target.id,
                relation_type=rel.relation_type,
                source_segment_id=segment_id,
            )
        )
        written += 1

    await db.flush()
    return written


async def write_segment_entities(
    db: AsyncSession,
    *,
    segment_id: str,
    producer_id: str,
    entities: Sequence[ExtractedEntity],
) -> EntityWriteResult:
    """Record what this recording said about whom.

    Flushes but never commits — see the module docstring.
    """
    result = EntityWriteResult()

    # Replace, don't append. Runs even when `entities` is empty: a re-analysis
    # that now finds nobody must leave nobody behind, or a name the producer
    # removed from the transcript would outlive the sentence that named it.
    await db.execute(
        delete(EntityMention).where(EntityMention.raw_segment_id == segment_id)
    )
    await db.flush()

    for extracted in entities:
        normalized = normalize_entity_name(extracted.name)
        if not normalized:
            # A name that normalises to nothing cannot be merged or matched;
            # it would collide with every other blank one on the unique key.
            continue

        entity, created = await _get_or_create_entity(
            db, producer_id=producer_id, extracted=extracted, normalized=normalized,
            result=result,
        )
        if created:
            result.entities_created += 1
        else:
            result.entities_matched += 1

        db.add(
            EntityMention(
                entity_id=entity.id,
                raw_segment_id=segment_id,
                # Segment-level, deliberately. One mention per entity per
                # recording is what the entity map and the deletion safety
                # check both read; chunk-level rows would double-count against
                # a NULL-chunk row for the same pair. Chunk precision already
                # lives on TranscriptChunk.mentioned_entities.
                chunk_id=None,
                summary=extracted.summary,
            )
        )
        result.mentions_written += 1

        if extracted.needs_type_confirmation:
            result.needs_confirmation.append(extracted)

    await db.flush()

    result.orphans_removed = await delete_orphaned_entities(db, producer_id)

    if result.needs_confirmation:
        # WARNING, not INFO: on the normal path human_confirm has already
        # asked and cleared these, so reaching here means the question was
        # skipped and a coin-flip classification was stored unasked.
        logger.warning(
            "entity_type_confirmation_skipped",
            extra={
                "segment_id": segment_id,
                "producer_id": producer_id,
                "entities": [
                    f"{e.name}: {e.type} or {e.alternative_type}"
                    for e in result.needs_confirmation
                ],
            },
        )
    logger.info(
        "segment_entities_written",
        # Key names avoid `name`, `created`, `msg`, `module`, `args` and the
        # rest of LogRecord's own attributes: `extra` overwriting one raises
        # KeyError at log time, which would turn a successful write into a
        # failed ingest from inside the line that was only reporting it.
        extra={
            "segment_id": segment_id,
            "producer_id": producer_id,
            "entities_created": result.entities_created,
            "entities_matched": result.entities_matched,
            "mentions_written": result.mentions_written,
            "orphans_removed": result.orphans_removed,
        },
    )
    return result


async def delete_orphaned_entities(db: AsyncSession, producer_id: str) -> int:
    """Drop this producer's entities that no recording mentions any more.

    This is Graphiti's "remove an entity only when its MENTIONS count is 1"
    rule, restated as something the engine enforces instead of application
    bookkeeping: an entity two recordings mention survives one of them being
    deleted, because the other row is still there.

    `is_self` is EXCLUDED, and that exclusion is load-bearing rather than
    defensive. The producer's own entity is created by migration 0012 with no
    mentions at all — it exists so relations have a root to point at ("I have
    four brothers" needs a node for the "I"). An orphan sweep that did not
    skip it would delete the family tree's root the first time anyone
    re-ingested a recording, and nothing in the UI would show that it had
    happened.

    Scoped to one producer: a global sweep here would be a footgun, since the
    caller only ever knows that ITS producer's mentions are settled.
    """
    orphaned = (
        select(Entity.id)
        .where(Entity.producer_id == producer_id)
        .where(~Entity.is_self)
        .where(
            ~select(EntityMention.id)
            .where(EntityMention.entity_id == Entity.id)
            .exists()
        )
    )
    ids = list((await db.execute(orphaned)).scalars().all())
    if not ids:
        return 0
    await db.execute(delete(Entity).where(Entity.id.in_(ids)))
    await db.flush()
    return len(ids)


# ── Reads ───────────────────────────────────────────────────────────────────


async def get_entity_names_for_segments(
    db: AsyncSession, segment_ids: Sequence[str], producer_id: str
) -> Dict[str, List[str]]:
    """segment_id -> the entity names it mentions, for MANY segments at once.

    ONE query for the whole archive. This is the function the migration was
    mostly for: the graph version had no bulk form, so `_build_entity_map`
    issued a round trip PER RECORDING against a remote free-tier Neo4j — 45%
    of a turn and 100% of the latency variance (1.35s-9.55s across identical
    passes). The per-segment shape was the cost, not the database.

    Segments with no entities are absent from the result rather than mapped to
    an empty list; callers build maps from what is here.
    """
    if not segment_ids:
        return {}
    rows = await db.execute(
        select(EntityMention.raw_segment_id, Entity.name)
        .join(Entity, Entity.id == EntityMention.entity_id)
        .where(EntityMention.raw_segment_id.in_(list(segment_ids)))
        .where(Entity.producer_id == producer_id)
        .distinct()
        .order_by(Entity.name)
    )
    out: Dict[str, List[str]] = {}
    for segment_id, name in rows:
        out.setdefault(segment_id, []).append(name)
    return out


async def get_segment_entity_names(
    db: AsyncSession, segment_id: str, producer_id: str
) -> List[str]:
    """The entity names one segment mentions. Never transcript content."""
    return (await get_entity_names_for_segments(db, [segment_id], producer_id)).get(
        segment_id, []
    )


async def get_segment_entities(
    db: AsyncSession, segment_id: str, producer_id: str
) -> List[Tuple[str, str, str, Optional[str]]]:
    """(entity_id, name, type, summary) for one segment — the extraction panel.

    The summary is THIS recording's, which is the entire point of the panel:
    "ניר: אח של הדובר" shows not just that a name was picked up but what the
    system decided it MEANS, which is where a wrong-but-plausible extraction
    reveals itself. Under the graph this was the entity's single consolidated
    summary, so every recording mentioning it showed the same text.

    The id rides along since the photo work (MEDIA_GALLERY.md Phase 3): the
    panel's portrait upload needs a real entity to attach to, and a name is
    not a handle — two people can share one.
    """
    rows = await db.execute(
        select(Entity.id, Entity.name, Entity.type, EntityMention.summary)
        .join(EntityMention, EntityMention.entity_id == Entity.id)
        .where(EntityMention.raw_segment_id == segment_id)
        .where(Entity.producer_id == producer_id)
        .order_by(Entity.name)
    )
    return [(id_, name, type_, summary) for id_, name, type_, summary in rows]


async def find_segments_mentioning(
    db: AsyncSession,
    entity_names: Sequence[str],
    producer_id: str,
    exclude_ids: Sequence[str] = (),
    limit: int = 10,
) -> List[str]:
    """Segment ids mentioning any of `entity_names`. Never transcript content.

    Matching is on `normalized_name`, which is a real improvement rather than
    a translation: the graph matched names exactly (case-insensitively), so a
    final-letter or maqaf variant of the same name missed. Here it cannot,
    because the same key that merged the entity does the lookup.
    """
    keys = [k for k in (normalize_entity_name(n) for n in entity_names) if k]
    if not keys:
        return []
    rows = await db.execute(
        select(EntityMention.raw_segment_id)
        .join(Entity, Entity.id == EntityMention.entity_id)
        .where(Entity.producer_id == producer_id)
        .where(Entity.normalized_name.in_(keys))
        .where(EntityMention.raw_segment_id.notin_(list(exclude_ids)) if exclude_ids else True)
        .distinct()
        .limit(limit)
    )
    return [r[0] for r in rows]


async def find_segments_mentioning_scored(
    db: AsyncSession,
    entity_names: Sequence[str],
    producer_id: str,
    exclude_ids: Sequence[str] = (),
    limit: int = 10,
) -> List[Dict[str, object]]:
    """Like `find_segments_mentioning`, plus how many of the named entities
    each segment shares — the retrieval pipeline's edge-weight proxy.

    The graph's "score" was already `COUNT(DISTINCT entity)` with an
    `ORDER BY count DESC`, so this is the same computation stated directly
    rather than a reimplementation of something cleverer.
    """
    keys = [k for k in (normalize_entity_name(n) for n in entity_names) if k]
    if not keys:
        return []
    shared = func.count(func.distinct(Entity.id)).label("shared_entity_count")
    rows = await db.execute(
        select(EntityMention.raw_segment_id, shared)
        .join(Entity, Entity.id == EntityMention.entity_id)
        .where(Entity.producer_id == producer_id)
        .where(Entity.normalized_name.in_(keys))
        .where(EntityMention.raw_segment_id.notin_(list(exclude_ids)) if exclude_ids else True)
        .group_by(EntityMention.raw_segment_id)
        .order_by(shared.desc())
        .limit(limit)
    )
    return [{"segment_id": sid, "shared_entity_count": n} for sid, n in rows]


async def get_entity_candidates(
    db: AsyncSession, name: str, producer_id: str, limit: int = 5
) -> List[Dict[str, Optional[str]]]:
    """Fuzzy name matches, for human-in-the-loop disambiguation.

    Returns {"uuid", "name", "summary"} — `uuid` keeps the key name the
    confirmation payload and its frontend modal already use, so the shape does
    not change under them; it holds an entity id.

    pg_trgm rather than the graph's hybrid vector+BM25+RRF search. Nothing is
    lost today: every caller filters these through `names_are_similar`, a
    purely lexical gate, so semantic recall was discarded downstream anyway.
    The ORDER BY makes the ranking explicit — the graph's relevance order was
    opaque, and this one can be read.

    Same contract as before, deliberately: candidates ranked, WITHOUT a
    minimum-similarity floor. Filtering stays the caller's job.
    """
    key = normalize_entity_name(name)
    if not key:
        return []

    if db.bind is not None and db.bind.dialect.name == "postgresql":
        # The real path: pg_trgm's similarity(), backed by the GIN index
        # migration 0012 creates. Verified against the live database, since
        # the tests below necessarily exercise the fallback instead.
        similarity = func.similarity(Entity.normalized_name, key).label("sim")
        rows = await db.execute(
            select(
                Entity.id,
                Entity.name,
                Entity.type,
                Entity.identity_asked_at,
                similarity,
            )
            .where(Entity.producer_id == producer_id)
            .where(~Entity.is_self)
            .order_by(similarity.desc(), Entity.name)
            .limit(limit)
        )
        ranked = [(eid, ename, etype, asked) for eid, ename, etype, asked, _ in rows]
    else:
        # SQLite (tests) has no pg_trgm. Rank in Python over the producer's
        # entities — correct, and affordable at any size this ever reaches,
        # but NOT what production runs, which is why the branch is explicit
        # rather than hidden behind a helper that pretends they are the same.
        rows = await db.execute(
            select(
                Entity.id,
                Entity.name,
                Entity.normalized_name,
                Entity.type,
                Entity.identity_asked_at,
            )
            .where(Entity.producer_id == producer_id)
            .where(~Entity.is_self)
        )
        scored = sorted(
            (
                (SequenceMatcher(None, key, norm).ratio(), ename, eid, etype, asked)
                for eid, ename, norm, etype, asked in rows
            ),
            key=lambda t: (-t[0], t[1]),
        )
        ranked = [
            (eid, ename, etype, asked) for _, ename, eid, etype, asked in scored[:limit]
        ]

    candidates = [
        {
            "uuid": eid,
            "name": ename,
            "summary": None,
            # person | place | organisation | event | other. Carried so a
            # question about a PLACE does not ask whether it is the same
            # person — which it did, and תל אביב is mentioned in as many
            # recordings as anybody.
            "type": etype,
            # Has the producer already confirmed who this row is? The caller
            # uses it to decide whether a VERBATIM name match may be merged
            # without asking — see check_entities_node. Carried on the
            # candidate rather than fetched separately so the decision and the
            # evidence for it arrive together.
            "identity_asked": asked is not None,
        }
        for eid, ename, etype, asked in ranked
    ]
    # Summaries come from the mentions, newest first — "which Gila is this"
    # is answered by what was most recently said about her.
    for candidate in candidates:
        summaries = await db.execute(
            select(EntityMention.summary)
            .where(EntityMention.entity_id == candidate["uuid"])
            .where(EntityMention.summary.isnot(None))
            .order_by(EntityMention.created_at.desc())
            .limit(1)
        )
        candidate["summary"] = summaries.scalar_one_or_none()
    return candidates


#: Marks a candidate that has no entity row yet — see `pending_entity_candidates`.
PENDING_CANDIDATE_PREFIX = "pending:"


MANUAL_ORIGIN = "manual"


async def _manual_edge_exists(db: AsyncSession, a_id: str, b_id: str) -> bool:
    """Is this pair already joined by a relation the producer set by hand?

    Direction-agnostic: the producer stated something about these two people,
    and re-deriving it from a recording in the other direction would be the
    same contradiction wearing a different hat.
    """
    found = (
        await db.execute(
            select(EntityRelation.id).where(
                EntityRelation.origin == MANUAL_ORIGIN,
                (
                    (EntityRelation.from_entity_id == a_id)
                    & (EntityRelation.to_entity_id == b_id)
                )
                | (
                    (EntityRelation.from_entity_id == b_id)
                    & (EntityRelation.to_entity_id == a_id)
                ),
            )
        )
    ).scalars().first()
    return found is not None


async def set_relation_by_hand(
    db: AsyncSession,
    *,
    producer_id: str,
    from_entity_id: str,
    to_entity_id: str,
    relation_type: str,
) -> Dict[str, Any]:
    """Write a relation the producer stated directly, REPLACING what it contradicts.

    docs/TREE_EDITING.md's governing rule: a manual edit always wins. No
    dialog, and — this is the part that matters — no silent coexistence. Two
    edges that cannot both be true would leave the tree to pick one at render,
    which is exactly the bug that made a chosen parent look like it had failed
    to save.

    WHAT COUNTS AS CONTRADICTING is computed from `generation_delta`, never
    from a hardcoded list of types. The delta IS the claim an edge makes about
    where somebody sits relative to the other end, so two edges between the
    same pair conflict precisely when their deltas disagree — and a relation
    type added to the lookup table later is covered without editing this
    function. That is the whole reason `relation_types` is a table.

    WHAT GETS REPLACED IS ABOUT THE PERSON, NOT THE PAIR — and getting that
    wrong is what made the first version of this look broken. Scoping the
    replacement to edges between the two people named misses the contradiction
    almost every time: told "יוסף is רז's child", the edges that disagree are
    יוסף's SIBLING edge to the producer and his parent edges from אילנה and
    צבי, none of which involve רז at all. Nothing was replaced, the tree kept
    its first placement, and the edit looked like it had silently failed.

    So the rule is: `from_entity_id` is the person being placed, and every
    existing edge that implies a DIFFERENT generation for them is removed.
    Generations are read from the tree as it stands, so "different" means
    different from where this edit puts them, not different in the abstract.

    The other end is untouched: "יוסף is רז's child" says nothing about who
    רז's own parents are.
    """
    if from_entity_id == to_entity_id:
        raise ValueError("A relation needs two different people.")

    rows = (
        await db.execute(
            select(Entity).where(
                Entity.producer_id == producer_id,
                Entity.id.in_([from_entity_id, to_entity_id]),
            )
        )
    ).scalars().all()
    if len(rows) != 2:
        raise ValueError("Both people must be in this archive.")

    wanted = (
        await db.execute(
            select(RelationType).where(RelationType.relation_type == relation_type)
        )
    ).scalars().first()
    if wanted is None:
        raise ValueError(f"{relation_type!r} is not a relation this archive knows.")

    # Where everyone currently sits, from the tree as it stands. The
    # replacement is decided against real placements, not against relation
    # types in the abstract — "different generation" only means anything
    # relative to somebody.
    from app.services import family_tree

    generations = await family_tree.generations_for(db, producer_id)
    anchor_generation = generations.get(to_entity_id)
    target_generation = (
        None
        if anchor_generation is None or wanted.generation_delta is None
        # `from` is the subject: "<from> is the <type> of <to>", and the tree
        # walks it as gen(from) = gen(to) + delta (see _assign_generations —
        # `parent` carries -1, and a parent sits one row ABOVE their child).
        #
        # ⚠️ This sign was FLIPPED here for as long as the function existed,
        # and every test passed anyway: when the old and new edge carry the
        # SAME delta the flip cancels out, and in the tested mixed-delta cases
        # both sides happened to disagree, so "replace" was right for the
        # wrong reason. It surfaced the first time an agreeing mixed-delta
        # edge existed — placing somebody as a parent's child computed their
        # target a generation ABOVE the parent, decided their sibling, spouse
        # and children edges all disagreed with it, and deleted the lot.
        else anchor_generation + wanted.generation_delta
    )

    # Every edge touching the person being placed, in either direction.
    existing = (
        await db.execute(
            select(EntityRelation, RelationType)
            .join(RelationType, RelationType.relation_type == EntityRelation.relation_type)
            .where(
                (EntityRelation.from_entity_id == from_entity_id)
                | (EntityRelation.to_entity_id == from_entity_id)
            )
        )
    ).all()

    replaced: List[Dict[str, Any]] = []
    for relation, rtype in existing:
        other_id = (
            relation.to_entity_id
            if relation.from_entity_id == from_entity_id
            else relation.from_entity_id
        )
        if other_id == to_entity_id:
            # SAME PAIR. Decided on the relation types alone, not on where the
            # two are placed: "Raz is Chen's sibling" and "Raz is Chen's
            # parent" contradict each other whether or not either has a path
            # to the root, and a producer editing two unplaced people is
            # exactly who needs this to work.
            pair_delta = rtype.generation_delta
            if pair_delta is not None and relation.from_entity_id == to_entity_id:
                pair_delta = -pair_delta
            equivalent = (
                pair_delta is not None
                and wanted.generation_delta is not None
                and pair_delta == wanted.generation_delta
                # An identical restatement is still replaced rather than
                # duplicated — clicking Save again when nothing appeared to
                # happen is exactly what a producer does.
                and relation.relation_type != relation_type
            )
            if equivalent:
                continue
            replaced.append(
                {
                    "relation_type": relation.relation_type,
                    "origin": relation.origin,
                    "source_segment_id": relation.source_segment_id,
                }
            )
            await db.delete(relation)
            continue

        other_generation = generations.get(other_id)
        if (
            target_generation is None
            or other_generation is None
            or rtype.generation_delta is None
        ):
            # Nothing placed either end, so this edge makes no claim that can
            # disagree. Left alone rather than guessed at — the tree's own rule.
            continue

        # What generation this edge implies for the person being placed —
        # same convention as the target above: gen(from) = gen(to) + delta,
        # so an edge FROM the person implies other + delta, and an edge TO
        # them implies other - delta. (Both signs were flipped alongside the
        # target's; see the warning there.)
        delta = rtype.generation_delta
        implied = (
            other_generation + delta
            if relation.from_entity_id == from_entity_id
            else other_generation - delta
        )
        if implied == target_generation:
            # Agrees with where this edit puts them. Keep it — replacing a
            # true statement with an equivalent one would lose the recording
            # it came from for nothing.
            continue

        replaced.append(
            {
                "relation_type": relation.relation_type,
                "origin": relation.origin,
                "source_segment_id": relation.source_segment_id,
            }
        )
        await db.delete(relation)

    await db.flush()
    written = EntityRelation(
        from_entity_id=from_entity_id,
        to_entity_id=to_entity_id,
        relation_type=relation_type,
        # No recording behind it, so no segment and no cascade. Permanent by
        # construction — see migration 0020.
        source_segment_id=None,
        origin=MANUAL_ORIGIN,
    )
    db.add(written)
    await db.flush()

    logger.info(
        f"Manual relation set: {from_entity_id[:8]} -{relation_type}-> "
        f"{to_entity_id[:8]}, replacing {len(replaced)}"
    )
    return {"relation_id": written.id, "replaced": replaced}


async def mark_placement_asked(db: AsyncSession, entity_ids: Sequence[str]) -> int:
    """Stamp the ask-once questions as settled for these people.

    `parentage_asked_at` and `side_asked_at` gate the QUESTIONS, not direct
    writes — so without this a producer who places somebody by hand is asked
    where that person belongs on their next recording, and answering would
    contradict the edit they just made. Stamping is what makes the manual
    answer count as the answer.

    Only ever sets a stamp, never clears one: nothing here should make a
    question that has already been settled come back.
    """
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(select(Entity).where(Entity.id.in_(list(entity_ids))))
    ).scalars().all()
    stamped = 0
    for entity in rows:
        if entity.parentage_asked_at is None:
            entity.parentage_asked_at = now
            stamped += 1
        if entity.side_asked_at is None:
            entity.side_asked_at = now
            stamped += 1
    await db.flush()
    return stamped


@dataclass(frozen=True)
class ConfusableEntity:
    """One person in a group of people this archive calls by similar names."""

    entity_id: str
    name: str
    #: What the archive most recently said about them — this, not the name, is
    #: what tells an uncle from an army friend. Both are called אמנון.
    summary: str
    #: The recordings that mention THIS one. The whole basis of the
    #: distinction: each mention points at exactly one entity, so the archive
    #: already knows which אמנון each recording means.
    segment_ids: Tuple[str, ...]


async def confusable_entities(
    db: AsyncSession, producer_id: str
) -> List[List[ConfusableEntity]]:
    """Groups of 2+ entities this producer's archive calls by similar names.

    The retrieval side of the אמנון problem. `/talk` reads transcripts, and a
    transcript says "אמנון" whether it means the uncle or the army friend —
    so an answer about one can be assembled from the other's recordings, and
    was. The archive is NOT missing the distinction: `entity_mentions` points
    each mention at exactly one entity, so it knows which אמנון every
    recording means. Nothing was reading it.

    Returns only the AMBIGUOUS groups, and returning nothing is the normal
    case. That matters more than it looks: every caller is expected to change
    its behaviour only when this is non-empty, so an archive with no repeated
    names produces a byte-identical prompt to the one it produced before this
    existed. The safest version of a feature that might over-fire is one that
    cannot fire at all where there is nothing to disambiguate.

    A STRICTER test than the confirmation screen's `names_are_similar`, and
    the difference is deliberate. That gate includes a character-similarity
    fallback for spelling variants ("גילה"/"גליה"), which is right when the
    cost of a false positive is one question a human answers in a second. Here
    the cost is asking a LISTENER "which one did you mean?" about two people
    nobody could confuse — measured on the live archive, it grouped אירה
    (ניר's wife) with יאיר (ניר's child), who share three letters and nothing
    else. Over-asking is the failure mode this feature is most likely to have,
    so the rule is the one that actually describes confusability: the same
    name, or one name a more specific version of the other ("אמנון" /
    "אמנון נחום"). Someone saying the shorter one could mean either; nobody
    saying "אירה" could mean יאיר.

    `is_self` is excluded: the producer is never one of two people the
    listener could mean.
    """

    def confusable(a: str, b: str) -> bool:
        # Normalised tokens, so Hebrew final-letter forms compare equal.
        at = {t for t in normalize_entity_name(a).split() if t}
        bt = {t for t in normalize_entity_name(b).split() if t}
        return bool(at and bt and (at <= bt or bt <= at))

    entities = list(
        (
            await db.execute(
                select(Entity)
                .where(Entity.producer_id == producer_id, ~Entity.is_self)
                .order_by(Entity.name)
            )
        ).scalars().all()
    )
    if len(entities) < 2:
        return []

    # Union-find over the similarity relation. Not pairwise-only: similarity
    # is not transitive, and "אמנון" ~ "אמנון נחום" ~ "אמנון נחום כהן" must be
    # ONE group of three rather than two overlapping pairs — a listener asking
    # about "אמנון" could mean any of them.
    parent = {e.id: e.id for e in entities}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(entities):
        for b in entities[i + 1 :]:
            if confusable(a.name, b.name):
                parent[find(a.id)] = find(b.id)

    grouped: Dict[str, List[Entity]] = {}
    for e in entities:
        grouped.setdefault(find(e.id), []).append(e)

    result: List[List[ConfusableEntity]] = []
    for members in grouped.values():
        if len(members) < 2:
            continue
        group: List[ConfusableEntity] = []
        for entity in members:
            rows = (
                await db.execute(
                    select(EntityMention.raw_segment_id, EntityMention.summary)
                    .where(EntityMention.entity_id == entity.id)
                    .order_by(EntityMention.created_at.desc())
                )
            ).all()
            summary = next((s for _, s in rows if s), "") or ""
            group.append(
                ConfusableEntity(
                    entity_id=entity.id,
                    name=entity.name,
                    summary=summary.strip(),
                    segment_ids=tuple(sid for sid, _ in rows),
                )
            )
        # A group whose members are mentioned nowhere cannot disambiguate
        # anything — there is no recording to attribute either way.
        if any(member.segment_ids for member in group):
            result.append(sorted(group, key=lambda m: m.name))
    return sorted(result, key=lambda g: g[0].name)


async def mark_identity_asked(
    db: AsyncSession, producer_id: str, entity_ids: Sequence[str]
) -> int:
    """Record that the producer has settled WHO these rows are.

    The stamp that makes always-asking bearable. A name matching an existing
    entity verbatim now raises an identity question instead of merging on the
    assumption that one name means one person — but asked on every recording
    that mentions a brother, that question would be noise, and noise is
    answered without reading. Stamped once, it never comes back for that
    person.

    Set whether the answer was "yes, the same" or "someone different", and set
    even when the question went unanswered — identical to `year_asked_at` and
    for the identical reason. Silence is a real answer here ("someone new", the
    safe default), and a question that comes back until answered a particular
    way is not a question.

    Scoped by producer as well as id: entity ids are unguessable uuids, but a
    write should not rest on that.

    Only ever SETS a stamp. Nothing should make a settled question return, and
    the two callers — this pipeline and any repair script — must not be able to
    undo each other by ordering.
    """
    ids = [i for i in entity_ids if i]
    if not ids:
        return 0
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(Entity).where(
                Entity.id.in_(ids),
                Entity.producer_id == producer_id,
                Entity.identity_asked_at.is_(None),
            )
        )
    ).scalars().all()
    for entity in rows:
        entity.identity_asked_at = now
    await db.flush()
    return len(rows)


async def speaker_name_for(db: AsyncSession, producer_id: str) -> Optional[str]:
    """What to tell the extractor the narrator is called.

    The `is_self` entity's name first — migration 0012 built it from
    `full_name`, and it is the name the archive already calls this person. The
    user row is the fallback for a producer created before that hook existed.
    """
    self_entity = (
        await db.execute(
            select(Entity.name).where(Entity.producer_id == producer_id, Entity.is_self)
        )
    ).scalars().first()
    if self_entity:
        return self_entity
    user = (
        await db.execute(
            select(User.full_name, User.username).where(User.id == producer_id)
        )
    ).first()
    if user is None:
        return None
    return user.full_name or user.username or None


async def fold_speaker_into_self(
    db: AsyncSession,
    producer_id: str,
    extracted: Sequence[ExtractedEntity],
    proposed: Sequence[ExtractedRelation],
    self_marker: str,
) -> Tuple[List[ExtractedEntity], List[ExtractedRelation]]:
    """Drop the producer from their own extraction, and re-point relations.

    A transcript is the speaker's own account, so they are the one person in it
    who must NOT become an entity — they are who the archive belongs to, not
    somebody in it. When they name themselves ("אנחנו חמישה: אני טל, עדי…") and
    that name is extracted, the producer gets a second, disconnected copy of
    themselves: `טל` alongside the `is_self` row, collecting relations that
    belong on the tree's root. Seen exactly that way on the live archive.

    The prompt now says not to, and a prompt cannot be the only guard on
    something that silently forks the root. This is the structural half:

      * an extracted entity whose merge key matches the producer's OWN entity
        is dropped;
      * any relation pointing at it is re-pointed at `__SELF__`, so the
        relation survives rather than being lost with the entity;
      * a relation that becomes a self-loop is dropped, because
        `ck_entity_relations_not_self` forbids it and "X is their own sibling"
        is not a fact worth keeping.

    Matches on `normalized_name`, so this catches the case where the producer's
    stored name and the spoken one agree. It does NOT catch a producer whose
    self entity is "Tal Nahum" saying "אני טל" — different scripts, different
    merge keys — which is why the prompt rule carries the other half. Two
    partial guards on different failure modes, deliberately, rather than one
    that looks total and is not.
    """
    self_entity = (
        await db.execute(
            select(Entity).where(Entity.producer_id == producer_id, Entity.is_self)
        )
    ).scalars().first()
    if self_entity is None:
        return list(extracted), list(proposed)

    self_keys = {self_entity.normalized_name}
    self_keys.discard("")
    dropped = {
        e.name for e in extracted if normalize_entity_name(e.name) in self_keys
    }
    if not dropped:
        return list(extracted), list(proposed)

    logger.info(
        f"Extraction named the producer themselves ({sorted(dropped)}); "
        f"folding into the self entity"
    )
    kept = [e for e in extracted if e.name not in dropped]

    relations: List[ExtractedRelation] = []
    for relation in proposed:
        from_name = self_marker if relation.from_name in dropped else relation.from_name
        to_name = self_marker if relation.to_name in dropped else relation.to_name
        if from_name == to_name:
            continue
        relations.append(replace(relation, from_name=from_name, to_name=to_name))
    return kept, relations


async def pending_entity_candidates(
    db: AsyncSession, producer_id: str, exclude_segment_id: str
) -> List[Dict[str, Optional[str]]]:
    """Names this producer's OTHER unanswered recordings have already proposed.

    Closes a window that silently fragments people across stories. Entities are
    written by `finalize_ingest`, which runs AFTER the confirmation pause — so a
    recording analysed while an earlier one is still waiting for answers cannot
    see that earlier recording's entities at all. Measured on the live archive:
    a recording naming "איציק" paused for 91 seconds waiting on a human, and a
    second recording naming "איציק כהן" ran its identity check inside that
    window, matched nothing, and created a second person. No question was ever
    asked, because at the moment of asking there was genuinely nothing to ask
    about.

    Waiting for the earlier write is not an option: it happens after the human
    answers, so waiting on it means waiting on them.

    WHY A CANDIDATE NEEDS NO ROW. A confirmed identity is applied by RENAMING —
    `_apply_entity_resolutions` reads `resolved_name` and treats `same_as_uuid`
    as nothing but a boolean gate, because in Postgres the merge is
    UNIQUE (producer_id, normalized_name) rather than a suggestion to an
    engine. So answering "yes, the same" writes this recording's entity under
    the other name, and both land on one row whenever that row is created. The
    candidate is a NAME; the id is ceremony, and carries a marker prefix so it
    can never be mistaken for a real entity id.
    """
    rows = await db.execute(
        select(RawSegment.pending_confirmation)
        .join(InterviewSession, InterviewSession.id == RawSegment.interview_session_id)
        .where(
            InterviewSession.user_id == producer_id,
            RawSegment.status == "pending_confirmation",
            RawSegment.id != exclude_segment_id,
        )
    )
    seen: Dict[str, Tuple[str, Optional[str]]] = {}
    for (payload,) in rows:
        # `editable_entities` is every entity that recording extracted — the
        # same superset the confirmation screen offers for name correction.
        for item in (payload or {}).get("editable_entities") or []:
            name = (item or {}).get("name")
            key = normalize_entity_name(name or "")
            if key and key not in seen:
                seen[key] = (name, (item or {}).get("type"))
    return [
        {
            "uuid": f"{PENDING_CANDIDATE_PREFIX}{name}",
            "name": name,
            "summary": "from a recording you haven't finished checking yet",
            # The extractor's guess, unconfirmed — which is all this candidate
            # is. Enough to word the question ("the same place") without
            # claiming more than is known.
            "type": etype,
            # Never settled, and it cannot be: there is no row to have
            # confirmed. A verbatim match against one of these therefore always
            # asks — which is the right outcome, since an unanswered recording
            # is the least verified thing the archive knows about anybody.
            "identity_asked": False,
        }
        for name, etype in sorted(seen.values())
    ]


# ── Write internals ─────────────────────────────────────────────────────────


async def _get_or_create_entity(
    db: AsyncSession,
    *,
    producer_id: str,
    extracted: ExtractedEntity,
    normalized: str,
    result: Optional[EntityWriteResult] = None,
) -> Tuple[Entity, bool]:
    """The merge. Returns (entity, created)."""

    def _record(change: Optional[Tuple[str, str]]) -> None:
        if change and result is not None:
            result.type_changes.append((extracted.name, change[0], change[1]))

    existing = await _find_entity(db, producer_id, normalized)
    if existing is not None:
        _record(
            _maybe_upgrade_type(
                existing, extracted.type, confirmed=extracted.type_confirmed
            )
        )
        _maybe_set_year(existing, extracted.year_start)
        _mark_year_asked(existing, extracted.year_asked)
        return existing, False

    entity = Entity(
        year_start=extracted.year_start,
        year_asked_at=datetime.now(timezone.utc) if extracted.year_asked else None,
        producer_id=producer_id,
        # Verbatim, not normalised: `name` is what gets shown back to the
        # producer, and it should be what they actually said. The normalised
        # form is a key, not a display value.
        name=extracted.name.strip(),
        normalized_name=normalized,
        type=extracted.type,
    )
    try:
        # A savepoint, so losing the race costs this one INSERT rather than
        # the whole transaction. Two recordings for the same producer can be
        # analysed concurrently and both name מונטריאול for the first time;
        # the unique constraint is what decides, and the loser re-reads the
        # winner's row instead of failing the ingest.
        #
        # `add` goes INSIDE the block, and that is not cosmetic: with the add
        # outside, the failing flush is attributed to the enclosing
        # transaction and the whole session is marked rollback-only, so the
        # recovery below dies on PendingRollbackError — the exact failure this
        # is here to avoid, with a more confusing error. Nor is the pending
        # instance expunged afterwards: the savepoint rollback already evicted
        # it, and expunging again raises InvalidRequestError.
        async with db.begin_nested():
            db.add(entity)
            await db.flush()
    except IntegrityError:
        existing = await _find_entity(db, producer_id, normalized)
        if existing is None:
            # The unique constraint was not what rejected this, so the write
            # is genuinely broken and must not be swallowed.
            raise
        _record(
            _maybe_upgrade_type(
                existing, extracted.type, confirmed=extracted.type_confirmed
            )
        )
        return existing, False
    return entity, True


async def ensure_self_entity(db: AsyncSession, user) -> Tuple[Optional[Entity], bool]:
    """The producer's own node — the root every family relation hangs off.

    Migration 0012 created one per producer EXISTING at the time and its own
    comment said new producers get theirs "at signup (application code)". This
    is that code. Without it a producer who signed up after the migration has
    no root, and relations cannot be expressed at all: every extracted summary
    is phrased relative to הדובר (the speaker), so "I have four brothers" needs
    a node for them to be brothers OF.

    Created eagerly rather than lazily on first use, so the tree's root is an
    invariant instead of something every caller has to remember. Idempotent —
    safe to call on every registration and to re-run as a backfill.

    Name is `full_name`, falling back to `username` when it is null or blank,
    matching the migration (one existing producer needed the fallback). It is a
    display label the producer can correct later; what must exist is the ROW.

    `normalized_name` uses the application normaliser, NOT the plain
    LOWER(TRIM(...)) the migration used — the migration ran in SQL where the
    Hebrew normaliser was unavailable and documented that as a deliberate
    limitation. Here it is available, and using it is what makes the merge key
    correct: if a transcript ever names the producer, `_get_or_create_entity`
    looks up by the normalised key and lands on THIS row instead of creating a
    duplicate person. Verified 2026-08-02 that all 5 existing self-entities
    normalise identically under both, so this introduces no split today; a
    Hebrew `full_name` is where the two would have diverged.

    Returns (entity, created). `(None, False)` only in the collision case
    described below, which is logged loudly rather than raised — a producer
    should never fail to register because of an entity row.
    """
    if getattr(user, "role", None) != "producer":
        return None, False

    raw_name = (user.full_name or "").strip() or (user.username or "").strip()
    if not raw_name:
        logger.error(
            f"Cannot create self-entity for {user.id}: both full_name and username are blank"
        )
        return None, False

    normalized = normalize_entity_name(raw_name)

    existing = await _find_entity(db, user.id, normalized)
    if existing is not None:
        if existing.is_self:
            return existing, False
        if existing.type == "person":
            # A transcript already named the producer before this ran. It IS
            # them, and the merge key says so — promote rather than colliding.
            existing.is_self = True
            logger.info(f"Promoted existing entity {existing.id} to self for producer {user.id}")
            return existing, False
        # Same key, but typed place/organisation — promoting would violate
        # ck_entities_self_is_person, and retyping someone's archive because a
        # place shares their name would be worse than having no root. Loud,
        # not silent: the tree renders its empty state and this line says why.
        logger.error(
            f"Cannot create self-entity for producer {user.id}: an entity named "
            f"{existing.name!r} of type {existing.type!r} already holds that key"
        )
        return None, False

    entity = Entity(
        producer_id=user.id,
        name=raw_name,
        normalized_name=normalized,
        type="person",
        is_self=True,
    )
    try:
        # Same savepoint pattern as _get_or_create_entity: two concurrent
        # callers (a registration racing a backfill) must cost one INSERT,
        # not the enclosing transaction.
        async with db.begin_nested():
            db.add(entity)
            await db.flush()
    except IntegrityError:
        existing = await _find_entity(db, user.id, normalized)
        if existing is None:
            raise
        return existing, False
    return entity, True


# ── sibling parentage ─────────────────────────────────────────────────────
#
# Phase 6 of docs/FAMILY_TREE_TIMELINE.md. A sibling is recorded as a sibling
# OF THE PRODUCER, and nothing in that says whose child they are — so the
# family tree can place them in the right row and still draw no line to them.
# The producer is asked once, and the answer is written as ordinary parent
# relations.
#
# ## Everything here is keyed by NAME, not by entity id
#
# Because the answer has to be available on the FIRST recording. A new
# producer's first answer is "my parents are צבי and אילנה and my siblings are
# ...", and at the moment the question is built none of those people exist as
# rows yet — they are written at finalize, after the confirmation. Keying by id
# would mean the question could only ever be asked about people some EARLIER
# recording had already established, which is exactly the bug this replaces:
# across four recordings the question never once appeared, because each one was
# itself the recording that created the siblings.
#
# Names resolve to entities at write time, by which point write_segment_entities
# has created them. See write_parentage.
#
# These two relation names are hardcoded, unlike everything else that reads the
# `relation_types` vocabulary from the database. That is deliberate: the
# question is literally "is your parent also their parent", which is about
# those two relations. Deriving it from `generation_delta` would also sweep in
# step-parents and spouses, which is exactly wrong here.
PARENT_RELATION = "parent"
SIBLING_RELATION = "sibling"
AUNT_UNCLE_RELATION = "aunt_uncle"
GRANDPARENT_RELATION = "grandparent"


def _proposed_names(
    proposed_relations: Sequence[dict], self_marker: str, relation_type: str
) -> List[str]:
    """Names this recording proposes as `relation_type` of the producer.

    Symmetric relations are stored one way round, and the extractor may emit
    either — so the producer can be on either end and both are checked.
    """
    found: List[str] = []
    for relation in proposed_relations or []:
        if relation.get("relation_type") != relation_type:
            continue
        from_name = relation.get("from_name")
        to_name = relation.get("to_name")
        if to_name == self_marker and from_name and from_name != self_marker:
            found.append(from_name)
        elif from_name == self_marker and to_name and to_name != self_marker:
            found.append(to_name)
    return found


async def people_for_correction(
    db: AsyncSession,
    producer_id: str,
    proposed_relations: Optional[Sequence[dict]] = None,
    self_marker: str = "__SELF__",
) -> List[Dict[str, Any]]:
    """Everyone a corrected relation may point at.

    The archive's people PLUS anyone this recording has only just named, which
    is what lets a correction be made on a first recording — the same reasoning
    as `parentage_candidates`, but without its preconditions. That function
    returns nothing at all when the producer has no recorded parents, because
    there is then no parentage question to ask; a wrong relation still needs
    correcting in that situation, so this cannot be derived from it.

    Picking beats typing: a typed name resolves by normalised match, so one
    different character makes a second person instead of linking to the first.
    """
    people = {
        entity.normalized_name: {"name": entity.name, "entity_id": entity.id}
        for entity in (
            await db.execute(
                select(Entity).where(
                    Entity.producer_id == producer_id,
                    Entity.type == "person",
                    Entity.is_self.is_(False),
                )
            )
        ).scalars().all()
    }

    for relation in proposed_relations or []:
        for name in (relation.get("from_name"), relation.get("to_name")):
            if not name or name == self_marker:
                continue
            normalized = normalize_entity_name(name)
            if normalized and normalized not in people:
                people[normalized] = {"name": name, "entity_id": None}

    return sorted(people.values(), key=lambda person: person["name"])


async def parentage_candidates(
    db: AsyncSession,
    producer_id: str,
    proposed_relations: Optional[Sequence[dict]] = None,
    self_marker: str = "__SELF__",
) -> Dict[str, list]:
    """Who to ask about, and which parents to offer.

    Returns `{"parents": [...], "siblings": [...], "known_people": [...]}`,
    each entry `{"name", "entity_id"}` with a null id for anyone this recording
    has only just named.

    Both lists combine what the archive already holds with what THIS recording
    proposes, which is what makes the question answerable on a producer's very
    first recording. A sibling qualifies when they have no parent recorded and
    have never been asked; with no parents to offer, nothing is asked at all.
    """
    self_entity = (
        await db.execute(
            select(Entity).where(Entity.producer_id == producer_id, Entity.is_self)
        )
    ).scalars().first()
    if self_entity is None:
        return {"parents": [], "siblings": [], "known_people": []}

    everyone = {
        e.normalized_name: e
        for e in (
            await db.execute(
                select(Entity).where(Entity.producer_id == producer_id)
            )
        ).scalars().all()
    }

    # ── parents: already recorded, plus any this recording proposes ──────
    recorded_parents = list(
        (
            await db.execute(
                select(Entity)
                .join(EntityRelation, EntityRelation.from_entity_id == Entity.id)
                .where(
                    EntityRelation.to_entity_id == self_entity.id,
                    EntityRelation.relation_type == PARENT_RELATION,
                )
                .order_by(Entity.name)
                .distinct()
            )
        ).scalars().all()
    )
    parents: Dict[str, dict] = {
        p.normalized_name: {"name": p.name, "entity_id": p.id} for p in recorded_parents
    }
    for name in _proposed_names(proposed_relations, self_marker, PARENT_RELATION):
        key = normalize_entity_name(name)
        if key and key not in parents:
            existing = everyone.get(key)
            parents[key] = {
                "name": existing.name if existing else name,
                "entity_id": existing.id if existing else None,
            }
    if not parents:
        return {"parents": [], "siblings": [], "known_people": []}

    # ── siblings: already recorded, plus any this recording proposes ─────
    sibling_rows = (
        await db.execute(
            select(EntityRelation.from_entity_id, EntityRelation.to_entity_id).where(
                EntityRelation.relation_type == SIBLING_RELATION,
                (EntityRelation.from_entity_id == self_entity.id)
                | (EntityRelation.to_entity_id == self_entity.id),
            )
        )
    ).all()
    recorded_sibling_ids = {
        (to_id if from_id == self_entity.id else from_id)
        for from_id, to_id in sibling_rows
    }
    recorded_sibling_ids.discard(self_entity.id)

    # Anyone who already has a parent recorded is never asked about — the tree
    # can already draw them, whether that parent is the producer's or not.
    with_parents = set(
        (
            await db.execute(
                select(EntityRelation.to_entity_id).where(
                    EntityRelation.relation_type == PARENT_RELATION
                )
            )
        ).scalars().all()
    )

    candidates: Dict[str, dict] = {}
    for entity in everyone.values():
        if entity.id not in recorded_sibling_ids:
            continue
        if entity.id in with_parents or entity.parentage_asked_at is not None:
            continue
        candidates[entity.normalized_name] = {
            "name": entity.name,
            "entity_id": entity.id,
            # Their sibling relation is already recorded, so a parentage
            # answer for them stands on its own. A proposed-only sibling is
            # conditional on that proposal being accepted — see
            # human_confirm_node.
            "recorded": True,
        }
    for name in _proposed_names(proposed_relations, self_marker, SIBLING_RELATION):
        key = normalize_entity_name(name)
        if not key or key in candidates or key in parents:
            continue
        existing = everyone.get(key)
        if existing is not None and (
            existing.id in with_parents or existing.parentage_asked_at is not None
        ):
            continue
        candidates[key] = {
            "name": existing.name if existing else name,
            "entity_id": existing.id if existing else None,
            "recorded": False,
        }

    if not candidates:
        return {"parents": [], "siblings": [], "known_people": []}

    # Everyone already in the archive, so "someone else" can be PICKED rather
    # than typed. A typed name resolves by normalised match, so a spelling that
    # differs by one character silently creates a second person instead of
    # linking to the first. Excludes the producer: nobody is their own
    # sibling's parent.
    known_people = sorted(
        (
            {"name": e.name, "entity_id": e.id}
            for e in everyone.values()
            if e.type == "person" and not e.is_self
        ),
        key=lambda p: p["name"],
    )

    return {
        "parents": sorted(parents.values(), key=lambda p: p["name"]),
        "siblings": sorted(candidates.values(), key=lambda s: s["name"]),
        "known_people": known_people,
    }


async def write_parentage(
    db: AsyncSession,
    *,
    producer_id: str,
    segment_id: str,
    asked_sibling_names: Sequence[str],
    answers: Dict[str, dict],
    not_sibling_names: Sequence[str] = (),
) -> Dict[str, int]:
    """Apply the parentage answers. Flushes, never commits.

    `answers` is keyed by sibling NAME:
        {"ניר": {"parent_names": [...], "new_parent_name": "רבקה"}}

    EVERY sibling in `asked_sibling_names` is stamped `parentage_asked_at`,
    answered or not. Skipping is an answer — "I do not know", or "not now" —
    and without recording it the same question returns on every future
    recording until the producer learns to click past the whole screen. Same
    rule as `year_asked_at`, for the same reason.

    Relations are written with `origin="confirmation"`: the producer said this
    on a screen, not in the recording, and the tree must not offer to play a
    recording that never mentions the person.

    Runs AFTER write_segment_entities, so every name the question offered
    resolves to a row — including people this recording was the first to name.

    `not_sibling_names` are people whose answer named NO parent of the
    producer's, so they cannot be the producer's sibling — a nephew proposed
    as a brother. Their sibling relation is REPLACED rather than left to
    contradict the parent edge written here: the tree cannot honour both, and
    when it kept the sibling and dropped the parent it looked exactly like the
    chosen parent failing to save. The node decides this (it is the one place
    that knows which parents were offered); this deletes what it decided.
    """
    now = datetime.now(timezone.utc)
    result = {"relations": 0, "new_parents": 0, "asked": 0, "siblings_replaced": 0}

    asked = list(dict.fromkeys(asked_sibling_names))
    if not asked:
        return result

    async def resolve(name: str) -> Optional[Entity]:
        normalized = normalize_entity_name(name)
        return await _find_entity(db, producer_id, normalized) if normalized else None

    for sibling_name in asked:
        sibling = await resolve(sibling_name)
        if sibling is None:
            # The entity should exist by now; if the producer declined the
            # sibling relation that named them, it will not. Nothing to stamp
            # and nothing to write.
            logger.warning(f"parentage: no entity for sibling {sibling_name!r}")
            continue

        sibling.parentage_asked_at = now
        result["asked"] += 1

        if sibling_name in set(not_sibling_names):
            # Retract the sibling relation this answer contradicts. Scoped to
            # edges between this person and the PRODUCER: "he is not my
            # brother" says nothing about his being someone else's.
            self_entity = (
                await db.execute(
                    select(Entity).where(
                        Entity.producer_id == producer_id, Entity.is_self
                    )
                )
            ).scalars().first()
            if self_entity is not None:
                removed = await db.execute(
                    delete(EntityRelation).where(
                        EntityRelation.relation_type == SIBLING_RELATION,
                        (
                            (EntityRelation.from_entity_id == sibling.id)
                            & (EntityRelation.to_entity_id == self_entity.id)
                        )
                        | (
                            (EntityRelation.from_entity_id == self_entity.id)
                            & (EntityRelation.to_entity_id == sibling.id)
                        ),
                    )
                )
                result["siblings_replaced"] += removed.rowcount or 0

        answer = answers.get(sibling_name) or {}
        wanted = list(answer.get("parent_names") or [])
        new_name = (answer.get("new_parent_name") or "").strip()
        if new_name:
            wanted.append(new_name)

        parent_ids: List[str] = []
        for parent_name in wanted:
            parent = await resolve(parent_name)
            if parent is None:
                normalized = normalize_entity_name(parent_name)
                if not normalized:
                    continue
                parent = Entity(
                    producer_id=producer_id,
                    name=parent_name.strip(),
                    normalized_name=normalized,
                    type="person",
                )
                db.add(parent)
                await db.flush()
                result["new_parents"] += 1
            parent_ids.append(parent.id)

        for parent_id in dict.fromkeys(parent_ids):
            if parent_id == sibling.id:
                continue  # ck_entity_relations_not_self
            db.add(
                EntityRelation(
                    from_entity_id=parent_id,
                    to_entity_id=sibling.id,
                    relation_type=PARENT_RELATION,
                    source_segment_id=segment_id,
                    origin="confirmation",
                )
            )
            result["relations"] += 1

    await db.flush()
    return result


async def aunt_uncle_candidates(
    db: AsyncSession,
    producer_id: str,
    proposed_relations: Optional[Sequence[dict]] = None,
    self_marker: str = "__SELF__",
) -> Dict[str, list]:
    """Which aunts and uncles still need a side, and which parents to offer.

    `אמנון -aunt_uncle-> Tal` says they are the producer's uncle. It does not
    say WHOSE sibling they are, so there is no edge between them and צבי and
    the parents' row is four boxes with nothing marking the two that are
    parents.

    Same construction as `parentage_candidates`, for the same reasons: keyed by
    NAME so a first recording can answer it, and merging the archive with what
    this recording proposes so the question arrives in the same pass as the
    relation it depends on.

    Someone qualifies when they have no sibling edge to any of those parents
    and have never been asked.
    """
    self_entity = (
        await db.execute(
            select(Entity).where(Entity.producer_id == producer_id, Entity.is_self)
        )
    ).scalars().first()
    if self_entity is None:
        return {"parents": [], "relatives": []}

    everyone = {
        e.normalized_name: e
        for e in (
            await db.execute(select(Entity).where(Entity.producer_id == producer_id))
        ).scalars().all()
    }

    recorded_parents = list(
        (
            await db.execute(
                select(Entity)
                .join(EntityRelation, EntityRelation.from_entity_id == Entity.id)
                .where(
                    EntityRelation.to_entity_id == self_entity.id,
                    EntityRelation.relation_type == PARENT_RELATION,
                )
                .order_by(Entity.name)
                .distinct()
            )
        ).scalars().all()
    )
    parents: Dict[str, dict] = {
        p.normalized_name: {"name": p.name, "entity_id": p.id} for p in recorded_parents
    }
    for name in _proposed_names(proposed_relations, self_marker, PARENT_RELATION):
        key = normalize_entity_name(name)
        if key and key not in parents:
            existing = everyone.get(key)
            parents[key] = {
                "name": existing.name if existing else name,
                "entity_id": existing.id if existing else None,
            }
    if not parents:
        return {"parents": [], "relatives": []}

    # Who is already a sibling of one of those parents — they have their answer.
    parent_ids = {p["entity_id"] for p in parents.values() if p["entity_id"]}
    settled = set()
    if parent_ids:
        rows = (
            await db.execute(
                select(EntityRelation.from_entity_id, EntityRelation.to_entity_id).where(
                    EntityRelation.relation_type == SIBLING_RELATION,
                    EntityRelation.from_entity_id.in_(parent_ids)
                    | EntityRelation.to_entity_id.in_(parent_ids),
                )
            )
        ).all()
        for from_id, to_id in rows:
            settled.add(to_id if from_id in parent_ids else from_id)

    recorded_rows = (
        await db.execute(
            select(EntityRelation.from_entity_id, EntityRelation.to_entity_id).where(
                EntityRelation.relation_type == AUNT_UNCLE_RELATION,
                (EntityRelation.from_entity_id == self_entity.id)
                | (EntityRelation.to_entity_id == self_entity.id),
            )
        )
    ).all()
    recorded_ids = {
        (to_id if from_id == self_entity.id else from_id)
        for from_id, to_id in recorded_rows
    }
    recorded_ids.discard(self_entity.id)

    candidates: Dict[str, dict] = {}
    for entity in everyone.values():
        if entity.id not in recorded_ids:
            continue
        if entity.id in settled or entity.side_asked_at is not None:
            continue
        candidates[entity.normalized_name] = {
            "name": entity.name,
            "entity_id": entity.id,
            "recorded": True,
        }
    for name in _proposed_names(proposed_relations, self_marker, AUNT_UNCLE_RELATION):
        key = normalize_entity_name(name)
        if not key or key in candidates or key in parents:
            continue
        existing = everyone.get(key)
        if existing is not None and (
            existing.id in settled or existing.side_asked_at is not None
        ):
            continue
        candidates[key] = {
            "name": existing.name if existing else name,
            "entity_id": existing.id if existing else None,
            "recorded": False,
        }

    if not candidates:
        return {"parents": [], "relatives": []}

    return {
        "parents": sorted(parents.values(), key=lambda p: p["name"]),
        "relatives": sorted(candidates.values(), key=lambda r: r["name"]),
    }


async def grandparent_candidates(
    db: AsyncSession,
    producer_id: str,
    proposed_relations: Optional[Sequence[dict]] = None,
    self_marker: str = "__SELF__",
) -> Dict[str, list]:
    """Which grandparents still need a side, and which parents to offer.

    The SAME gap as `aunt_uncle_candidates`, one generation up.
    `יוכבד -grandparent-> Tal` says she is the producer's grandmother. It does
    not say WHOSE mother she is, so no edge joins her to צבי and she draws a
    line straight to the producer, skipping the generation between — which on
    the chart reads as a grandparent floating unattached to either parent.

    Found on live data: יוכבד and ג'ולי both correctly captured, both correctly
    placed in row -2, and neither connected to a parent, because nothing had
    ever asked.

    Someone qualifies when no parent edge joins them to any of the producer's
    parents and they have never been asked. `side_asked_at` is REUSED rather
    than given a sibling column: it already means "we asked which of your
    parents this person attaches to", which is exactly this question — the
    only difference is the edge the answer writes.
    """
    self_entity = (
        await db.execute(
            select(Entity).where(Entity.producer_id == producer_id, Entity.is_self)
        )
    ).scalars().first()
    if self_entity is None:
        return {"parents": [], "relatives": []}

    everyone = {
        e.normalized_name: e
        for e in (
            await db.execute(select(Entity).where(Entity.producer_id == producer_id))
        ).scalars().all()
    }

    recorded_parents = list(
        (
            await db.execute(
                select(Entity)
                .join(EntityRelation, EntityRelation.from_entity_id == Entity.id)
                .where(
                    EntityRelation.to_entity_id == self_entity.id,
                    EntityRelation.relation_type == PARENT_RELATION,
                )
                .order_by(Entity.name)
                .distinct()
            )
        ).scalars().all()
    )
    parents: Dict[str, dict] = {
        p.normalized_name: {"name": p.name, "entity_id": p.id} for p in recorded_parents
    }
    for name in _proposed_names(proposed_relations, self_marker, PARENT_RELATION):
        key = normalize_entity_name(name)
        if key and key not in parents:
            existing = everyone.get(key)
            parents[key] = {
                "name": existing.name if existing else name,
                "entity_id": existing.id if existing else None,
            }
    if not parents:
        return {"parents": [], "relatives": []}

    # Already the parent of one of those parents — they have their answer.
    parent_ids = {p["entity_id"] for p in parents.values() if p["entity_id"]}
    settled = set()
    if parent_ids:
        rows = (
            await db.execute(
                select(EntityRelation.from_entity_id).where(
                    EntityRelation.relation_type == PARENT_RELATION,
                    EntityRelation.to_entity_id.in_(parent_ids),
                )
            )
        ).all()
        settled = {from_id for (from_id,) in rows}

    recorded_rows = (
        await db.execute(
            select(EntityRelation.from_entity_id, EntityRelation.to_entity_id).where(
                EntityRelation.relation_type == GRANDPARENT_RELATION,
                (EntityRelation.from_entity_id == self_entity.id)
                | (EntityRelation.to_entity_id == self_entity.id),
            )
        )
    ).all()
    recorded_ids = {
        (to_id if from_id == self_entity.id else from_id) for from_id, to_id in recorded_rows
    }
    recorded_ids.discard(self_entity.id)

    candidates: Dict[str, dict] = {}
    for entity in everyone.values():
        if entity.id not in recorded_ids:
            continue
        if entity.id in settled or entity.side_asked_at is not None:
            continue
        candidates[entity.normalized_name] = {
            "name": entity.name,
            "entity_id": entity.id,
            "recorded": True,
        }
    for name in _proposed_names(proposed_relations, self_marker, GRANDPARENT_RELATION):
        key = normalize_entity_name(name)
        if not key or key in candidates or key in parents:
            continue
        existing = everyone.get(key)
        if existing is not None and (
            existing.id in settled or existing.side_asked_at is not None
        ):
            continue
        candidates[key] = {
            "name": existing.name if existing else name,
            "entity_id": existing.id if existing else None,
            "recorded": False,
        }

    if not candidates:
        return {"parents": [], "relatives": []}

    return {
        "parents": sorted(parents.values(), key=lambda p: p["name"]),
        "relatives": sorted(candidates.values(), key=lambda r: r["name"]),
    }


async def write_sides(
    db: AsyncSession,
    *,
    producer_id: str,
    segment_id: str,
    asked_names: Sequence[str],
    answers: Dict[str, str],
    kinds: Optional[Dict[str, str]] = None,
) -> Dict[str, int]:
    """Record which of the producer's parents a relative attaches to.

    ONE question, TWO consequences, because "which side of the family are they
    on?" is the same question for both kinds and only the resulting edge
    differs:

      * an aunt or uncle is that parent's SIBLING;
      * a grandparent is that parent's PARENT.

    `kinds` maps relative NAME -> "aunt_uncle" | "grandparent", defaulting to
    aunt_uncle so a payload stored before grandparents were asked about still
    writes what it always did.

    `answers` maps relative NAME -> parent NAME. Anyone asked is stamped
    `side_asked_at` whether or not they answered — "not sure" is an answer, and
    a question that returns every recording teaches people to skip the screen.

    The existing `aunt_uncle` / `grandparent` row is KEPT. It is true — they
    ARE the producer's uncle — and the two agree rather than compete: each pair
    places the person in the same row, so the tree reports no contradiction.
    Replacing a true statement with a more specific one would lose the
    recording it came from.
    """
    now = datetime.now(timezone.utc)
    result = {"relations": 0, "asked": 0}
    asked = list(dict.fromkeys(asked_names))
    if not asked:
        return result

    async def resolve(name: str) -> Optional[Entity]:
        normalized = normalize_entity_name(name)
        return await _find_entity(db, producer_id, normalized) if normalized else None

    for relative_name in asked:
        relative = await resolve(relative_name)
        if relative is None:
            logger.warning(f"side: no entity for {relative_name!r}")
            continue

        relative.side_asked_at = now
        result["asked"] += 1

        parent_name = (answers.get(relative_name) or "").strip()
        if not parent_name:
            continue
        parent = await resolve(parent_name)
        if parent is None or parent.id == relative.id:
            continue

        # A grandparent is the PARENT of that parent; an aunt or uncle is their
        # SIBLING. Same question, same answer shape, different edge.
        edge = (
            PARENT_RELATION
            if (kinds or {}).get(relative_name) == GRANDPARENT_RELATION
            else SIBLING_RELATION
        )
        db.add(
            EntityRelation(
                from_entity_id=relative.id,
                to_entity_id=parent.id,
                relation_type=edge,
                source_segment_id=segment_id,
                origin="confirmation",
            )
        )
        result["relations"] += 1

    await db.flush()
    return result


async def clear_ask_once_stamps_for_segment(
    db: AsyncSession, segment_id: str
) -> Dict[str, int]:
    """Un-ask anything whose ANSWER this segment held.

    An ask-once stamp lives on the entity; the answer it recorded lives in
    `entity_relations`, scoped to the recording open when it was given.
    Deleting that recording cascades the relations away — and left alone, the
    stamp says "already asked" about a question whose answer no longer exists.
    The producer can never be asked again and the tree can never draw the line.

    That happened once with parentage and cost five recordings. Both stamps are
    cleared here so it cannot happen a second time with sides.

    Only people whose relations came from THIS segment are cleared. Someone
    asked who SKIPPED keeps their stamp: nothing they said was destroyed.
    """
    cleared = {"parentage": 0, "side": 0}

    for relation_type, column, key in (
        (PARENT_RELATION, Entity.parentage_asked_at, "parentage"),
        (SIBLING_RELATION, Entity.side_asked_at, "side"),
    ):
        rows = (
            await db.execute(
                select(
                    EntityRelation.from_entity_id, EntityRelation.to_entity_id
                ).where(
                    EntityRelation.source_segment_id == segment_id,
                    EntityRelation.relation_type == relation_type,
                    EntityRelation.origin == "confirmation",
                )
            )
        ).all()
        # The stamped party is the one the question was ABOUT: the child for a
        # parentage answer, the relative for a side answer.
        affected = {to_id for _from, to_id in rows} if relation_type == PARENT_RELATION \
            else {from_id for from_id, _to in rows}
        if not affected:
            continue
        entities = list(
            (
                await db.execute(
                    select(Entity).where(Entity.id.in_(affected), column.isnot(None))
                )
            ).scalars().all()
        )
        for entity in entities:
            setattr(entity, column.key, None)
        cleared[key] = len(entities)

    await db.flush()
    return cleared


async def _find_entity(
    db: AsyncSession, producer_id: str, normalized: str
) -> Optional[Entity]:
    return (
        await db.execute(
            select(Entity)
            .where(Entity.producer_id == producer_id)
            .where(Entity.normalized_name == normalized)
        )
    ).scalar_one_or_none()


def _mark_year_asked(entity: Entity, asked: bool) -> None:
    """Stamp that the producer was offered this entity's year question.

    Set once and never cleared, whether or not they answered — that stamp is
    the whole mechanism preventing the same question reappearing on every
    later recording that happens to mention the same name.
    """
    if asked and entity.year_asked_at is None:
        entity.year_asked_at = datetime.now(timezone.utc)


def _maybe_set_year(entity: Entity, year: Optional[int]) -> None:
    """Fill in a year we did not have; never overwrite one we did.

    Only reachable when the producer typed one — the extractor never supplies
    years. The screen asks ONLY about entities with no year (see
    analysis_graph.year_questions), so an existing value means a second
    recording answered the same question and the first answer stands rather
    than being re-decided by ingest order. Correcting a wrong year is a
    separate, deliberate action, not a side effect of recording again.
    """
    if year is not None and entity.year_start is None:
        entity.year_start = year


def _maybe_upgrade_type(
    entity: Entity, new_type: str, *, confirmed: bool = False
) -> Optional[Tuple[str, str]]:
    """Fill in a type we did not have; never let an EXTRACTOR overwrite one we
    did; always let the PRODUCER. Returns (old, new) when it changed.

    'other' is the fallback for an extraction that could not classify, so a
    later recording that DOES classify the same name is strictly more
    information and worth taking.

    A disagreement between two extractions is deliberately not resolved here —
    this function's long-standing rule, and its reason was always that the
    disagreement "is a question for the producer, not something to settle by
    whichever recording was ingested last."

    `confirmed=True` is the producer answering that question. Honouring it is
    what the rule was FOR; ignoring it meant the confirmation screen asked
    "place or organisation?", accepted the answer, discarded it, and said
    nothing — observed live on הכפר הירוק, where the producer chose place and
    the entity stayed organisation with no feedback. A confirmed answer that
    has no effect is worse than not asking.

    `is_self` is the one exception, and it is a hard one: the self-entity must
    stay a person or `ck_entities_self_is_person` rejects the write. A
    transcript that happens to name the producer must never be able to retype
    them, however the answer arrived.
    """
    if entity.is_self:
        return None

    if entity.type == "other" and new_type != "other":
        old = entity.type
        entity.type = new_type
        return (old, new_type)

    if new_type == "other" or entity.type == new_type:
        return None

    if confirmed:
        old = entity.type
        entity.type = new_type
        logger.info(
            "entity_type_resolved_by_producer",
            extra={
                "entity_id": entity.id,
                "entity_name": entity.name,
                "was": old,
                "now": new_type,
            },
        )
        return (old, new_type)

    logger.info(
        "entity_type_disagreement",
        extra={
            "entity_id": entity.id,
            "entity_name": entity.name,
            "kept_type": entity.type,
            "proposed_type": new_type,
        },
    )
    return None
