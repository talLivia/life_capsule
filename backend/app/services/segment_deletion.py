"""Complete removal of a recording and everything derived from it.

ONE implementation, used by both paths that destroy recordings:
  * deleting a single take — DELETE /segments/{id} in interview.py
  * "reset all my data" — users.py

Ingest does NOT call this. A question holds several takes, so recording again
adds one; discarding a take is an explicit delete by the producer.

Writing these separately is how orphans happen: the two paths drift, one
forgets a store, and data survives that the user believes is gone. A recording
fans out into Postgres rows, a stored file, entity mentions, and derived
caches — so "delete" has to mean all of them.

WHAT IS DELETED, and how each is verified:

  RawSegment + TranscriptChunks — chunks cascade at BOTH layers (ORM
    cascade="all, delete-orphan" and an ondelete="CASCADE" FK), so they go
    whether deletion runs through the ORM or raw SQL.

  The stored video — by its own `video_key`.

  Its entity mentions — by FK cascade, in the SAME transaction as the row.
    Then any entity no recording mentions any more, via one orphan sweep.
    An entity another recording still mentions survives, which used to be
    Graphiti's "drop only when the MENTIONS count is 1" bookkeeping and is
    now a NOT EXISTS the engine enforces. Nothing to assert afterwards: the
    cascade cannot half-happen.

  Archive/entity-map/unit caches — invalidated, and re-warmed once at the end
    of a batch rather than per segment.

NOT deleted, deliberately: assembled clips already in storage. Their cache key
is a digest of the SELECTED RANGES, so once a segment is gone retrieval can
never select those ranges again and the old key is unreachable — a stale clip
cannot be served. What remains is dead bytes on disk and Redis keys that
expire on their own TTL. The storage service exposes no prefix listing, so
enumerating them would mean new machinery for a non-correctness problem.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import InterviewSession, RawSegment
from app.services import entity_store
from app.services.storage import storage_service

logger = logging.getLogger(__name__)


@dataclass
class DeletionResult:
    """What actually happened, so callers can report it rather than assume."""

    segments_deleted: int = 0
    # Entities left with no mentions once this segment's were cascaded away.
    # Renamed from `episodes_removed`: it counted Graphiti episodes, which
    # were per-segment, so it could only ever equal segments_deleted. This
    # counts something that actually varies and is worth reporting.
    entities_removed: int = 0
    files_deleted: int = 0
    failures: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


async def delete_segment_data(
    segment_id: str, group_id: str, *, warm_cache: bool = True
) -> DeletionResult:
    """Remove one recording and everything derived from it."""
    result = await _delete_segments([segment_id], group_id)
    await _refresh_caches(group_id, warm=warm_cache)
    return result


async def delete_all_producer_recordings(
    group_id: str, *, warm_cache: bool = True
) -> DeletionResult:
    """Every recording for a producer — the "reset my data" path.

    Deliberately scoped to recordings and their derivatives. The account,
    avatars and voice samples are untouched: they are not derived from
    recordings, and destroying them would make a data reset indistinguishable
    from deleting the account."""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(RawSegment.id)
                .join(InterviewSession, RawSegment.interview_session_id == InterviewSession.id)
                .where(InterviewSession.user_id == group_id)
            )
        ).scalars().all()

    result = await _delete_segments(list(rows), group_id)
    await _refresh_caches(group_id, warm=warm_cache)
    logger.info(
        "producer_data_reset",
        extra={
            "group_id": group_id,
            "segments": result.segments_deleted,
            "entities": result.entities_removed,
            "failures": len(result.failures),
        },
    )
    return result


async def _delete_segments(segment_ids: List[str], group_id: str) -> DeletionResult:
    result = DeletionResult()
    if not segment_ids:
        return result

    for segment_id in segment_ids:
        # File first, then the row. Ordering matters: the row holds the
        # video_key, so losing it first would strand the file.
        async with AsyncSessionLocal() as db:
            segment = (
                await db.execute(select(RawSegment).where(RawSegment.id == segment_id))
            ).scalar_one_or_none()
            if segment is None:
                continue

            video_key: Optional[str] = segment.video_key
            if video_key:
                try:
                    await storage_service.delete_file(video_key)
                    result.files_deleted += 1
                except Exception as e:
                    # A missing file is fine — the point is that it's gone.
                    logger.warning(f"Could not delete stored file {video_key}: {e}")

            # Deleting the row cascades to its entity_mentions, then the sweep
            # removes any entity no recording mentions any more. ONE
            # transaction, where this used to be a two-database dance with no
            # way to make both halves succeed or fail together.
            await db.delete(segment)  # chunks and entity_mentions cascade
            await db.flush()
            result.entities_removed += await entity_store.delete_orphaned_entities(
                db, group_id
            )
            await db.commit()
            result.segments_deleted += 1

    return result


async def _refresh_caches(group_id: str, *, warm: bool) -> None:
    """Derived caches must not outlive the data they describe. Imported here
    rather than at module scope to avoid a circular import (retrieval imports
    entity_store, which this module also uses)."""
    try:
        from app.services.full_archive_retrieval import (
            invalidate_archive_cache,
            warm_archive_cache,
        )

        invalidate_archive_cache(group_id)
        if warm:
            await warm_archive_cache(group_id)
    except Exception as e:
        logger.warning(f"Could not refresh archive cache for {group_id}: {e}")
