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


# ── §3 validator ────────────────────────────────────────────────────────────


def _rows(*pairs):
    return [{"question_id": q, "filenames": f} for q, f in pairs]


def test_validator_happy_path_preserves_csv_order():
    errors, warnings, plan = bi.validate_mapping(
        _rows(("childhood_q01", "a.mp4;b.mp4"), ("army_q01", "c.mp4")),
        ["a.mp4", "b.mp4", "c.mp4"],
        ["childhood_q01", "army_q01"],
    )
    assert errors == [] and warnings == []
    assert [p["filename"] for p in plan] == ["a.mp4", "b.mp4", "c.mp4"]


def test_validator_flags_every_error_kind():
    errors, warnings, plan = bi.validate_mapping(
        _rows(
            ("bogus_q99", "a.mp4"),          # unknown id
            ("childhood_q01", "missing.mp4"),  # not uploaded
            ("army_q01", "a.mp4"),           # ok
            ("army_q02", "a.mp4"),           # duplicate reference
            ("army_q03", "notes.txt"),       # not a video
        ),
        ["a.mp4", "orphan.mp4"],
        ["childhood_q01", "army_q01", "army_q02", "army_q03"],
    )
    kinds = sorted(e["error"] for e in errors)
    assert kinds == [
        "duplicate_filename_reference", "file_not_uploaded",
        "not_a_video", "unknown_question_id",
    ]
    assert [w["filename"] for w in warnings] == ["orphan.mp4"]
    assert [p["filename"] for p in plan] == ["a.mp4"]


def test_validator_empty_batch_is_an_error():
    errors, _, plan = bi.validate_mapping(_rows(("childhood_q01", "")), ["x.mp4"], ["childhood_q01"])
    assert plan == [] and errors[0]["error"] == "empty_batch"


# ── batch lifecycle flow ────────────────────────────────────────────────────


async def _make_batch(client, auth_headers):
    r = await client.post("/api/v1/bulk-import/batches", headers=auth_headers)
    assert r.status_code == 201
    return r.json()["id"]


@pytest.mark.asyncio
async def test_batch_flow_stage_validate_and_resume(client, auth_headers, tmp_path, monkeypatch):
    from app.services import storage as storage_mod

    stored = {}

    async def fake_upload(key, data, **kw):
        stored[key] = data
        return f"/uploads/{key}"

    monkeypatch.setattr(storage_mod.storage_service, "upload_file", fake_upload)

    bid = await _make_batch(client, auth_headers)
    r = await client.put(
        f"/api/v1/bulk-import/batches/{bid}/files/a.mp4",
        headers=auth_headers,
        files={"file": ("a.mp4", b"vid-bytes", "video/mp4")},
    )
    assert r.status_code == 200 and r.json()["files"]["a.mp4"]["staged"]
    assert any(k.startswith("bulk_staging/") and k.endswith(f"{bid}/a.mp4") for k in stored)

    csv_ok = "question_id,category,question_text,filenames\r\nchildhood_q01,c,t,a.mp4\r\n"
    r = await client.post(
        f"/api/v1/bulk-import/batches/{bid}/mapping",
        headers=auth_headers,
        files={"file": ("map.csv", csv_ok.encode("utf-8"), "text/csv")},
    )
    body = r.json()
    assert body["state"] == "validated" and body["report"]["errors"] == []
    assert body["mapping"] == [{"question_id": "childhood_q01", "filename": "a.mp4"}]

    # resume: the batch is findable and intact from a fresh "session"
    r = await client.get("/api/v1/bulk-import/batches", headers=auth_headers)
    assert any(b["id"] == bid and b["state"] == "validated" for b in r.json())

    # a bad mapping flips it back to staging with a real report
    csv_bad = "question_id,category,question_text,filenames\r\nnope_q1,c,t,a.mp4\r\n"
    r = await client.post(
        f"/api/v1/bulk-import/batches/{bid}/mapping",
        headers=auth_headers,
        files={"file": ("map.csv", csv_bad.encode("utf-8"), "text/csv")},
    )
    body = r.json()
    assert body["state"] == "staging"
    assert body["report"]["errors"][0]["error"] == "unknown_question_id"


@pytest.mark.asyncio
async def test_batch_is_owner_scoped(client, auth_headers):
    bid = await _make_batch(client, auth_headers)
    r = await client.get(f"/api/v1/bulk-import/batches/{bid}")
    assert r.status_code in (401, 403)
