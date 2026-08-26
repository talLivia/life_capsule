"""Bulk import (BULK_IMPORT_PLAN): template generation now; validation and
orchestrator tests join as those milestones land."""

import csv
import io

import pytest

from app import interview_config
from app.api.v1 import bulk_import as bi


def test_template_has_one_row_per_catalog_question():
    body = bi.build_template_csv("he")
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0] == bi.TEMPLATE_COLUMNS
    catalog = interview_config.get_questions("he")
    assert len(rows) - 1 == len(catalog) == 129
    ids = [r[0] for r in rows[1:]]
    assert ids == [q["id"] for q in catalog]  # catalog order preserved
    assert all(r[3] == "" for r in rows[1:])  # filenames start empty
    assert all(r[2] for r in rows[1:])  # every row carries the question text


@pytest.mark.asyncio
async def test_template_download_requires_producer(client):
    r = await client.get("/api/v1/bulk-import/template.csv")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_template_download_is_bom_prefixed_csv(client, auth_headers):
    r = await client.get("/api/v1/bulk-import/template.csv", headers=auth_headers)
    assert r.status_code == 200
    assert r.content.startswith("﻿".encode("utf-8"))  # Excel BOM
    assert "text/csv" in r.headers["content-type"]
    text = r.content.decode("utf-8-sig")
    first = next(csv.reader(io.StringIO(text)))
    assert first == bi.TEMPLATE_COLUMNS
