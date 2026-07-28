"""What the system understood from ONE recording, assembled in one place.

This exists so the producer can SEE what was extracted from each take and
catch a mistake — a misheard name, a person who was missed — while they still
remember the recording, instead of discovering it later through a bad answer.

READ-ONLY. Nothing here writes; correcting what it shows is a separate
feature, deliberately not built yet.

WHY A SERVICE RATHER THAN A QUERY IN THE ENDPOINT: the pieces live in three
different places — transcript and topic tags in Postgres, entities in
Postgres too since the Graphiti migration, and unit count is not stored at all
but derived by the same splitter retrieval uses. When entities moved,
`_load_entities` below was the ONLY function that changed: the endpoint, the
response shape and the UI never knew where entities lived, which is the entire
point of routing this through here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InterviewSession, RawSegment, TranscriptChunk
from app.services import entity_store

logger = logging.getLogger(__name__)


@dataclass
class ExtractedEntity:
    """One thing the extractor recognised in this recording.

    `kind` was None for every entity while entities lived in the graph, which
    stored a single generic `Entity` label with no person/place/organisation
    distinction — labelling them then would have meant inventing a
    classification the system never made, and on a screen whose whole purpose
    is showing what the system actually understood, a confident wrong label is
    worse than no label. The field was left in place so that typed entities
    could land without anything downstream changing shape.

    They have. `kind` now carries `entities.type` — a real stored value, not
    a guess — so this is the classification the system genuinely made, and the
    producer can see a wrong one.
    """

    name: str
    summary: Optional[str] = None
    kind: Optional[str] = None


@dataclass
class SegmentExtraction:
    segment_id: str
    question_asked: str
    status: str
    transcript: Optional[str] = None
    topic_tags: List[str] = field(default_factory=list)
    unit_count: int = 0
    entities: List[ExtractedEntity] = field(default_factory=list)
    # True when analysis hasn't finished, so the UI can say "still working"
    # rather than presenting an empty extraction as a finished one — the
    # difference between "we found nothing" and "we haven't looked yet".
    still_processing: bool = False
    # Set when the entity store could not be reached. The rest of the
    # extraction is still worth showing, but an empty entity list would
    # otherwise read as "no people found", which is a very different claim.
    entities_unavailable: bool = False


async def get_segment_extraction(
    db: AsyncSession, segment_id: str, group_id: str
) -> Optional[SegmentExtraction]:
    """Everything derived from one recording. None if it isn't this
    producer's or doesn't exist — callers turn that into a 404.

    Takes the caller's session rather than opening its own: this is only ever
    reached from a request that already has one, so a second connection would
    be pure waste (and would read from a different engine than the caller,
    which is exactly the sort of thing that only shows up under test).
    """
    segment = (
        await db.execute(
            select(RawSegment)
            .join(
                InterviewSession,
                RawSegment.interview_session_id == InterviewSession.id,
            )
            .where(RawSegment.id == segment_id, InterviewSession.user_id == group_id)
        )
    ).scalar_one_or_none()
    if segment is None:
        return None

    chunks = list(
        (
            await db.execute(
                select(TranscriptChunk)
                .where(TranscriptChunk.raw_segment_id == segment_id)
                .order_by(TranscriptChunk.sequence_index)
            )
        )
        .scalars()
        .all()
    )

    result = SegmentExtraction(
        segment_id=segment.id,
        question_asked=segment.question_asked,
        status=segment.status,
        transcript=segment.transcript,
        topic_tags=list(segment.topic_tags or []),
        unit_count=_count_units(segment, chunks),
        still_processing=segment.status not in ("ready", "analyzed", "failed"),
    )

    try:
        result.entities = await _load_entities(db, segment_id, group_id)
    except Exception as e:
        # Kept even though entities now come from the same database as the
        # transcript, so a separate store can no longer be independently down.
        # `entities_unavailable` is a real state the UI renders, and the panel
        # still should not lose the transcript — the part most likely to
        # reveal a mishearing — because the entity join failed.
        logger.warning(f"Could not load entities for segment {segment_id}: {e}")
        result.entities_unavailable = True

    return result


def _count_units(segment: RawSegment, chunks: List[TranscriptChunk]) -> int:
    """How many utterance units this recording was split into.

    Derived, never stored — and derived by the SAME splitter retrieval uses,
    imported rather than reimplemented. A second copy of the pause-threshold
    logic would drift, and then this screen would confidently report a number
    that no longer describes how the recording is actually cut.

    Imported inside the function to avoid a circular import at module scope
    (full_archive_retrieval imports entity_store, which this module uses too)
    — the same reason segment_deletion defers its import.
    """
    if not chunks:
        return 0
    try:
        from app.services.full_archive_retrieval import (
            ArchiveSegment,
            _split_segment_into_units,
        )

        units, _ = _split_segment_into_units(
            ArchiveSegment(segment=segment, chunks=chunks), 1
        )
        return len(units)
    except Exception as e:
        logger.warning(f"Could not count units for segment {segment.id}: {e}")
        return 0


async def _load_entities(
    db: AsyncSession, segment_id: str, group_id: str
) -> List[ExtractedEntity]:
    """THE migration seam — and it held. This is the only function that
    changed when entities moved from Graphiti to Postgres: the endpoint, the
    response shape and the UI did not know where entities lived, which was the
    entire point of routing this through here.

    Returns names WITH their summaries, not names alone. The summary is what
    makes this screen useful for catching a mistake — "ניר: אח של הדובר"
    ("Nir: brother of the speaker") shows not just that a name was picked up
    but what the system decided it MEANS, which is where a wrong-but-plausible
    extraction actually shows itself.

    The summary is now THIS recording's rather than the entity's single
    consolidated one, so two recordings mentioning the same person no longer
    show identical text — a visible improvement on exactly the screen whose
    job is revealing what the system understood about one recording.
    """
    return [
        ExtractedEntity(name=name, kind=kind, summary=summary)
        for name, kind, summary in await entity_store.get_segment_entities(
            db, segment_id, group_id
        )
    ]
