"""
Video-clip ASSEMBLY — the shared layer that turns chosen time ranges into
one real, verbatim clip: ffmpeg trim (frame-accurate re-encode) + concat +
upload, plus the clip cache key and the Phase 8 category lookup.

This module once also held the v1 (`video_clips`) chunk-retrieval
orchestration — the Prompt 12-14 chain of per-candidate LLM verification,
sub-phrase pinpointing and boundary expansion. That mode was removed after
the A/B against the full-archive reader settled it (docs/V1_REMOVAL_PLAN.md;
`pre-v1-removal` tags the last tree that had it). What remains is exactly
the code the surviving mode uses: full_archive_retrieval decides WHICH
ranges answer a question and calls down into here to make them a video.

Caching note (the CACHE_TTL_SECONDS consumer lives in the caller):
identical questions landing on the exact same final range set would
otherwise reprocess through ffmpeg every time. The cache key is a
deterministic hash of the final (segment, start, end) tuples, TTL-bounded
so a re-recorded or deleted segment can't leave a permanently stale clip
referenced forever.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import RawSegment
from app.services.storage import storage_service

logger = logging.getLogger(__name__)

TMPDIR = Path(tempfile.gettempdir())


CACHE_TTL_SECONDS = 24 * 60 * 60  # see module docstring's "Caching" section

# subprocess.run has no timeout by default — a stuck/hung ffmpeg process
# (malformed input, an unexpected interactive prompt despite -y, etc.) would
# otherwise block the whole WS turn forever with no exception ever raised,
# the same class of bug as an unbounded LLM client timeout (see llm.py's
# LLM_CALL_TIMEOUT_SECONDS). A single clip trim/concat is always a short
# piece of video (a handful of utterance units), so this is generous.
FFMPEG_TIMEOUT_SECONDS = 60


@dataclass
class ExpandedClip:
    raw_segment_id: str
    start_sec: float
    end_sec: float
    source_chunk_id: str  # the chunk (or unit's chunk) this range came from


@dataclass
class VideoClipResult:
    video_url: Optional[str]
    no_story: bool = False
    fallback_text: str = ""
    # Which utterance units this answer played, as {key, unit_id, text}.
    # Persisted on the assistant message so the next turn knows what was
    # already shown and what it said.
    shown_units: List[dict] = field(default_factory=list)
    # {"question": str} offering to continue with related material that
    # exists in the archive and hasn't been shown. Chat text ONLY — it is
    # never spoken and never part of the video, which stays verbatim footage.
    follow_up: Optional[dict] = None
    # {"question": str, "options": [str, ...]} when the question could have
    # meant either of two people who share a name. Set INSTEAD of a video,
    # never alongside one — a best guess plus "or did you mean the other?" is
    # the conflation this exists to remove, wearing a question mark.
    clarify: Optional[dict] = None
    # The archive read never completed (API failure), so this result says
    # NOTHING about what the archive contains. Presenting it as a no-story
    # tells a family member their relative has no story about something the
    # archive may cover in full.
    read_failed: bool = False
    # The life-period categories of the recordings this answer's footage came
    # from, deduped, first-appearance order — /talk shows each category's
    # photo gallery under the clip (MEDIA_GALLERY.md §9.4). A LOOKUP through
    # question_id, never a classification: a clip already knows which period
    # it belongs to. Empty when nothing played.
    photo_categories: List[str] = field(default_factory=list)


def _run_ffmpeg(cmd: List[str]) -> "subprocess.CompletedProcess[bytes]":
    """Plain synchronous subprocess, always called via asyncio.to_thread —
    NOT asyncio.create_subprocess_exec, which is unimplemented under
    WindowsSelectorEventLoopPolicy (pinned in main.py for psycopg3/
    LangGraph). Same workaround animator.py's _animate_simple already
    established for this exact codebase/platform combination.

    timeout= is required here: subprocess.run has none by default, so a
    stuck ffmpeg process would otherwise hang this thread (and the WS turn
    awaiting it) forever with no exception. Converted to a plain
    RuntimeError on expiry so callers' existing error handling (or, for
    _assemble_and_upload_clip's per-clip fail-soft loop, a skip-and-continue)
    applies uniformly instead of needing a separate TimeoutExpired case."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"ffmpeg timed out after {FFMPEG_TIMEOUT_SECONDS}s: {' '.join(cmd)}"
        ) from e


async def _trim_clip(source_path: Path, start_sec: float, end_sec: float, output_path: Path) -> None:
    """Re-encodes (not stream-copy) for frame-accurate cut points — a
    stream-copy trim only snaps to keyframes, which could be off by a
    second or more depending on the source's GOP structure, undermining
    the whole point of a word-timestamp-precise pinpointed answer.

    `-preset veryfast` because re-encoding is ~98% of assembly time and the
    default (medium) buys nothing here. MEASURED on a real 4.2s cut from this
    archive, 5 runs: 1.149s -> 0.491s, and the output got SMALLER (0.79 MB ->
    0.57 MB, 1432 -> 1000 kbps) with identical duration, resolution and frame
    count. Faster AND smaller is unusual for a faster preset; it holds here
    because the footage is a static talking head, which x264 encodes well
    without the slower motion search. Confirmed by eye on the face/lip region
    before landing, since that is where compression artefacts would show
    first and this footage is a person talking.

    Whole-assembly effect, same archive: 0.761s -> 0.338s for a
    single-recording answer, 3.057s -> 1.275s for one spanning two.

    Do NOT reach for `-preset ultrafast`: measured 0.284s but 2.10 MB — it
    trades away far more bitrate than the extra 0.2s is worth, and the client
    downloads the result."""
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start_sec:.3f}",
        "-to",
        f"{end_sec:.3f}",
        "-i",
        str(source_path),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        str(output_path),
    ]
    result = await asyncio.to_thread(_run_ffmpeg, cmd)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg trim failed: {result.stderr.decode(errors='replace')}")


