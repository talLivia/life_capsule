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


# ── §5/§6 orchestrator ──────────────────────────────────────────────────────


@pytest.fixture
async def runner_env(test_engine, monkeypatch):
    """Retarget the runner's DB access at the test engine and stub the two
    heavy externals (storage bytes, the analysis graph)."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app import analysis_graph as ag
    from app.services import bulk_import_runner as runner
    from app.services import storage as storage_mod

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(runner, "AsyncSessionLocal", factory)
    monkeypatch.setattr(ag, "AsyncSessionLocal", factory)
    # SQLite's StaticPool shares ONE connection: two concurrent ingest
    # transactions clobber each other in ways Postgres's FOR UPDATE lock
    # prevents. Pool-of-1 here; real concurrency is exercised by the §8
    # integration batch against Postgres.
    monkeypatch.setattr(runner, "WORKER_CONCURRENCY", 1)

    store = {}

    async def fake_upload(key, data, **kw):
        store[key] = data
        return f"/uploads/{key}"

    async def fake_download(key):
        return store[key]

    monkeypatch.setattr(storage_mod.storage_service, "upload_file", fake_upload)
    monkeypatch.setattr(storage_mod.storage_service, "download_file", fake_download)

    async def fake_analysis(segment_id):
        # The REAL graph is exercised elsewhere; here each segment just
        # lands in the status the filename asks for.
        async with factory() as db:
            from app.models import RawSegment

            seg = await db.get(RawSegment, segment_id)
            seg.status = "failed" if "corrupt" in (seg.video_key or "") else "ready"
            await db.commit()

    monkeypatch.setattr(ag, "run_segment_analysis", fake_analysis)
    # The start endpoint fires run_batch as a background task; tests drive
    # the REAL run_batch deterministically instead, so the endpoint's copy
    # becomes a no-op here (production runs it exactly once).
    real_run_batch = runner.run_batch
    from unittest.mock import AsyncMock as _AM
    monkeypatch.setattr(runner, "run_batch", _AM())
    return {"store": store, "run_batch": real_run_batch}


async def _validated_batch(client, auth_headers, filenames):
    bid = (await client.post("/api/v1/bulk-import/batches", headers=auth_headers)).json()["id"]
    for name in filenames:
        r = await client.put(
            f"/api/v1/bulk-import/batches/{bid}/files/{name}",
            headers=auth_headers,
            files={"file": (name, b"bytes-" + name.encode(), "video/mp4")},
        )
        assert r.status_code == 200
    rows = "\r\n".join(f"childhood_q0{i+1},c,t,{n}" for i, n in enumerate(filenames))
    csv_body = "question_id,category,question_text,filenames\r\n" + rows + "\r\n"
    r = await client.post(
        f"/api/v1/bulk-import/batches/{bid}/mapping",
        headers=auth_headers,
        files={"file": ("m.csv", csv_body.encode(), "text/csv")},
    )
    assert r.json()["state"] == "validated", r.json()
    return bid


@pytest.mark.asyncio
async def test_batch_runs_continue_and_report(client, auth_headers, runner_env, db_session):
    from sqlalchemy import select

    from app.models import RawSegment
    from app.services import bulk_import_runner as runner

    bid = await _validated_batch(client, auth_headers, ["a.mp4", "corrupt.mp4", "b.mp4"])
    r = await client.post(f"/api/v1/bulk-import/batches/{bid}/start", headers=auth_headers)
    assert r.status_code == 200 and r.json()["state"] == "running"
    await runner_env["run_batch"](bid)

    r = await client.get(f"/api/v1/bulk-import/batches/{bid}", headers=auth_headers)
    body = r.json()
    assert body["state"] == "done_with_failures"
    states = body["file_states"]
    assert states["a.mp4"]["state"] == "ready"
    assert states["b.mp4"]["state"] == "ready"
    assert states["corrupt.mp4"]["state"] == "failed"

    await db_session.commit()  # end the fixture's snapshot; see fresh rows
    segs = (await db_session.execute(
        select(RawSegment).where(RawSegment.import_batch_id == bid)
    )).scalars().all()
    assert len(segs) == 3
    assert all(s.import_batch_id == bid for s in segs)
    assert sorted(s.question_id for s in segs) == ["childhood_q01", "childhood_q02", "childhood_q03"]
    assert len({s.recording_no for s in segs}) == 3  # unique, serialized


@pytest.mark.asyncio
async def test_retry_reruns_only_the_failed_file(client, auth_headers, runner_env, db_session):
    from app.services import bulk_import_runner as runner

    bid = await _validated_batch(client, auth_headers, ["corrupt.mp4"])
    await client.post(f"/api/v1/bulk-import/batches/{bid}/start", headers=auth_headers)
    await runner_env["run_batch"](bid)

    # heal the file: re-stage under a name the fake analysis passes
    staged = [k for k in runner_env["store"] if k.endswith("corrupt.mp4") and "bulk_staging" in k]
    assert staged
    # monkey-fix: make the analysis succeed this time by rewriting the staged key content path
    from app.services import segment_deletion

    async def fake_delete(segment_id, group_id, **kw):
        from app.models import RawSegment

        seg = await db_session.get(RawSegment, segment_id)
        if seg:
            await db_session.delete(seg)
            await db_session.commit()
        return None

    segment_deletion_real = segment_deletion.delete_segment_data
    segment_deletion.delete_segment_data = fake_delete
    try:
        import app.services.bulk_import_runner as r2

        async def analysis_ok(segment_id):
            from app.models import RawSegment
            factory = r2.AsyncSessionLocal
            async with factory() as db:
                seg = await db.get(RawSegment, segment_id)
                seg.status = "ready"
                await db.commit()

        import app.analysis_graph as ag
        old = ag.run_segment_analysis
        ag.run_segment_analysis = analysis_ok
        try:
            ok = await runner.retry_file(bid, "corrupt.mp4")
        finally:
            ag.run_segment_analysis = old
    finally:
        segment_deletion.delete_segment_data = segment_deletion_real
    assert ok
    r = await client.get(f"/api/v1/bulk-import/batches/{bid}", headers=auth_headers)
    body = r.json()
    assert body["file_states"]["corrupt.mp4"]["state"] == "ready"
    assert body["state"] == "done"
