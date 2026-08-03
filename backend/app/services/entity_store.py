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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entity, EntityMention, EntityRelation, RelationType
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
) -> List[Tuple[str, str, Optional[str]]]:
    """(name, type, summary) for one segment — what the extraction panel shows.

    The summary is THIS recording's, which is the entire point of the panel:
    "ניר: אח של הדובר" shows not just that a name was picked up but what the
    system decided it MEANS, which is where a wrong-but-plausible extraction
    reveals itself. Under the graph this was the entity's single consolidated
    summary, so every recording mentioning it showed the same text.
    """
    rows = await db.execute(
        select(Entity.name, Entity.type, EntityMention.summary)
        .join(EntityMention, EntityMention.entity_id == Entity.id)
        .where(EntityMention.raw_segment_id == segment_id)
        .where(Entity.producer_id == producer_id)
        .order_by(Entity.name)
    )
    return [(name, type_, summary) for name, type_, summary in rows]


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
            select(Entity.id, Entity.name, similarity)
            .where(Entity.producer_id == producer_id)
            .where(~Entity.is_self)
            .order_by(similarity.desc(), Entity.name)
            .limit(limit)
        )
        ranked = [(eid, ename) for eid, ename, _ in rows]
    else:
        # SQLite (tests) has no pg_trgm. Rank in Python over the producer's
        # entities — correct, and affordable at any size this ever reaches,
        # but NOT what production runs, which is why the branch is explicit
        # rather than hidden behind a helper that pretends they are the same.
        rows = await db.execute(
            select(Entity.id, Entity.name, Entity.normalized_name)
            .where(Entity.producer_id == producer_id)
            .where(~Entity.is_self)
        )
        scored = sorted(
            (
                (SequenceMatcher(None, key, norm).ratio(), ename, eid)
                for eid, ename, norm in rows
            ),
            key=lambda t: (-t[0], t[1]),
        )
        ranked = [(eid, ename) for _, ename, eid in scored[:limit]]

    candidates = [{"uuid": eid, "name": ename, "summary": None} for eid, ename in ranked]
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
# These two relation names are hardcoded, unlike everything else that reads
# the `relation_types` vocabulary from the database. That is deliberate: the
# question being asked is literally "is your parent also their parent", which
# is about those two relations specifically. It is not generic over the
# vocabulary, and pretending otherwise by deriving it from `generation_delta`
# would also sweep in step-parents and spouses, which is exactly wrong here.
PARENT_RELATION = "parent"
SIBLING_RELATION = "sibling"