async def _concat_clips(clip_paths: List[Path], output_path: Path) -> None:
    """ffmpeg's concat demuxer — safe to stream-copy here since every input
    was already re-encoded to the same H.264/AAC params by _trim_clip
    above, so no further quality loss or re-encoding cost."""
    list_path = output_path.with_suffix(".txt")
    list_path.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in clip_paths), encoding="utf-8"
    )
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(output_path)]
    try:
        result = await asyncio.to_thread(_run_ffmpeg, cmd)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {result.stderr.decode(errors='replace')}")
    finally:
        list_path.unlink(missing_ok=True)


def _clip_cache_key(group_id: str, expanded: List[ExpandedClip]) -> str:
    """Deterministic across identical final chunk-range sets — see module
    docstring's "Caching" section. Rounded to whole seconds: sub-second
    jitter in pinpointing (e.g. word-boundary rounding) shouldn't defeat an
    otherwise-identical cache hit."""
    parts = sorted(
        f"{c.raw_segment_id}:{round(c.start_sec)}:{round(c.end_sec)}" for c in expanded
    )
    digest = hashlib.sha256(f"{group_id}|{'|'.join(parts)}".encode("utf-8")).hexdigest()
    return f"video_clip_assembly:{digest}"


async def _fetch_segment_videos(raw_segment_ids: List[str]) -> Dict[str, RawSegment]:
    if not raw_segment_ids:
        return {}
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(RawSegment).where(RawSegment.id.in_(raw_segment_ids)))
        return {seg.id: seg for seg in result.scalars().all()}


async def _assemble_and_upload_clip(
    expanded: List[ExpandedClip], group_id: str, session_id: str
) -> Optional[str]:
    """Downloads each expanded clip's parent video, trims it, concatenates
    all trims in order, uploads the result. Per-clip fail-soft: a single
    clip's download/trim failure is logged and that clip excluded from the
    final concatenation rather than failing the whole response — if NONE
    survive, returns None (caller applies NO_STORY_FALLBACK)."""
    segments_by_id = await _fetch_segment_videos(
        list({c.raw_segment_id for c in expanded})
    )

    work_dir = Path(tempfile.mkdtemp(dir=TMPDIR, prefix="video-clip-assembly-"))
    trimmed_paths: List[Path] = []
    # A single source segment can now contribute MULTIPLE non-contiguous
    # pieces (relevance-per-sub-topic splitting) — download its video once
    # and reuse the local copy for every piece trimmed from it, rather than
    # re-downloading the same file per piece.
    source_paths_by_segment: Dict[str, Path] = {}
    try:
        for i, clip in enumerate(expanded):
            segment = segments_by_id.get(clip.raw_segment_id)
            if segment is None or not segment.video_key:
                logger.warning(f"No source video for segment {clip.raw_segment_id}; skipping clip")
                continue
            try:
                source_path = source_paths_by_segment.get(clip.raw_segment_id)
                if source_path is None:
                    video_bytes = await storage_service.download_file(segment.video_key)
                    source_path = work_dir / f"source_{clip.raw_segment_id}.mp4"
                    source_path.write_bytes(video_bytes)
                    source_paths_by_segment[clip.raw_segment_id] = source_path
                trimmed_path = work_dir / f"trim_{i}.mp4"
                await _trim_clip(source_path, clip.start_sec, clip.end_sec, trimmed_path)
                trimmed_paths.append(trimmed_path)
            except Exception as e:
                logger.warning(f"Failed to trim clip from segment {clip.raw_segment_id}: {e}")
                continue

        if not trimmed_paths:
            return None

        if len(trimmed_paths) == 1:
            final_path = trimmed_paths[0]
        else:
            final_path = work_dir / "final.mp4"
            await _concat_clips(trimmed_paths, final_path)

        video_bytes = final_path.read_bytes()
        cache_key = _clip_cache_key(group_id, expanded)
        storage_key = f"video-clips/{group_id}/{hashlib.sha256(cache_key.encode()).hexdigest()}.mp4"
        await storage_service.upload_file(video_bytes, storage_key, content_type="video/mp4")
        return await storage_service.serving_url(storage_key)
    finally:
        for p in work_dir.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            work_dir.rmdir()
        except OSError:
            pass


async def photo_categories_for_segments(segment_ids: List[str]) -> List[str]:
    """The life periods these recordings answer for, deduped, in the order
    the segments were given (i.e. the order the answer plays them).

    A lookup, not a classification (MEDIA_GALLERY.md §9.4): every recording
    carries `question_id`, and `category_for_question_id` resolves it live or
    retired. A segment with no question id (an upload outside the guided set)
    or an unresolvable one contributes nothing — never a guess.
    """
    from app import interview_config

    ordered_unique = list(dict.fromkeys(segment_ids))
    if not ordered_unique:
        return []
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(RawSegment.id, RawSegment.question_id).where(
                    RawSegment.id.in_(ordered_unique)
                )
            )
        ).all()
    question_id_by_segment = {sid: qid for sid, qid in rows}
    categories: List[str] = []
    for sid in ordered_unique:
        qid = question_id_by_segment.get(sid)
        category = interview_config.category_for_question_id(qid) if qid else None
        if category and category not in categories:
            categories.append(category)
    return categories
