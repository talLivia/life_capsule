"""Read helpers for media_assets — the photo side of entity payloads.

One job for now: "the face for each of these entities", in bulk. Kept as its
own seam (like segment_extraction._load_entities was for the entity
migration) so the surfaces that render a portrait — the tree, the extraction
panel, later the entity list (Phase 4) — never know where photos live or how
a primary is chosen.

Always ONE query regardless of how many entities are asked about — the
per-row round-trip shape is exactly what the entity migration measured at
4.1s mean and replaced with 0.37s (PROJECT_STATUS, chunk 3).
"""

import logging
from typing import Dict, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaAsset
from app.services.storage import storage_service

logger = logging.getLogger(__name__)


async def primary_photo_urls(
    db: AsyncSession, entity_ids: Sequence[str]
) -> Dict[str, str]:
    """entity_id -> resolved serving URL of its primary photo.

    Entities without a primary photo are simply absent — the caller renders
    its existing placeholder, which is not an error state. URLs are resolved
    server-side; storage keys never reach a client (the video rule).
    """
    if not entity_ids:
        return {}
    rows = (
        await db.execute(
            select(MediaAsset.entity_id, MediaAsset.storage_key).where(
                MediaAsset.entity_id.in_(list(entity_ids)),
                MediaAsset.is_primary,
            )
        )
    ).all()
    return {
        entity_id: await storage_service.serving_url(storage_key)
        for entity_id, storage_key in rows
    }
