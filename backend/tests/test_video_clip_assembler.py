"""
Tests for video_clip_assembler.py — the shared clip-assembly layer (ffmpeg
trim/concat/upload, the cache key, the Phase 8 category lookup). ffmpeg is
mocked via subprocess.run the same way test_animator_simple.py does for the
avatar path's own ffmpeg call; real DB rows (in-memory SQLite) back the
category-lookup tests. The v1 retrieval-orchestration tests that used to
live here were removed with the mode (docs/V1_REMOVAL_PLAN.md).
"""

import subprocess
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InterviewSession, RawSegment
from app.services import video_clip_assembler as vca

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def vca_session_factory(test_engine, monkeypatch):
    """Autouse since photo_categories_for_segments: the success-path
    orchestration now opens the module-level AsyncSessionLocal itself, so a
    test that mocks every collaborator would still hit the REAL configured
    database without this. No test in this file should ever touch it."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(vca, "AsyncSessionLocal", factory)
    return factory


# ── ffmpeg trim / concat ─────────────────────────────────────────────────────


async def test_trim_clip_uses_synchronous_subprocess(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, capture_output=True, timeout=None):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake video")
    output = tmp_path / "out.mp4"

    await vca._trim_clip(source, 5.0, 9.0, output)

    cmd = captured["cmd"]
    assert cmd[0] == "ffmpeg"
    assert "-ss" in cmd and "5.000" in cmd
    assert "-to" in cmd and "9.000" in cmd
    assert str(source) in cmd
    assert str(output) in cmd


async def test_trim_clip_raises_on_ffmpeg_failure(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output=True, timeout=None):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"ffmpeg exploded")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="ffmpeg trim failed"):
        await vca._trim_clip(tmp_path / "in.mp4", 0.0, 1.0, tmp_path / "out.mp4")


async def test_run_ffmpeg_passes_timeout_to_subprocess(monkeypatch):
    """subprocess.run has no timeout by default — a stuck ffmpeg process
    would otherwise hang the whole WS turn forever with no exception."""
    captured = {}

    def fake_run(cmd, capture_output=True, timeout=None):
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    vca._run_ffmpeg(["ffmpeg", "-y"])
    assert captured["timeout"] == vca.FFMPEG_TIMEOUT_SECONDS


async def test_run_ffmpeg_converts_timeout_expired_to_runtime_error(monkeypatch):
    def fake_run(cmd, capture_output=True, timeout=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="ffmpeg timed out"):
        vca._run_ffmpeg(["ffmpeg", "-y"])


async def test_trim_clip_raises_runtime_error_when_ffmpeg_hangs(monkeypatch, tmp_path):
    """End-to-end through _trim_clip (not just _run_ffmpeg in isolation): a
    hung ffmpeg process must surface as a catchable RuntimeError, not block
    the awaiting WS turn forever."""

    def fake_run(cmd, capture_output=True, timeout=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="ffmpeg timed out"):
        await vca._trim_clip(tmp_path / "in.mp4", 0.0, 1.0, tmp_path / "out.mp4")


async def test_concat_clips_uses_stream_copy_concat_demuxer(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, capture_output=True, timeout=None):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    clip_a.write_bytes(b"a")
    clip_b.write_bytes(b"b")
    output = tmp_path / "final.mp4"

    await vca._concat_clips([clip_a, clip_b], output)

    cmd = captured["cmd"]
    assert cmd[0] == "ffmpeg"
    assert "-f" in cmd and "concat" in cmd
    assert "-c" in cmd and "copy" in cmd
    # The temporary list file must not survive past the call.
    assert not output.with_suffix(".txt").exists()


async def test_concat_clips_raises_and_cleans_up_list_file_on_failure(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output=True, timeout=None):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"concat failed")

    monkeypatch.setattr(subprocess, "run", fake_run)

    clip_a = tmp_path / "a.mp4"
    clip_a.write_bytes(b"a")
    output = tmp_path / "final.mp4"

    with pytest.raises(RuntimeError, match="ffmpeg concat failed"):
        await vca._concat_clips([clip_a], output)
    assert not output.with_suffix(".txt").exists()


# ── _assemble_and_upload_clip (non-contiguous, multi-recording) ────────────


async def test_assemble_and_upload_clip_multi_recording_non_contiguous(monkeypatch):
    """Two clips from two DIFFERENT raw_segment_ids (non-contiguous, across
    recordings) get individually trimmed and concatenated into one output."""
    clip_a = vca.ExpandedClip(raw_segment_id="seg-a", start_sec=1.0, end_sec=3.0, source_chunk_id="c-a")
    clip_b = vca.ExpandedClip(raw_segment_id="seg-b", start_sec=10.0, end_sec=12.0, source_chunk_id="c-b")

    segments = {
        "seg-a": RawSegment(id="seg-a", video_key="videos/seg-a.mp4", interview_session_id="x", question_asked="q", question_index=0),
        "seg-b": RawSegment(id="seg-b", video_key="videos/seg-b.mp4", interview_session_id="x", question_asked="q", question_index=1),
    }
    monkeypatch.setattr(vca, "_fetch_segment_videos", AsyncMock(return_value=segments))
    monkeypatch.setattr(
        vca.storage_service, "download_file", AsyncMock(return_value=b"fake source bytes")
    )
    monkeypatch.setattr(vca.storage_service, "upload_file", AsyncMock(return_value="key"))
    monkeypatch.setattr(
        vca.storage_service, "serving_url", AsyncMock(return_value="https://cdn.example/final.mp4")
    )

    trim_calls = []

    async def fake_trim(source_path, start_sec, end_sec, output_path):
        trim_calls.append((str(source_path), start_sec, end_sec))
        output_path.write_bytes(b"trimmed")

    concat_calls = []

    async def fake_concat(clip_paths, output_path):
        concat_calls.append([str(p) for p in clip_paths])
        output_path.write_bytes(b"concatenated")

    monkeypatch.setattr(vca, "_trim_clip", fake_trim)
    monkeypatch.setattr(vca, "_concat_clips", fake_concat)

    url = await vca._assemble_and_upload_clip([clip_a, clip_b], "group-1", "session-1")

    assert url == "https://cdn.example/final.mp4"
    assert len(trim_calls) == 2
    assert len(concat_calls) == 1
    assert len(concat_calls[0]) == 2


async def test_assemble_and_upload_clip_returns_none_when_every_clip_fails(monkeypatch):
    clip_a = vca.ExpandedClip(raw_segment_id="seg-a", start_sec=1.0, end_sec=3.0, source_chunk_id="c-a")
    segments = {
        "seg-a": RawSegment(id="seg-a", video_key="videos/seg-a.mp4", interview_session_id="x", question_asked="q", question_index=0),
    }
    monkeypatch.setattr(vca, "_fetch_segment_videos", AsyncMock(return_value=segments))
    monkeypatch.setattr(
        vca.storage_service, "download_file", AsyncMock(side_effect=RuntimeError("storage down"))
    )
    upload_mock = AsyncMock()
    monkeypatch.setattr(vca.storage_service, "upload_file", upload_mock)

    url = await vca._assemble_and_upload_clip([clip_a], "group-1", "session-1")

    assert url is None
    upload_mock.assert_not_called()


async def test_assemble_and_upload_clip_skips_segment_with_no_video_key(monkeypatch):
    clip_a = vca.ExpandedClip(raw_segment_id="seg-a", start_sec=1.0, end_sec=3.0, source_chunk_id="c-a")
    segments = {
        "seg-a": RawSegment(id="seg-a", video_key=None, interview_session_id="x", question_asked="q", question_index=0),
    }
    monkeypatch.setattr(vca, "_fetch_segment_videos", AsyncMock(return_value=segments))
    download_mock = AsyncMock()
    monkeypatch.setattr(vca.storage_service, "download_file", download_mock)

    url = await vca._assemble_and_upload_clip([clip_a], "group-1", "session-1")

    assert url is None
    download_mock.assert_not_called()


# ── photo_categories_for_segments (MEDIA_GALLERY.md §9.4) ───────────────────


async def _seed_segment(factory, user_id: str, segment_id: str, question_id):
    async with factory() as db:
        session = InterviewSession(id=f"is-{segment_id}", user_id=user_id, status="active")
        db.add(session)
        await db.flush()
        db.add(
            RawSegment(
                id=segment_id,
                interview_session_id=session.id,
                question_asked="q",
                question_index=0,
                status="ready",
                question_id=question_id,
            )
        )
        await db.commit()


async def test_photo_categories_resolve_dedupe_and_keep_play_order(
    vca_session_factory, test_user
):
    """A LOOKUP through question_id, never a classification — and the order
    is the order the answer plays its recordings, deduped, so the /talk
    gallery leads with the period the clip opens in."""
    from app import interview_config

    cats = interview_config.get_categories("he")
    q_a = cats[0]["question_ids"][0]
    q_b = cats[1]["question_ids"][0]
    await _seed_segment(vca_session_factory, test_user.id, "seg-a", q_a)
    await _seed_segment(vca_session_factory, test_user.id, "seg-b", q_b)
    await _seed_segment(vca_session_factory, test_user.id, "seg-a2", q_a)

    # b first, then a twice — the result is b's category then a's, once each.
    result = await vca.photo_categories_for_segments(["seg-b", "seg-a", "seg-a2"])
    assert result == [cats[1]["category"], cats[0]["category"]]


async def test_photo_categories_skip_what_cannot_be_resolved(
    vca_session_factory, test_user
):
    """A segment with no question id (an upload outside the guided set), an
    invented id, or an unknown segment contributes NOTHING — the gallery
    never guesses a period (the year-attribution rule, applied to photos)."""
    await _seed_segment(vca_session_factory, test_user.id, "seg-none", None)
    await _seed_segment(vca_session_factory, test_user.id, "seg-bogus", "not-a-question-id")

    assert await vca.photo_categories_for_segments(
        ["seg-none", "seg-bogus", "seg-missing"]
    ) == []
    assert await vca.photo_categories_for_segments([]) == []
