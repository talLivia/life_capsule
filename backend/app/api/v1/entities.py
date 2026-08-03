"""
Read-only entity views: the family tree, and the moments behind one person.

Phase 4 of docs/FAMILY_TREE_TIMELINE.md.

Its own router rather than another endpoint on `interview.py`, which is about
recording an interview — a tree is a view over the archive that outlives any
one interview session, and the timeline (Phase 5) will read from here too.

Nothing here writes. Editing the tree is explicitly out of scope, and the read
path staying read-only is what makes it safe to open up later without
revisiting authorisation.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.interview import require_producer
from app.database import get_db
from app.models import User
from app.schemas import EntityMomentResponse, TreeResponse
from app.services import family_tree

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
