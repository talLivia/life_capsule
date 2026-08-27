"""Bulk import of legacy videos (docs/BULK_IMPORT_PLAN.md).

A mapper + dispatcher over the EXISTING ingestion path — never a second
pipeline. This module grows in plan order: the CSV mapping template
(§2) first; validation (§3), the batch orchestrator (§5/§6) and status
endpoints follow.
"""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app import interview_config
from app.api.v1.interview import require_producer
from app.models import User

router = APIRouter()

#: Column order is the contract the §3 validator parses back.
TEMPLATE_COLUMNS = ["question_id", "category", "question_text", "filenames", "notes"]

#: First-row note (the validator ignores extra columns): both multi-take
#: formats are supported, and the duplicate-row pattern is the natural
#: Excel workflow (live finding, 2026-08-28).
_TEMPLATE_NOTE = (
    "Several takes for one question: either separate filenames with ; "
    "in one cell, or duplicate the row (same question_id, one filename "
    "per row). Rows with an empty filenames cell are skipped."
)


def build_template_csv(language: str) -> str:
    """One row per catalog question, filenames empty — generated live from
    interview_questions.json at download time so ids can never go stale.
    `category` is the human label (for the producer reading the sheet);
    the id column alone is authoritative for the validator."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")  # Excel-friendly
    writer.writerow(TEMPLATE_COLUMNS)
    for i, q in enumerate(interview_config.get_questions(language)):
        writer.writerow([
            q["id"], q.get("category_label") or q["category"], q["text"], "",
            _TEMPLATE_NOTE if i == 0 else "",
        ])
    return out.getvalue()


@router.get("/template.csv")
async def download_mapping_template(user: User = Depends(require_producer)):
    """The §2 mapping template, in the producer's own recording language.
    UTF-8 BOM so Excel opens Hebrew correctly on double-click."""
    body = "﻿" + build_template_csv(user.recording_language or "he")
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="bulk_import_template.csv"'},
    )


# ── §3: mapping validation + batch lifecycle ────────────────────────────────

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}


def validate_mapping(rows, staged_filenames, catalog_ids):
    """Pure §3 validator — no side effects, whole-batch report.

    `rows` = parsed CSV rows (dicts with question_id/filenames); returns
    (errors, warnings, plan) where plan is the ordered
    [{"question_id", "filename"}] ingestion list (CSV row order = take
    order = archive order). ANY error blocks ingestion; warnings don't.
    """
    errors, warnings, plan, seen = [], [], [], {}
    catalog = set(catalog_ids)
    staged = set(staged_filenames)
    for i, row in enumerate(rows, start=2):  # header is line 1
        qid = (row.get("question_id") or "").strip()
        names = [f.strip() for f in (row.get("filenames") or "").split(";") if f.strip()]
        if not names:
            continue
        if qid not in catalog:
            errors.append({"line": i, "error": "unknown_question_id", "question_id": qid})
            continue
        for name in names:
            ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
            if ext not in VIDEO_EXTENSIONS:
                errors.append({"line": i, "error": "not_a_video", "filename": name})
                continue
            if name in seen:
                errors.append(
                    {"line": i, "error": "duplicate_filename_reference",
                     "filename": name, "first_line": seen[name]}
                )
                continue
            seen[name] = i
            if name not in staged:
                errors.append({"line": i, "error": "file_not_uploaded", "filename": name})
                continue
            plan.append({"question_id": qid, "filename": name})
    for name in sorted(staged - set(seen)):
        warnings.append({"warning": "unmapped_file", "filename": name})
    if not plan and not errors:
        errors.append({"line": None, "error": "empty_batch"})
    return errors, warnings, plan


def staging_key(producer_id: str, batch_id: str, filename: str) -> str:
    return f"bulk_staging/{producer_id}/{batch_id}/{filename}"


# ── batch lifecycle endpoints ───────────────────────────────────────────────

import io as _io
import os

from fastapi import File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import BulkImportBatch
from app.services.storage import storage_service


async def _owned_batch(batch_id: str, user: User, db: AsyncSession) -> BulkImportBatch:
    batch = await db.get(BulkImportBatch, batch_id)
    if batch is None or batch.producer_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such batch")
    return batch


def _batch_view(batch: BulkImportBatch) -> dict:
    return {
        "id": batch.id,
        "state": batch.state,
        "files": batch.files or {},
        "mapping": batch.mapping,
        "report": batch.report,
        "file_states": batch.file_states or {},
    }


@router.post("/batches", status_code=status.HTTP_201_CREATED)
async def create_batch(
    db: AsyncSession = Depends(get_db), user: User = Depends(require_producer)
):
    batch = BulkImportBatch(producer_id=user.id, files={}, file_states={})
    db.add(batch)
    await db.commit()
    return _batch_view(batch)


@router.get("/batches")
async def list_batches(
    db: AsyncSession = Depends(get_db), user: User = Depends(require_producer)
):
    """Resume support: reopening Settings finds the draft/running batches."""
    from sqlalchemy import select

    rows = (
        (await db.execute(
            select(BulkImportBatch)
            .where(BulkImportBatch.producer_id == user.id)
            .order_by(BulkImportBatch.created_at.desc())
        )).scalars().all()
    )
    return [_batch_view(b) for b in rows]


@router.get("/batches/{batch_id}")
async def get_batch(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    return _batch_view(await _owned_batch(batch_id, user, db))


@router.put("/batches/{batch_id}/files/{filename}")
async def stage_file(
    batch_id: str,
    filename: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """Stage one video into the batch prefix. Mirrors the local-mode half of
    the presign flow: files land under bulk_staging/{producer}/{batch}/ and
    survive closed tabs; ingestion later reads them from there. Re-uploading
    the same filename overwrites (resume-by-reselect)."""
    batch = await _owned_batch(batch_id, user, db)
    if batch.state not in ("staging", "validated"):
        raise HTTPException(status_code=409, detail=f"Batch is {batch.state}")
    safe = os.path.basename(filename)
    data = await file.read()
    await storage_service.upload_file(data, staging_key(user.id, batch.id, safe))
    files = dict(batch.files or {})
    files[safe] = {"size": len(data), "staged": True}
    batch.files = files
    batch.state = "staging"  # any new file voids a previous validation
    batch.report = None
    await db.commit()
    return _batch_view(batch)


@router.post("/batches/{batch_id}/mapping")
async def upload_mapping(
    batch_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """§3: parse the filled template, validate the WHOLE batch, store the
    report. No side effects beyond the stored report — nothing ingests until
    the producer explicitly starts a `validated` batch."""
    import csv as _csv

    batch = await _owned_batch(batch_id, user, db)
    if batch.state == "running":
        raise HTTPException(status_code=409, detail="Batch already running")
    text = (await file.read()).decode("utf-8-sig")
    rows = list(_csv.DictReader(_io.StringIO(text)))
    catalog_ids = [q["id"] for q in interview_config.get_questions(user.recording_language or "he")]
    staged = [name for name, meta in (batch.files or {}).items() if meta.get("staged")]
    errors, warnings, plan = validate_mapping(rows, staged, catalog_ids)
    batch.report = {"errors": errors, "warnings": warnings}
    batch.mapping = plan
    batch.state = "validated" if not errors else "staging"
    await db.commit()
    return _batch_view(batch)


@router.post("/batches/{batch_id}/start")
async def start_batch(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """§5: flip a VALIDATED batch to running and hand it to the server-side
    orchestrator. The browser can go away — progress lives on the batch row."""
    import asyncio

    from app.services import bulk_import_runner

    batch = await _owned_batch(batch_id, user, db)
    if batch.state != "validated":
        raise HTTPException(status_code=409, detail=f"Batch is {batch.state}, not validated")
    batch.state = "running"
    states = {e["filename"]: {"state": "pending"} for e in (batch.mapping or [])}
    batch.file_states = states
    await db.commit()
    asyncio.create_task(bulk_import_runner.run_batch(batch.id))
    return _batch_view(batch)


@router.post("/batches/{batch_id}/files/{filename}/retry")
async def retry_failed_file(
    batch_id: str,
    filename: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """§6: re-run ONE failed file through the same path (delete-and-reingest,
    never duplicate)."""
    from app.services import bulk_import_runner

    await _owned_batch(batch_id, user, db)  # ownership check
    ok = await bulk_import_runner.retry_file(batch_id, os.path.basename(filename))
    if not ok:
        raise HTTPException(status_code=409, detail="File is not in a failed state")
    batch = await _owned_batch(batch_id, user, db)
    await db.refresh(batch)
    return _batch_view(batch)
