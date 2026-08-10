"""
Read-only entity views: the family tree, and the moments behind one person.

Phase 4 of docs/FAMILY_TREE_TIMELINE.md.

Its own router rather than another endpoint on `interview.py`, which is about
recording an interview — a tree is a view over the archive that outlives any
one interview session, and the timeline (Phase 5) will read from here too.

This was read-only until docs/TREE_EDITING.md: extraction proposals are
one-shot, so a relation the extractor missed — or got wrong — could never be
fixed afterwards, and a person the tree could not place stayed unplaceable
forever. The one write here is deliberately narrow: set the relation between
two people who are already in the archive.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.interview import require_producer
from app.database import get_db
from app.models import User
from app.schemas import EntityMomentResponse, SetRelationRequest, TreeResponse
from app.services import entity_store, family_tree, timeline
from sqlalchemy import select
from app.models import Entity, EntityRelation, RelationType

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/tree", response_model=TreeResponse)
async def get_family_tree(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """The producer's family tree: generation rows, edges, and anyone the
    archive knows about but cannot place.

    Always 200, even with no relations at all — an archive that has not
    captured any family yet is an empty tree, not an error, and the page says
    so rather than showing a failure.
    """
    return await family_tree.build_tree(db, user.id)


@router.get("/{entity_id}/moments", response_model=list[EntityMomentResponse])
async def get_entity_moments(
    entity_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """The recordings that mention this person — clicking a name in the tree.

    404 rather than an empty list when the entity is not this producer's: an
    empty list would say "this person was never mentioned", which is a
    different and misleading answer.
    """
    from sqlalchemy import select

    from app.models import Entity

    owned = (
        await db.execute(
            select(Entity.id).where(Entity.id == entity_id, Entity.producer_id == user.id)
        )
    ).scalar_one_or_none()
    if owned is None:
        raise HTTPException(status_code=404, detail="Entity not found")

    return await family_tree.get_entity_moments(db, user.id, entity_id)


@router.get("/timeline")
async def get_timeline(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """The producer's life periods, in the interview's own order.

    Read-only. Empty periods are hidden and counted — a category with no
    recording is a question not yet answered, not a fact about the life.
    """
    return await timeline.build_timeline(db, user.id, user.recording_language)


@router.get("/relation-types")
async def list_relation_types(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """The relation vocabulary a tree edit may choose from.

    Served from the `relation_types` TABLE rather than a list in the client,
    for the reason that table exists: `entity_relations.relation_type` has a FK
    to it, so a picker built from anything else could offer a value the write
    would refuse. Adding a relation type stays a data change.
    """
    rows = (
        await db.execute(
            select(RelationType)
            .where(RelationType.is_tree_edge.is_(True))
            .order_by(RelationType.generation_delta, RelationType.relation_type)
        )
    ).scalars().all()
    return [
        {"value": r.relation_type, "label": r.label_en or r.relation_type.replace("_", " ")}
        for r in rows
    ]


@router.post("/{entity_id}/relations")
async def set_relation(
    entity_id: str,
    payload: SetRelationRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """Set how this person is related to another, REPLACING what contradicts it.

    The governing rule of docs/TREE_EDITING.md: a manual edit always wins. It
    is unprompted, deliberate, and made while looking at the tree it changes —
    so it is not confirmed, and it does not sit alongside the relation it
    contradicts. Two edges the tree cannot both honour would leave it to pick
    one at render, which is precisely what made an earlier chosen parent look
    as though it had failed to save.

    `replaced` comes back rather than being swallowed. Nothing asks first, and
    the producer should still be able to SEE what the edit did.

    Its own endpoint, NOT EntityBatchConfirmRequest: that request answers a
    paused pipeline — validated against one segment's stored questions, and
    submitting it resumes a LangGraph thread. A tree edit has no segment, no
    pause and no thread, and fabricating a pending payload to reuse the shape
    would give one field two meanings.
    """
    owned = (
        await db.execute(
            select(Entity.id).where(Entity.id == entity_id, Entity.producer_id == user.id)
        )
    ).scalar_one_or_none()
    if owned is None:
        # 404 for both "no such person" and "not yours", like DELETE segment.
        raise HTTPException(status_code=404, detail="Person not found")

    # `direction` decides which end this entity is. "חן is the child of רז" and
    # "רז is the parent of חן" are the same fact stated from two ends, and the
    # producer picks whichever reads naturally on the card they opened.
    if payload.direction == "outgoing":
        from_id, to_id = entity_id, payload.other_entity_id
    else:
        from_id, to_id = payload.other_entity_id, entity_id

    try:
        result = await entity_store.set_relation_by_hand(
            db,
            producer_id=user.id,
            from_entity_id=from_id,
            to_entity_id=to_id,
            relation_type=payload.relation_type,
        )
    except ValueError as invalid:
        raise HTTPException(status_code=400, detail=str(invalid))

    # WHICH SIDE OF THE FAMILY — the manual twin of the questionnaire's side
    # question (analysis_graph.side_questions -> entity_store.write_sides).
    # An aunt_uncle or grandparent edge places somebody in a row and says
    # nothing about which parent they attach to, so without this second edge
    # the parents' row is boxes with nothing marking whose sibling (or whose
    # parent) the new arrival is. Same answer, same resulting edge, just
    # stated from the tree instead of after a recording.
    if payload.side_parent_id:
        if payload.relation_type not in ("aunt_uncle", "grandparent"):
            raise HTTPException(
                status_code=400,
                detail="A side of the family only applies to an aunt/uncle or grandparent.",
            )
        # The relative is the SUBJECT end ("<from> is the <type> of <to>"),
        # and the side parent must be a recorded parent of the OTHER end —
        # the questionnaire offers exactly that list, and accepting anyone
        # else would attach the relative to a branch nobody stated.
        is_parent_of_anchor = (
            await db.execute(
                select(Entity.id)
                .join(EntityRelation, EntityRelation.from_entity_id == Entity.id)
                .where(
                    Entity.id == payload.side_parent_id,
                    Entity.producer_id == user.id,
                    EntityRelation.to_entity_id == to_id,
                    EntityRelation.relation_type == "parent",
                )
            )
        ).scalar_one_or_none()
        if is_parent_of_anchor is None:
            raise HTTPException(
                status_code=400,
                detail="The side must be one of their recorded parents.",
            )
        try:
            side_result = await entity_store.set_relation_by_hand(
                db,
                producer_id=user.id,
                from_entity_id=from_id,
                to_entity_id=payload.side_parent_id,
                # An aunt or uncle is that parent's SIBLING; a grandparent is
                # that parent's PARENT — the identical mapping write_sides
                # applies to the questionnaire's answer.
                relation_type=(
                    "sibling" if payload.relation_type == "aunt_uncle" else "parent"
                ),
            )
        except ValueError as invalid:
            raise HTTPException(status_code=400, detail=str(invalid))
        result["replaced"] = [*result["replaced"], *side_result["replaced"]]

    # Stamp the ask-once questions as settled. The producer has just answered
    # by hand what those questions exist to ask, and a question that comes back
    # afterwards to contradict a deliberate edit is the drift this whole change
    # is about.
    await entity_store.mark_placement_asked(db, [from_id, to_id])

    await db.commit()
    return result


@router.delete("/{entity_id}/relations/{relation_id}")
async def remove_relation(
    entity_id: str,
    relation_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """Remove ONE recorded relation, from a person's card on the tree.

    The counterpart `set_relation` cannot reach: setting a relation replaces
    what contradicts it, but a relation that is simply WRONG — with nothing
    true to put in its place — had no way out. Reported live: the
    questionnaire attached אילן to ג'ולי, and the only "fix" available was
    inventing some other relation between them.

    Scoped to an entity the producer owns AND that the edge actually touches:
    the entity id is how the card asked, and checking it keeps a leaked
    relation id from deleting an edge between two strangers.

    ⚠️ Removing a RECORDING-origin edge does not stop a re-analysis of that
    recording from proposing the relation again — deletion removes the row,
    not the sentence it came from. Re-analysis is rare and manual; a tombstone
    for every deleted edge is complexity this page does not need yet.
    """
    owned = (
        await db.execute(
            select(Entity.id).where(Entity.id == entity_id, Entity.producer_id == user.id)
        )
    ).scalar_one_or_none()
    if owned is None:
        raise HTTPException(status_code=404, detail="Person not found")

    relation = (
        await db.execute(
            select(EntityRelation)
            .join(Entity, Entity.id == EntityRelation.from_entity_id)
            .where(
                EntityRelation.id == relation_id,
                Entity.producer_id == user.id,
                (EntityRelation.from_entity_id == entity_id)
                | (EntityRelation.to_entity_id == entity_id),
            )
        )
    ).scalars().first()
    if relation is None:
        # Same shape as the 404 above: "no such edge" and "not this person's
        # edge" get one answer.
        raise HTTPException(status_code=404, detail="Relation not found")

    removed = {
        "id": relation.id,
        "from_entity_id": relation.from_entity_id,
        "to_entity_id": relation.to_entity_id,
        "relation_type": relation.relation_type,
        "origin": relation.origin,
    }
    await db.delete(relation)
    await db.commit()
    logger.info(
        f"Relation removed by hand: {removed['from_entity_id'][:8]} "
        f"-{removed['relation_type']}-> {removed['to_entity_id'][:8]} "
        f"(origin {removed['origin']})"
    )
    return {"removed": removed}
