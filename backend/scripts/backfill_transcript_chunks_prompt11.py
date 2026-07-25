"""
One-time backfill for Prompt 11's TranscriptChunk table.

The chunk-creation node (analysis_graph.create_transcript_chunks_node) only
ever runs for segments transcribed AFTER Prompt 11 landed — it needs
phrase/word timestamps that transcribe_node only produces when it actually
calls Whisper, not when it hits its "already transcribed" shortcut (see
transcribe_node's docstring). Every RawSegment ingested before this landed
has a transcript but no chunks. This script re-runs STT (with timestamps)
against each such segment's existing video and creates its chunks — it does
NOT touch `segment.transcript`/`embedding`/`topic_tags`/`status`, or
anything else on the avatar path.

Idempotent: a segment that already has TranscriptChunk rows is skipped, so
re-running this script is safe (e.g. after fixing a transient failure).
Calls create_transcript_chunks_node directly rather than re-implementing its
logic, so the backfilled chunks are produced by the exact same code path
Prompt 11 wired into the live pipeline (same contextual-embedding window,
same per-chunk topic tagging, same fail-soft behavior).

Usage: python scripts/backfill_transcript_chunks_prompt11.py   (run from
backend/, with a real .env in the repo root providing DATABASE_URL and
whatever storage/embedding/LLM credentials the segments' provider needs).
Uses whatever WHISPER_MODEL_INGESTION is currently configured — pass
--force to reprocess every segment (replacing existing chunks) instead of
skipping ones that already have them, e.g. after changing that setting.
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

from app.analysis_graph import create_transcript_chunks_node  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.models import InterviewSession, RawSegment, TranscriptChunk, User  # noqa: E402
from app.services.storage import storage_service  # noqa: E402
from app.services.stt import stt_service  # noqa: E402


async def _segments_needing_backfill(force: bool) -> list[tuple[RawSegment, str]]:
    """Every RawSegment with a video_key. By default, skips any segment that
    already has TranscriptChunk rows (safe to re-run after fixing a
    transient failure). `force=True` reprocesses every segment regardless —
    e.g. re-running after switching WHISPER_MODEL_INGESTION, where the goal
    is specifically to replace chunks made with the old model.
    create_transcript_chunks_node already deletes-then-recreates a segment's
    chunks, so forcing is safe either way. Returns (segment,
    recording_language) pairs."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(RawSegment)
            .options(joinedload(RawSegment.interview_session).joinedload(InterviewSession.user))
            .where(RawSegment.video_key.is_not(None))
        )
        segments = result.unique().scalars().all()

        already_chunked: set[str] = set()
        if not force:
            existing = await db.execute(select(TranscriptChunk.raw_segment_id).distinct())
            already_chunked = {row[0] for row in existing}

    pairs = []
    for seg in segments:
        if seg.id in already_chunked:
            continue
        user: User = seg.interview_session.user
        pairs.append((seg, user.recording_language or "en"))
    return pairs


async def main() -> None:
    force = "--force" in sys.argv
    print(f"Ingestion model: {stt_service.ingestion_model_name!r} (force={force})")
    candidates = await _segments_needing_backfill(force=force)
    print(f"Found {len(candidates)} segment(s) needing chunk backfill.")
    if not candidates:
        return

    processed = 0
    failed: list[tuple[str, str]] = []
    total_chunks = 0

    for segment, language in candidates:
        label = segment.id[:8]
        print(f"[{label}] transcribing (lang={language})…")
        try:
            video_bytes = await storage_service.download_file(segment.video_key)
            result = await stt_service.transcribe_with_timestamps(video_bytes, language=language)
            phrases = result["phrases"]
            print(f"[{label}] got {len(phrases)} phrase(s); creating chunks…")

            node_result = await create_transcript_chunks_node(
                {"segment_id": segment.id, "phrases": phrases}
            )
            chunk_ids = node_result.get("chunk_ids", [])
            total_chunks += len(chunk_ids)
            processed += 1
            print(f"[{label}] created {len(chunk_ids)} chunk(s).")
        except Exception as e:
            print(f"[{label}] FAILED: {type(e).__name__}: {e}")
            failed.append((segment.id, str(e)))

    print()
    print("── Backfill summary ──────────────────────────────────────────")
    print(f"Segments considered:  {len(candidates)}")
    print(f"Segments processed:   {processed}")
    print(f"Segments failed:      {len(failed)}")
    print(f"Total chunks created: {total_chunks}")
    if failed:
        print()
        print("Failures:")
        for seg_id, err in failed:
            print(f"  {seg_id}: {err}")


if __name__ == "__main__":
    asyncio.run(main())
