"""Deletion must be COMPLETE — a partial delete leaves data the user believes
is gone, which is the failure mode this module exists to prevent."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InterviewSession, RawSegment
from app.services import graph_memory, segment_deletion

pytestmark = pytest.mark.asyncio


@pytest.fixture
def sd_session_factory(test_engine, monkeypatch):
    """Point the module's own session factory at the test engine — it opens
    its own sessions rather than receiving one."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(segment_deletion, "AsyncSessionLocal", factory)
    return factory


@pytest.fixture
def patched(monkeypatch):
    """Stub the external stores; the DB is exercised for real via db_session."""
    calls = {"episodes": [], "files": []}

    async def fake_remove(segment_id, group_id="x"):
        calls["episodes"].append(segment_id)
        return 1

    async def fake_delete_file(key):
        calls["files"].append(key)

    monkeypatch.setattr(graph_memory, "remove_episodes_for_segment", fake_remove)
    monkeypatch.setattr(segment_deletion.graph_memory, "remove_episodes_for_segment", fake_remove)
    monkeypatch.setattr(segment_deletion.storage_service, "delete_file", fake_delete_file)
    monkeypatch.setattr(segment_deletion, "_refresh_caches", AsyncMock())
    return calls


async def _seed(db_session, test_user, seg_id="seg-del", key="segments/x/a.webm"):
    session = InterviewSession(id=f"int-{seg_id}", user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    seg = RawSegment(
        id=seg_id, interview_session_id=session.id, question_asked="Q",
        question_index=0, status="ready", video_key=key,
    )
    db_session.add(seg)
    await db_session.commit()
    return seg


async def test_delete_segment_removes_graph_file_and_row(
    db_session, test_user, patched, sd_session_factory
):
    await _seed(db_session, test_user)

    result = await segment_deletion.delete_segment_data("seg-del", test_user.id)

    assert result.segments_deleted == 1
    assert result.episodes_removed == 1
    assert result.files_deleted == 1
    assert patched["episodes"] == ["seg-del"], "graph episode must be removed"
    assert patched["files"] == ["segments/x/a.webm"], "stored video must be deleted"
    assert result.ok


async def test_graph_failure_is_reported_not_swallowed(
    db_session, test_user, monkeypatch, sd_session_factory
):
    """A graph cleanup failure must surface — silently continuing is how
    orphans are created."""
    await _seed(db_session, test_user, seg_id="seg-fail", key=None)

    async def boom(segment_id, group_id="x"):
        raise RuntimeError("neo4j unreachable")

    monkeypatch.setattr(segment_deletion.graph_memory, "remove_episodes_for_segment", boom)
    monkeypatch.setattr(segment_deletion, "_refresh_caches", AsyncMock())

    result = await segment_deletion.delete_segment_data("seg-fail", test_user.id)

    assert not result.ok
    assert any("graph cleanup failed" in f for f in result.failures)
    # The row is still removed — leaving it would be its own inconsistency.
    assert result.segments_deleted == 1


async def test_missing_stored_file_does_not_fail_the_delete(
    db_session, test_user, monkeypatch, sd_session_factory
):
    await _seed(db_session, test_user, seg_id="seg-nofile")

    async def missing(key):
        raise FileNotFoundError(key)

    monkeypatch.setattr(
        segment_deletion.graph_memory, "remove_episodes_for_segment", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(segment_deletion.storage_service, "delete_file", missing)
    monkeypatch.setattr(segment_deletion, "_refresh_caches", AsyncMock())

    result = await segment_deletion.delete_segment_data("seg-nofile", test_user.id)
    assert result.segments_deleted == 1, "a missing file is fine — it's gone either way"


async def test_reset_deletes_every_recording_for_the_producer(
    db_session, test_user, patched, sd_session_factory
):
    for i in range(3):
        await _seed(db_session, test_user, seg_id=f"seg-r{i}", key=f"segments/x/{i}.webm")

    result = await segment_deletion.delete_all_producer_recordings(test_user.id)

    assert result.segments_deleted == 3
    assert sorted(patched["episodes"]) == ["seg-r0", "seg-r1", "seg-r2"]
    assert len(patched["files"]) == 3


async def test_delete_unknown_segment_is_a_noop(patched, sd_session_factory):
    result = await segment_deletion.delete_segment_data("does-not-exist", "group")
    assert result.segments_deleted == 0
