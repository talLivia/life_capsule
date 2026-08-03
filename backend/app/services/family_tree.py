"""
The family tree — person entities placed in generation rows.

Phase 4 of docs/FAMILY_TREE_TIMELINE.md. Read-only: nothing here writes, and
the page cannot edit the tree.

## The tree never guesses

That rule decides every awkward case below, and all three are real rather than
defensive — the live archive hits the second one today.

  * A person with no path to the root gets NO generation. They are returned
    separately as `unplaced`, not dropped and not parked in row 0. Dropping
    them hides someone the producer talked about; guessing a row draws a
    family that does not exist.
  * A relation type with no `generation_delta` cannot be placed either, even
    though it is marked as a tree edge. Reported, not assumed to be zero.
  * Two paths that disagree about someone's generation are a CONTRADICTION —
    "A is B's parent" in one recording and the reverse in another. The first
    assignment stands (breadth-first from the root, so it is the shortest and
    most direct path), and the conflicting edge is reported rather than
    silently redrawing the tree.

## Why breadth-first from the self-entity

Generation is meaningful only relative to somebody, and the producer is the
only fixed point in their own archive. BFS also means the row someone lands in
comes from their SHORTEST relationship path to the producer, which is the one
a human would use to explain it — "my father's mother", not a chain through
four cousins that happens to arrive first.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entity, EntityRelation, RelationType

logger = logging.getLogger(__name__)


async def _load_nodes(db: AsyncSession, producer_id: str) -> List[Entity]:
    return list(
        (
            await db.execute(
                select(Entity)
                .where(Entity.producer_id == producer_id, Entity.type == "person")
                .order_by(Entity.name)
            )
        ).scalars().all()
    )


async def _load_edges(db: AsyncSession, producer_id: str) -> List[Dict[str, Any]]:
    """Tree-bearing relations for this producer.

    `entity_relations` has no producer_id of its own, so scoping goes through
    the from-entity. Both ends always belong to the same producer — entities
    are per-producer and a relation is written from one recording — so one
    join is enough.
    """
    rows = (
        await db.execute(
            select(EntityRelation, RelationType)
            .join(RelationType, RelationType.relation_type == EntityRelation.relation_type)
            .join(Entity, Entity.id == EntityRelation.from_entity_id)
            .where(Entity.producer_id == producer_id, RelationType.is_tree_edge.is_(True))
            .order_by(EntityRelation.created_at, EntityRelation.id)
        )
    ).all()
    return [
        {
            "from_id": rel.from_entity_id,
            "to_id": rel.to_entity_id,
            "relation_type": rel.relation_type,
            "source_segment_id": rel.source_segment_id,
            "delta": rt.generation_delta,
            "label_en": rt.label_en,
            "label_he": rt.label_he,
            "is_symmetric": rt.is_symmetric,
            "inverse_type": rt.inverse_type,
        }
        for rel, rt in rows
    ]


def _assign_generations(
    root_id: str, edges: List[Dict[str, Any]]
) -> tuple[Dict[str, int], List[Dict[str, Any]]]:
    """Breadth-first from the root. Returns (generation by id, contradictions).

    Every edge is walked in BOTH directions — the schema stores ONE directed
    row per relation and derives the inverse at read time, so a tree that only
    followed `from -> to` would never reach a parent from their child.
    """
    adjacency: Dict[str, List[tuple[str, int, Dict[str, Any]]]] = {}
    for edge in edges:
        if edge["delta"] is None:
            # A tree edge whose type has no generation offset. Placing it
            # would mean inventing one; leaving it out means both people may
            # end up unplaced, which is visible and honest.
            continue
        adjacency.setdefault(edge["from_id"], []).append(
            (edge["to_id"], -edge["delta"], edge)
        )
        adjacency.setdefault(edge["to_id"], []).append(
            (edge["from_id"], edge["delta"], edge)
        )

    generation: Dict[str, int] = {root_id: 0}
    contradictions: List[Dict[str, Any]] = []
    # Every edge is walked from both ends, so one bad edge would otherwise be
    # reported twice — telling the producer two relations disagree when only
    # one does. A contradiction is a property of the EDGE, not of the
    # direction the walk happened to reach it from.
    reported: set = set()
    queue = deque([root_id])

    while queue:
        current = queue.popleft()
        for neighbour, delta, edge in adjacency.get(current, []):
            candidate = generation[current] + delta
            if neighbour not in generation:
                generation[neighbour] = candidate
                queue.append(neighbour)
            elif generation[neighbour] != candidate:
                # Two recordings disagree about where this person sits. The
                # first (shortest-path) assignment stands; the later edge is
                # surfaced rather than silently moving somebody's row.
                key = (
                    edge["from_id"],
                    edge["to_id"],
                    edge["relation_type"],
                    edge["source_segment_id"],
                )
                if key in reported:
                    continue
                reported.add(key)
                contradictions.append(
                    {
                        "from_id": edge["from_id"],
                        "to_id": edge["to_id"],
                        "relation_type": edge["relation_type"],
                        "source_segment_id": edge["source_segment_id"],
                        "kept_generation": generation[neighbour],
                        "implied_generation": candidate,
                    }
                )
    return generation, contradictions


async def build_tree(db: AsyncSession, producer_id: str) -> Dict[str, Any]:
    """Nodes, edges and generation rows for one producer's family tree."""
    nodes = await _load_nodes(db, producer_id)
    edges = await _load_edges(db, producer_id)

    root = next((n for n in nodes if n.is_self), None)
    if root is None:
        # Relations cannot be expressed without a root, so there is nothing to
        # draw and nothing to explain away. scripts/backfill_self_entities.py
        # repairs this; it should not happen for a producer created since the
        # signup hook landed.
        logger.warning(f"Producer {producer_id} has no self-entity; tree is empty")
        return {
            "root_id": None,
            "generations": [],
            "unplaced": [_node_view(n, None) for n in nodes],
            "edges": [],
            "contradictions": [],
            "missing_generation_delta": [],
        }

    generation, contradictions = _assign_generations(root.id, edges)

    placed = [n for n in nodes if n.id in generation]
    unplaced = [n for n in nodes if n.id not in generation]

    rows: Dict[int, List[Dict[str, Any]]] = {}
    for node in placed:
        rows.setdefault(generation[node.id], []).append(
            _node_view(node, generation[node.id])
        )

    return {
        "root_id": root.id,
        # Ascending: ancestors first, so the page renders top-down without
        # having to know that negative means "up".
        "generations": [
            {"generation": g, "people": rows[g]} for g in sorted(rows)
        ],
        # Real people the producer talked about, with no family path to them.
        # Shown separately so nobody is dropped and nobody is misplaced.
        "unplaced": [_node_view(n, None) for n in unplaced],
        "edges": [
            {
                "from_id": e["from_id"],
                "to_id": e["to_id"],
                "relation_type": e["relation_type"],
                "label_en": e["label_en"],
                "label_he": e["label_he"],
                # Every edge knows the recording that established it, so
                # "brother" can play the producer saying so.
                "source_segment_id": e["source_segment_id"],
            }
            for e in edges
        ],
        "contradictions": contradictions,
        "missing_generation_delta": sorted(
            {e["relation_type"] for e in edges if e["delta"] is None}
        ),
    }


