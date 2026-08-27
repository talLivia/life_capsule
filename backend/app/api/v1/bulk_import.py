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


def parse_mapping_pairs(rows) -> list:
    """CSV rows -> ordered (line, question_id, filename) pairs, one per
    referenced file (a semicolon cell or duplicated rows both yield one
    pair per file). Invalid entries are kept — status is judged at
    derivation time, so the table can SHOW a bad row rather than hide it."""
    pairs = []
    for i, row in enumerate(rows, start=2):  # header is line 1
        qid = (row.get("question_id") or "").strip()
        names = [f.strip() for f in (row.get("filenames") or "").split(";") if f.strip()]
        for name in names:
            pairs.append({"line": i, "question_id": qid, "filename": name})
    return pairs


def derive_rows(pairs, excluded, staged, catalog_ids, file_states=None):
    """THE single source of row truth (derived-rows redesign, 2026-08-28):
    every view of the batch — the table, the report, the start-time plan —
    is computed here from the stored pairs. Returns (rows, importable_idx).

    Status vocabulary, in judgment order:
      excluded | unknown_question | not_a_video | duplicate | no_file_yet |
      ready_to_import | pending | ingesting | done | failed
    """
    file_states = file_states or {}
    catalog = set(catalog_ids)
    staged_set = set(staged)
    excluded_set = set(excluded or [])
    rows, importable, seen = [], [], {}
    for idx, p in enumerate(pairs):
        qid, name = p["question_id"], p["filename"]
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        st, err = "ready_to_import", None
        if idx in excluded_set:
            st = "excluded"
        elif qid not in catalog:
            st, err = "unknown_question", f"question id {qid!r} is not in the catalog"
        elif ext not in VIDEO_EXTENSIONS:
            st, err = "not_a_video", f"{name} is not a video file"
        elif name in seen:
            st, err = "duplicate", f"{name} already referenced on line {seen[name]}"
        elif name not in staged_set:
            st = "no_file_yet"
        if st == "ready_to_import":
            seen[name] = p["line"]
            fs = file_states.get(name)
            if fs:  # the batch ran (or is running): live pipeline status
                st = {"pending": "pending", "ingesting": "ingesting",
                      "ready": "done", "failed": "failed"}.get(fs.get("state"), fs.get("state"))
                err = fs.get("error")
            else:
                importable.append(idx)
        rows.append({"index": idx, "line": p["line"], "question_id": qid,
                     "filename": name, "status": st, "error": err})
    return rows, importable


def validate_mapping(rows, staged_filenames, catalog_ids):
    """Back-compat adapter over derive_rows: (errors, warnings, plan) in the
    original report shapes. Kept because the §3 contract (all-or-nothing
    reporting, plan in CSV order) is now a VIEW of the derivation."""
    pairs = parse_mapping_pairs(rows)
    derived, importable = derive_rows(pairs, [], staged_filenames, catalog_ids)
    errors, warnings, plan = [], [], []
    kind = {"unknown_question": "unknown_question_id", "not_a_video": "not_a_video",
            "duplicate": "duplicate_filename_reference"}
    for r in derived:
        if r["status"] in kind:
            errors.append({"line": r["line"], "error": kind[r["status"]],
                           "filename": r["filename"], "question_id": r["question_id"]})
        elif r["status"] == "no_file_yet":
            warnings.append({"line": r["line"], "warning": "file_not_uploaded_skipped",
                             "filename": r["filename"]})
        elif r["status"] == "ready_to_import":
            plan.append({"question_id": r["question_id"], "filename": r["filename"]})
    referenced = {r["filename"] for r in derived}
    for name in sorted(set(staged_filenames) - referenced):
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


def _batch_view(batch: BulkImportBatch, language: str = "he") -> dict:
    view = {
        "id": batch.id,
        "state": batch.state,
        "files": batch.files or {},
        "mapping": batch.mapping,
        "report": batch.report,
        "file_states": batch.file_states or {},
        "rows": [],
        "unmapped_files": [],
    }
    if batch.mapping_rows:
        questions = {q["id"]: q["text"] for q in interview_config.get_questions(language)}
        staged = [n for n, m in (batch.files or {}).items() if m.get("staged")]
        rows, _ = derive_rows(
            batch.mapping_rows, batch.excluded or [], staged,
            questions.keys(), batch.file_states or {},
        )
        for r in rows:
            r["question_text"] = questions.get(r["question_id"], "")
        view["rows"] = rows
        referenced = {r["filename"] for r in rows}
        view["unmapped_files"] = sorted(set(staged) - referenced)
    return view


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
    lang = user.recording_language or "he"
    catalog_ids = [q["id"] for q in interview_config.get_questions(lang)]
    staged = [name for name, meta in (batch.files or {}).items() if meta.get("staged")]
    # Store the RAW pairs (derived-rows redesign): statuses are computed per
    # read from here on, and the runner plan is compiled at start time.
    batch.mapping_rows = parse_mapping_pairs(rows)
    batch.excluded = []
    errors, warnings, plan = validate_mapping(rows, staged, catalog_ids)
    batch.report = {"errors": errors, "warnings": warnings}
    batch.mapping = plan
    batch.state = "validated" if plan else "staging"
    await db.commit()
    return _batch_view(batch, lang)


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
    lang = user.recording_language or "he"
    if batch.mapping_rows:
        # Compile the effective plan NOW: importable rows minus exclusions
        # minus missing files — the table's current truth becomes the plan.
        questions = [q["id"] for q in interview_config.get_questions(lang)]
        staged = [n for n, m in (batch.files or {}).items() if m.get("staged")]
        rows, importable = derive_rows(
            batch.mapping_rows, batch.excluded or [], staged, questions
        )
        batch.mapping = [
            {"question_id": rows[i]["question_id"], "filename": rows[i]["filename"]}
            for i in importable
        ]
    if not batch.mapping:
        raise HTTPException(status_code=409, detail="Nothing importable in this batch")
    batch.state = "running"
    states = {e["filename"]: {"state": "pending"} for e in batch.mapping}
    batch.file_states = states
    await db.commit()
    asyncio.create_task(bulk_import_runner.run_batch(batch.id))
    return _batch_view(batch, lang)


@router.patch("/batches/{batch_id}/rows/{row_index}")
async def set_row_exclusion(
    batch_id: str,
    row_index: int,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """Exclude/restore ONE row from THIS batch (never touches the CSV).
    Body: {"excluded": bool}."""
    batch = await _owned_batch(batch_id, user, db)
    if batch.state == "running":
        raise HTTPException(status_code=409, detail="Batch is running")
    n = len(batch.mapping_rows or [])
    if not (0 <= row_index < n):
        raise HTTPException(status_code=404, detail="No such row")
    excluded = set(batch.excluded or [])
    if payload.get("excluded"):
        excluded.add(row_index)
    else:
        excluded.discard(row_index)
    batch.excluded = sorted(excluded)
    await db.commit()
    return _batch_view(batch, user.recording_language or "he")


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
