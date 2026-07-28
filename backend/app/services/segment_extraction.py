"""What the system understood from ONE recording, assembled in one place.

This exists so the producer can SEE what was extracted from each take and
catch a mistake — a misheard name, a person who was missed — while they still
remember the recording, instead of discovering it later through a bad answer.

READ-ONLY. Nothing here writes; correcting what it shows is a separate
feature, deliberately not built yet.

WHY A SERVICE RATHER THAN A QUERY IN THE ENDPOINT: the pieces live in three
different places right now — transcript and topic tags in Postgres, entities
in Graphiti, and unit count is not stored at all but derived by the same
splitter retrieval uses. Entities are due to move from Graphiti into Postgres.
When that happens, `_load_entities` below is the ONLY function that changes:
the endpoint, the response shape and the UI do not know where entities live,
which is the entire point of routing this through here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InterviewSession, RawSegment, TranscriptChunk
from app.services import graph_memory

logger = logging.getLogger(__name__)


@dataclass
class ExtractedEntity:
    """One thing the extractor recognised in this recording.

    `kind` is None for every entity today and that is HONEST, not a stub:
    the graph stores a single generic `Entity` label with no person/place/
    organisation distinction, so grouping them by type would mean inventing
    a classification the system never made. On a screen whose whole purpose
    is showing what the system actually understood, a confident wrong label
    is worse than no label. The field is here so that when typed entities do
    land, nothing downstream has to change shape.
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
        result.entities = await _load_entities(segment_id, group_id)
    except Exception as e:
        # A graph that is down must not take the whole panel with it: the
        # transcript is the part most likely to reveal a mishearing, and it
        # came from Postgres.
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
    (full_archive_retrieval imports graph_memory, which this module uses too)
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


async def _load_entities(segment_id: str, group_id: str) -> List[ExtractedEntity]:
    """THE migration seam. Entities live in Graphiti today; when they move to
    Postgres, this function is what gets rewritten and nothing above or
    beyond it should need to.

    Returns names WITH their summaries, not names alone. The summary is what
    makes this screen useful for catching a mistake — "ניר: אח של הדובר"
    ("Nir: brother of the speaker") shows not just that a name was picked up
    but what the system decided it MEANS, which is where a
    wrong-but-plausible extraction actually shows itself.
    """
    graphiti = graph_memory.get_graphiti()
    query = """
        MATCH (ep:Episodic {name: $name})-[:MENTIONS]->(e:Entity)
        WHERE ep.group_id = $group_id
        RETURN DISTINCT e.name AS name, e.summary AS summary
        ORDER BY name
    """
    result = await graphiti.driver.execute_query(
        query, name=f"segment-{segment_id}", group_id=group_id, routing_="r"
    )
    return [
        ExtractedEntity(name=r["name"], summary=r["summary"]) for r in result.records
    ]
