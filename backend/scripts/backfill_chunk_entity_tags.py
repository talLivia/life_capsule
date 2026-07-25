"""
One-time backfill for TranscriptChunk.mentioned_entities (Prompt 11 field).

Root cause (confirmed against the live archive, not guessed): EVERY chunk
in this archive predates check_entities_node's chunk-tagging step
(_tag_chunks_with_entities) — all 12 TranscriptChunk rows were created by
scripts/backfill_transcript_chunks_prompt11.py, which only transcribes,
chunks, embeds, and topic-tags; it never calls check_entities_node at all.
Confirmed via a direct live check: segment 502fb283's episode in Graphiti
already correctly lists "אילנה" (and 7 other entities) — Graphiti's own
extraction succeeded fully. The gap is ONLY that this graph-side result
was never propagated down to the chunk level, not a missed extraction.

This script therefore does NOT re-run entity extraction (no new LLM call,
no risk of re-triggering human-in-the-loop disambiguation) — it reuses
each segment's ALREADY-EXTRACTED entities straight from Graphiti
(graph_memory.get_episode_entity_names, the same source expand_graph/
expand_graph_chunks already trust) and applies analysis_graph.py's
existing _tag_chunks_with_entities helper, the exact same substring-tagging
logic the live pipeline uses today. Only 'ready' segments are eligible —
an episode only exists in Graphiti once finalize_ingest_node has run.

Idempotent: only segments with at least one chunk whose mentioned_entities
is still null/empty are considered, so re-running after fixing a
transient failure (e.g. a Neo4j hiccup) is safe and won't re-tag
already-correct chunks.

Usage: python scripts/backfill_chunk_entity_tags.py   (run from backend/,
with a real .env providing DATABASE_URL, GEMINI_API_KEY/NEO4J credentials).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Windows consoles default to cp1252, which can't print Hebrew transcripts.
sys.stdout.reconfigure(encoding="utf-8")

# psycopg3's async mode can't run under Windows' default ProactorEventLoop —
# must be set before any other asyncio-touching import (matches main.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import joinedload  # noqa: E402

from app.analysis_graph import _tag_chunks_with_entities  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.models import InterviewSession, RawSegment, TranscriptChunk, User  # noqa: E402
from app.services import graph_memory  # noqa: E402


async def _chunk_entity_tag_counts() -> tuple[int, int]:
    """(total chunks, chunks with a non-empty mentioned_entities) — used to
    report a real before/after, not an estimate."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TranscriptChunk.mentioned_entities))
        rows = result.scalars().all()
    total = len(rows)
    populated = sum(1 for r in rows if r)
    return total, populated


async def _segments_needing_backfill() -> list[tuple[RawSegment, str]]:
    """'ready' segments (an episode only exists in Graphiti once
    finalize_ingest_node has run) that have at least one TranscriptChunk
    with a null/empty mentioned_entities. Returns (segment, group_id) pairs
    — group_id is the segment's own producer's user_id, exactly like
    check_entities_node scopes its own graph_memory calls."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(RawSegment)
            .options(joinedload(RawSegment.interview_session).joinedload(InterviewSession.user))
            .where(RawSegment.status == "ready")
        )
        segments = result.unique().scalars().all()

        # Filtered in Python, not SQL — the column is plain `json` (not
        # `jsonb`), so a portable "is this list null/empty" check can't
        # rely on a Postgres JSONB-only function like jsonb_array_length.
        chunk_result = await db.execute(
            select(TranscriptChunk.raw_segment_id, TranscriptChunk.mentioned_entities)
        )
        untagged_segment_ids = {row.raw_segment_id for row in chunk_result if not row.mentioned_entities}

    pairs = []
    for seg in segments:
        if seg.id not in untagged_segment_ids:
            continue
        user: User = seg.interview_session.user
        pairs.append((seg, user.id))
    return pairs


async def main() -> None:
    before_total, before_populated = await _chunk_entity_tag_counts()
    print(f"Before: {before_populated}/{before_total} chunks have mentioned_entities populated.")

    candidates = await _segments_needing_backfill()
    print(f"Found {len(candidates)} segment(s) needing chunk entity-tag backfill.")

    processed = 0
    failed: list[tuple[str, str]] = []
    no_entities: list[str] = []

    for segment, group_id in candidates:
        label = segment.id[:8]
        try:
            names = await graph_memory.get_episode_entity_names(segment.id, group_id=group_id)
            if not names:
                print(f"[{label}] Graphiti has no entities for this episode — nothing to tag.")
                no_entities.append(segment.id)
                continue
            await _tag_chunks_with_entities(segment.id, names)
            print(f"[{label}] tagged chunks with: {names}")
            processed += 1
        except Exception as e:
            print(f"[{label}] FAILED: {type(e).__name__}: {e}")
            failed.append((segment.id, str(e)))

    after_total, after_populated = await _chunk_entity_tag_counts()

    print()
    print("── Backfill summary ──────────────────────────────────────────")
    print(f"Segments considered:      {len(candidates)}")
    print(f"Segments processed:       {processed}")
    print(f"Segments with no entities in Graphiti: {len(no_entities)}")
    print(f"Segments failed:          {len(failed)}")
    print(f"Chunks populated before:  {before_populated}/{before_total}")
    print(f"Chunks populated after:   {after_populated}/{after_total}")
    if failed:
        print()
        print("Failures:")
        for seg_id, err in failed:
            print(f"  {seg_id}: {err}")


if __name__ == "__main__":
    asyncio.run(main())
