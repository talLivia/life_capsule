"""Complete removal of a recording and everything derived from it.

ONE implementation, used by both paths that destroy recordings:
  * deleting a single take — DELETE /segments/{id} in interview.py
  * "reset all my data" — users.py

Ingest does NOT call this. A question holds several takes, so recording again
adds one; discarding a take is an explicit delete by the producer.

Writing these separately is how orphans happen: the two paths drift, one
forgets a store, and data survives that the user believes is gone. A recording
fans out into Postgres rows, a stored file, a Graphiti episode with its
entities, and derived caches — so "delete" has to mean all of them.

WHAT IS DELETED, and how each is verified:

  RawSegment + TranscriptChunks — chunks cascade at BOTH layers (ORM
    cascade="all, delete-orphan" and an ondelete="CASCADE" FK), so they go
    whether deletion runs through the ORM or raw SQL.

  The stored video — by its own `video_key`.

  The Graphiti episode(s) — via graph_memory.remove_episodes_for_segment,
    which deletes only entities whose sole source was this segment and
    ASSERTS afterwards that no episode with this segment's name remains.
    A segment may have several episodes (older re-records duplicated them),
    so all are removed, not the first.

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
from app.services import graph_memory
from app.services.storage import storage_service

logger = logging.getLogger(__name__)


@dataclass
class DeletionResult:
    """What actually happened, so callers can report it rather than assume."""

    segments_deleted: int = 0
    episodes_removed: int = 0
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
            "episodes": result.episodes_removed,
            "failures": len(result.failures),
        },
    )
    return result


async def _delete_segments(segment_ids: List[str], group_id: str) -> DeletionResult:
    result = DeletionResult()
    if not segment_ids:
        return result

    for segment_id in segment_ids:
        # Graph first, then file, then the row. Ordering matters: the row
        # holds the video_key, so losing it first would strand the file.
        try:
            result.episodes_removed += await graph_memory.remove_episodes_for_segment(
                segment_id, group_id=group_id
            )
        except Exception as e:
            msg = f"graph cleanup failed for {segment_id}: {e}"
            logger.error(msg)
            result.failures.append(msg)

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

            await db.delete(segment)  # chunks cascade
            await db.commit()
            result.segments_deleted += 1

    return result


async def _refresh_caches(group_id: str, *, warm: bool) -> None:
    """Derived caches must not outlive the data they describe. Imported here
    rather than at module scope to avoid a circular import (retrieval imports
    graph_memory, which this module also uses)."""
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