def _node_view(entity: Entity, generation: Optional[int]) -> Dict[str, Any]:
    return {
        "id": entity.id,
        "name": entity.name,
        "is_self": bool(entity.is_self),
        "year_start": entity.year_start,
        "year_end": entity.year_end,
        "generation": generation,
    }


async def get_entity_moments(
    db: AsyncSession, producer_id: str, entity_id: str
) -> List[Dict[str, Any]]:
    """The recordings that mention one person — video, transcript, title.

    The same query the timeline's sub-bubbles will use (§3), so the two pages
    share one endpoint rather than growing separate ones that drift.

    Scoped by producer as well as entity: an entity id is a uuid and unguessable,
    but authorisation should not rest on that.
    """
    from app.models import EntityMention, InterviewSession, RawSegment

    rows = (
        await db.execute(
            select(RawSegment, EntityMention.summary)
            .join(EntityMention, EntityMention.raw_segment_id == RawSegment.id)
            .join(Entity, Entity.id == EntityMention.entity_id)
            .join(
                InterviewSession,
                InterviewSession.id == RawSegment.interview_session_id,
            )
            .where(
                EntityMention.entity_id == entity_id,
                Entity.producer_id == producer_id,
                InterviewSession.user_id == producer_id,
            )
            .order_by(RawSegment.created_at)
        )
    ).all()

    return [
        {
            "segment_id": seg.id,
            "question_asked": seg.question_asked,
            "question_id": seg.question_id,
            "video_url": seg.video_url,
            "transcript": seg.transcript,
            "summary": summary,
        }
        for seg, summary in rows
    ]
