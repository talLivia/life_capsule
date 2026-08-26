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
TEMPLATE_COLUMNS = ["question_id", "category", "question_text", "filenames"]


def build_template_csv(language: str) -> str:
    """One row per catalog question, filenames empty — generated live from
    interview_questions.json at download time so ids can never go stale.
    `category` is the human label (for the producer reading the sheet);
    the id column alone is authoritative for the validator."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")  # Excel-friendly
    writer.writerow(TEMPLATE_COLUMNS)
    for q in interview_config.get_questions(language):
        writer.writerow([q["id"], q.get("category_label") or q["category"], q["text"], ""])
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