async def parentage_candidates(db: AsyncSession, producer_id: str) -> Dict[str, list]:
    """The producer's recorded parents, and the siblings still to ask about.

    Returns `{"parents": [...], "siblings": [...]}`, each a list of
    `{"id", "name"}`. Empty siblings means there is nothing to ask.

    A sibling qualifies when all of these hold:
      * they are the producer's sibling by a confirmed relation;
      * they have no parent of their own recorded;
      * they have never been asked (`parentage_asked_at IS NULL`).

    With no recorded parents for the producer there is nothing to offer as an
    answer, so the question is not asked at all rather than asked with an
    empty list of options.
    """
    self_entity = (
        await db.execute(
            select(Entity).where(Entity.producer_id == producer_id, Entity.is_self)
        )
    ).scalars().first()
    if self_entity is None:
        return {"parents": [], "siblings": []}

    parents = list(
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
    if not parents:
        return {"parents": [], "siblings": []}

    # Sibling is symmetric and stored as ONE directed row, so the producer may
    # be on either end. Checking only one direction would miss half of them.
    sibling_rows = (
        await db.execute(
            select(EntityRelation.from_entity_id, EntityRelation.to_entity_id).where(
                EntityRelation.relation_type == SIBLING_RELATION,
                (EntityRelation.from_entity_id == self_entity.id)
                | (EntityRelation.to_entity_id == self_entity.id),
            )
        )
    ).all()
    sibling_ids = {
        (to_id if from_id == self_entity.id else from_id)
        for from_id, to_id in sibling_rows
    }
    sibling_ids.discard(self_entity.id)
    if not sibling_ids:
        return {"parents": [], "siblings": []}

    # Anyone who already has a parent recorded is not asked about — the tree
    # can already draw them, whether that parent is the producer's or not.
    with_parents = set(
        (
            await db.execute(
                select(EntityRelation.to_entity_id).where(
                    EntityRelation.to_entity_id.in_(sibling_ids),
                    EntityRelation.relation_type == PARENT_RELATION,
                )
            )
        ).scalars().all()
    )

    siblings = list(
        (
            await db.execute(
                select(Entity)
                .where(
                    Entity.id.in_(sibling_ids - with_parents),
                    Entity.parentage_asked_at.is_(None),
                )
                .order_by(Entity.name)
            )
        ).scalars().all()
    )
    if not siblings:
        return {"parents": [], "siblings": []}

    return {
        "parents": [{"id": p.id, "name": p.name} for p in parents],
        "siblings": [{"id": s.id, "name": s.name} for s in siblings],
    }


async def write_parentage(
    db: AsyncSession,
    *,
    producer_id: str,
    segment_id: str,
    asked_sibling_ids: Sequence[str],
    answers: Dict[str, dict],
) -> Dict[str, int]:
    """Apply the parentage answers. Flushes, never commits.

    `answers` is keyed by sibling entity id:
        {"<sibling id>": {"parent_ids": [...], "new_parent_name": "מרים"}}

    EVERY sibling in `asked_sibling_ids` is stamped `parentage_asked_at`,
    answered or not. Skipping is an answer — "I do not know" or "not now" —
    and without recording it the same question returns on every future
    recording until the producer learns to click past the whole screen. This
    is the same rule `year_asked_at` follows, for the same reason.

    Relations are written with `origin="confirmation"`: the producer said this
    on a screen, not in the recording, and the tree must not offer to play a
    recording that never mentions the person.
    """
    now = datetime.now(timezone.utc)
    result = {"relations": 0, "new_parents": 0, "asked": 0}

    asked = list(dict.fromkeys(asked_sibling_ids))
    if not asked:
        return result

    siblings = {
        e.id: e
        for e in (
            await db.execute(
                select(Entity).where(
                    Entity.id.in_(asked), Entity.producer_id == producer_id
                )
            )
        ).scalars().all()
    }

    # Only the producer's own recorded parents may be ticked. A client naming
    # any other entity id would otherwise be able to attach an arbitrary
    # person as somebody's parent.
    offered = {p["id"] for p in (await parentage_candidates(db, producer_id))["parents"]}

    for sibling_id in asked:
        sibling = siblings.get(sibling_id)
        if sibling is None:
            logger.warning(f"parentage answer for unknown sibling {sibling_id}")
            continue

        sibling.parentage_asked_at = now
        result["asked"] += 1

        answer = answers.get(sibling_id) or {}
        parent_ids = [pid for pid in (answer.get("parent_ids") or []) if pid in offered]

        new_name = (answer.get("new_parent_name") or "").strip()
        if new_name:
            normalized = normalize_entity_name(new_name)
            parent = await _find_entity(db, producer_id, normalized) if normalized else None
            if parent is None and normalized:
                parent = Entity(
                    producer_id=producer_id,
                    name=new_name,
                    normalized_name=normalized,
                    type="person",
                )
                db.add(parent)
                await db.flush()
                result["new_parents"] += 1
            if parent is not None:
                parent_ids.append(parent.id)

        for parent_id in dict.fromkeys(parent_ids):
            if parent_id == sibling_id:
                continue  # ck_entity_relations_not_self
            db.add(
                EntityRelation(
                    from_entity_id=parent_id,
                    to_entity_id=sibling_id,
                    relation_type=PARENT_RELATION,
                    source_segment_id=segment_id,
                    origin="confirmation",
                )
            )
            result["relations"] += 1

    await db.flush()
    return result


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
