"""Bulk-import batch orchestrator (BULK_IMPORT_PLAN §5-§6).

Walks a validated batch's plan and pushes every file through the REAL
ingestion path — the same `create_segment_row` the /segments/ingest
endpoint uses, then the same `run_segment_analysis` graph — with a small
worker pool instead of fire-and-forget. AWAITING the analysis (rather
than create_task-ing it) is what bounds pipeline concurrency AND gives
per-file truth for the report.

Continue-and-report: one file failing marks itself `failed` with the
reason and the batch keeps going; the final state is `done` or
`done_with_failures`, never silently partial. Per-file retry deletes the
failed segment (the existing deletion service) and re-runs just that
file through the same path.
"""

from __future__ import annotations

import asyncio
import logging
import os

from app import interview_config
from app.database import AsyncSessionLocal
from app.models import BulkImportBatch, InterviewSession, RawSegment, User
from app.services.storage import storage_service

logger = logging.getLogger(__name__)

#: §5: bulk import is offline work — rate-limit civility beats speed.
WORKER_CONCURRENCY = 2

#: file_states is a whole-JSON column, so concurrent workers doing
#: read-modify-write would clobber each other's keys (measured: a failed
#: mark lost to a sibling's stale write). Every writer lives in this
#: process, so an asyncio lock is the honest serializer.
_FILE_STATE_LOCK = asyncio.Lock()


def _catalog_index(question_id: str, language: str) -> tuple:
    """(global catalog position, question text). The GLOBAL position is
    deliberately used as question_index: it is unique per question, so
    takes of the same question group together and the known
    cross-category question_index collision bug cannot be triggered by
    imports."""
    for i, q in enumerate(interview_config.get_questions(language)):
        if q["id"] == question_id:
            return i, q["text"]
    raise ValueError(f"question_id {question_id!r} not in catalog")


async def _set_file_state(batch_id: str, filename: str, **fields) -> None:
    async with _FILE_STATE_LOCK, AsyncSessionLocal() as db:
        batch = await db.get(BulkImportBatch, batch_id)
        states = dict(batch.file_states or {})
        states[filename] = {**(states.get(filename) or {}), **fields}
        batch.file_states = states
        await db.commit()


async def _ingest_one(batch: BulkImportBatch, producer: User,
                      session_id: str, entry: dict) -> None:
    from app.analysis_graph import run_segment_analysis
    from app.api.v1.bulk_import import staging_key
    from app.api.v1.interview import create_segment_row

    filename = entry["filename"]
    await _set_file_state(batch.id, filename, state="ingesting", error=None)
    # Staged bytes -> a real segment key (same prefix contract the presign
    # flow stamps), so everything downstream sees a normal recording.
    data = await storage_service.download_file(
        staging_key(producer.id, batch.id, filename)
    )
    qindex, qtext = _catalog_index(entry["question_id"], producer.recording_language or "he")
    safe = os.path.basename(filename)
    video_key = f"segments/{producer.id}/{session_id}/{qindex}/bulk_{batch.id[:8]}_{safe}"
    await storage_service.upload_file(data, video_key)
    async with AsyncSessionLocal() as db:
        session = await db.get(InterviewSession, session_id)
        segment = await create_segment_row(
            db,
            session,
            question_asked=qtext,
            question_index=qindex,
            question_id=entry["question_id"],
            video_url=storage_service.get_url(video_key),
            video_key=video_key,
            import_batch_id=batch.id,
        )
    await _set_file_state(batch.id, filename, segment_id=segment.id)
    await run_segment_analysis(segment.id)  # AWAITED: bounds concurrency
    async with AsyncSessionLocal() as db:
        seg = await db.get(RawSegment, segment.id)
        final = seg.status if seg else "failed"
    if final == "ready":
        await _set_file_state(batch.id, filename, state="ready")
    else:
        await _set_file_state(
            batch.id, filename, state="failed",
            error=f"analysis ended in status {final!r}",
        )


async def run_batch(batch_id: str) -> None:
    """The §5 worker pool over a validated batch's plan. Files are taken in
    plan order (CSV order = take order); the semaphore bounds how many are
    in flight; every failure is caught per file."""
    async with AsyncSessionLocal() as db:
        batch = await db.get(BulkImportBatch, batch_id)
        if batch is None or batch.state != "running":
            logger.warning(f"run_batch: batch {batch_id} not runnable")
            return
        producer = await db.get(User, batch.producer_id)
        plan = list(batch.mapping or [])
        # One interview session per batch, completed-marked so the /record
        # flow's single-active-session logic is never disturbed.
        session = InterviewSession(user_id=producer.id, status="completed")
        db.add(session)
        await db.commit()
        session_id = session.id

    sem = asyncio.Semaphore(WORKER_CONCURRENCY)

    async def worker(entry: dict) -> None:
        async with sem:
            try:
                await _ingest_one(batch, producer, session_id, entry)
            except Exception as e:
                logger.warning(f"bulk import {batch_id}: {entry['filename']} failed: {e}")
                await _set_file_state(
                    batch.id, entry["filename"], state="failed", error=str(e)[:500]
                )

    await asyncio.gather(*(worker(e) for e in plan))

    async with AsyncSessionLocal() as db:
        batch = await db.get(BulkImportBatch, batch_id)
        states = batch.file_states or {}
        failed = [f for f, st in states.items() if st.get("state") == "failed"]
        batch.state = "done_with_failures" if failed else "done"
        await db.commit()
    logger.info(f"bulk import {batch_id}: finished ({len(failed)} failure(s))")


async def retry_file(batch_id: str, filename: str) -> bool:
    """Re-run ONE failed file through the same path. Deletes the failed
    segment row first when one exists (the existing deletion service — the
    same implementation account reset uses), so retry never duplicates."""
    async with AsyncSessionLocal() as db:
        batch = await db.get(BulkImportBatch, batch_id)
        if batch is None:
            return False
        st = (batch.file_states or {}).get(filename) or {}
        if st.get("state") != "failed":
            return False
        entry = next((e for e in (batch.mapping or []) if e["filename"] == filename), None)
        if entry is None:
            return False
        producer = await db.get(User, batch.producer_id)
        seg_id = st.get("segment_id")
    if seg_id:
        from app.services import segment_deletion

        try:
            await segment_deletion.delete_segment_data(
                seg_id, producer.id, warm_cache=False
            )
        except Exception as e:
            logger.warning(f"retry cleanup of segment {seg_id} failed (continuing): {e}")
    async with AsyncSessionLocal() as db:
        session = InterviewSession(user_id=producer.id, status="completed")
        db.add(session)
        await db.commit()
        session_id = session.id
    try:
        await _ingest_one(batch, producer, session_id, entry)
    except Exception as e:
        await _set_file_state(batch_id, filename, state="failed", error=str(e)[:500])
    async with AsyncSessionLocal() as db:
        batch = await db.get(BulkImportBatch, batch_id)
        states = batch.file_states or {}
        failed = [f for f, s in states.items() if s.get("state") == "failed"]
        batch.state = "done_with_failures" if failed else "done"
        await db.commit()
    return True
