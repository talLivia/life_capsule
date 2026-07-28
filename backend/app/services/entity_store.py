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
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entity, EntityMention
from app.services.entity_extraction import ExtractedEntity
from app.services.entity_names import normalize_entity_name

logger = logging.getLogger(__name__)


@dataclass
class EntityWriteResult:
    entities_created: int = 0
    entities_matched: int = 0
    mentions_written: int = 0
    orphans_removed: int = 0
    # Entities whose type the extractor was torn about. This is the ONLY
    # trigger for asking the producer — see entity_extraction for why it is a
    # runner-up type and not a confidence score. Chunk 4's batched
    # confirmation screen is built from this; for now it is reported and
    # logged so the signal is visible rather than silently discarded.
    needs_confirmation: List[ExtractedEntity] = field(default_factory=list)


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
            db, producer_id=producer_id, extracted=extracted, normalized=normalized
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
        logger.info(
            "entity_type_confirmation_pending",
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
) -> Tuple[Entity, bool]:
    """The merge. Returns (entity, created)."""
    existing = await _find_entity(db, producer_id, normalized)
    if existing is not None:
        _maybe_upgrade_type(existing, extracted.type)
        return existing, False

    entity = Entity(
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
        _maybe_upgrade_type(existing, extracted.type)
        return existing, False
    return entity, True


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


def _maybe_upgrade_type(entity: Entity, new_type: str) -> None:
    """Fill in a type we did not have; never overwrite one we did.

    'other' is the fallback for an extraction that could not classify, so a
    later recording that DOES classify the same name is strictly more
    information and worth taking. A real disagreement (one recording says
    place, another says organisation) is NOT resolved here — it is a question
    for the producer, not something to settle by whichever recording was
    ingested last. Keeping the first answer also keeps `is_self` safe: the
    self-entity is 'person', so it can never be downgraded by a transcript
    that happens to name the producer.
    """
    if entity.type == "other" and new_type != "other":
        entity.type = new_type
        return
    if new_type != "other" and entity.type != new_type:
        logger.info(
            "entity_type_disagreement",
            extra={
                "entity_id": entity.id,
                "entity_name": entity.name,
                "kept_type": entity.type,
                "proposed_type": new_type,
            },
        )
