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
import re
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

# Smart-cut (2026-09-03): a long trim used to re-encode EVERY frame at
# ~6.6x realtime (measured: a 74s answer clip cost 11.2s), yet the only
# frames that NEED re-encoding are the partial GOPs at the cut points -
# everything between the first keyframe after the head cut and the last
# keyframe before the tail cut can be stream-copied bit-for-bit (also
# skipping a generation loss for ~95% of the footage). Audio is deliberately
# NOT spliced: it is re-encoded continuously in ONE pass over the whole
# range (audio encoding is ~free) and muxed onto the concatenated video, so
# there is no audible seam at the video splice points. The result is
# VERIFIED (duration + full decode) and ANY doubt falls back to the plain
# full re-encode - this optimization must never ship a broken clip.
SMART_CUT_MIN_CLIP_SECONDS = 20.0  # below this the full re-encode is already fast
_SMART_CUT_MIN_MIDDLE_SECONDS = 8.0  # smaller copied middle isn't worth 6 ffmpeg calls
_KEYFRAME_SCAN_WINDOW_SECONDS = 35.0  # > any sane GOP (measured 10s on bulk imports)


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
    # Where the unit selection came from ("fresh" / "cache" /
    # "speculative") — see UnitSelection.answer_source.
    answer_source: str = "fresh"
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
    """Frame-accurate trim. Long clips take the smart-cut path (re-encode
    only the partial GOPs at the cut points, stream-copy the middle,
    continuous audio; verified, fail-soft); short clips and every failure
    shape take the full re-encode below — the always-correct baseline."""
    duration = end_sec - start_sec
    if duration >= SMART_CUT_MIN_CLIP_SECONDS:
        try:
            if await _smart_trim(source_path, start_sec, end_sec, output_path):
                return
        except Exception as e:
            logger.warning(f"smart-cut failed ({e}); falling back to full re-encode")
    await _full_reencode_trim(source_path, start_sec, end_sec, output_path)


def _probe_json(args: List[str]) -> dict:
    import json as _json

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", *args],
        capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.decode(errors='replace')}")
    return _json.loads(result.stdout.decode() or "{}")


def _keyframes_in(source_path: Path, lo: float, hi: float) -> List[float]:
    data = _probe_json(
        [
            "-select_streams", "v:0", "-skip_frame", "nokey",
            "-show_entries", "frame=pts_time",
            "-read_intervals", f"{max(0.0, lo):.3f}%{hi:.3f}",
            str(source_path),
        ]
    )
    out = []
    for f in data.get("frames", []):
        try:
            out.append(float(f["pts_time"]))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def _clip_duration(path: Path) -> float:
    data = _probe_json(["-show_entries", "format=duration", str(path)])
    return float(data.get("format", {}).get("duration", 0.0))


#: The ONE decode-check message the verifier tolerates: at each concat seam
#: the container offsets can round a frame's DTS by a tick, and the null
#: muxer reports "non monotonically increasing dts ... A >= B". Measured on
#: every smart-cut output (values equal or off by one tick — sub-frame,
#: inaudible, invisible; rc stays 0 and players handle it). The tolerance is
#: BOUNDED: only this exact pattern, only |A-B| <= 2 ticks, only a seam's
#: worth of lines — a corrupt frame, missing reference or bad NAL prints
#: different text and still fails verification.
_SEAM_DTS_RE = re.compile(
    rb"non monotonically increasing dts to muxer in stream \d+: (\d+) >= (\d+)"
)


def _decode_stderr_is_benign(stderr: bytes) -> bool:
    lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    if len(lines) > 6:
        return False
    for ln in lines:
        m = _SEAM_DTS_RE.search(ln)
        if not m or abs(int(m.group(1)) - int(m.group(2))) > 2:
            return False
    return True


def _verify_clip(path: Path, expected_duration: float) -> bool:
    """The gate that makes smart-cut shippable: duration within tolerance
    AND a full decode that is clean apart from the bounded seam-rounding
    pattern above. A GOP-snapped cut or a glitchy splice shows up here; on
    any failure the caller falls back to the full re-encode, so a wrong
    smart-cut can cost time but never correctness."""
    try:
        if abs(_clip_duration(path) - expected_duration) > 0.5:
            return False
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return False
        return not result.stderr.strip() or _decode_stderr_is_benign(result.stderr)
    except Exception:
        return False


async def _smart_trim(
    source_path: Path, start_sec: float, end_sec: float, output_path: Path
) -> bool:
    """True = output_path holds a VERIFIED frame-accurate trim. False/raise =
    caller must run the full re-encode (output_path untouched or ignored)."""
    # Source video params — the re-encoded ends must match the copied middle
    # closely enough for one MP4 track. h264 only; anything else falls back.
    data = _probe_json(
        [
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,profile,pix_fmt,width,height",
            str(source_path),
        ]
    )
    streams = data.get("streams") or []
    if not streams or streams[0].get("codec_name") != "h264":
        return False
    src = streams[0]

    head_kfs = _keyframes_in(
        source_path, start_sec, start_sec + _KEYFRAME_SCAN_WINDOW_SECONDS
    )
    kf1 = next((k for k in head_kfs if k > start_sec + 0.05), None)
    tail_kfs = _keyframes_in(
        source_path, end_sec - _KEYFRAME_SCAN_WINDOW_SECONDS, end_sec
    )
    kf2 = next((k for k in reversed(tail_kfs) if k < end_sec - 0.05), None)
    if kf1 is None or kf2 is None or (kf2 - kf1) < _SMART_CUT_MIN_MIDDLE_SECONDS:
        return False  # no worthwhile copyable middle — full re-encode is fine

    work = output_path.parent
    stem = output_path.stem
    head = work / f"{stem}_sc_head.mp4"
    middle = work / f"{stem}_sc_mid.mp4"
    tail = work / f"{stem}_sc_tail.mp4"
    video_only = work / f"{stem}_sc_video.mp4"
    audio = work / f"{stem}_sc_audio.m4a"
    pieces = [head, middle, tail, video_only, audio]

    profile = (src.get("profile") or "").lower()
    # -bf 0: no B-frames in the re-encoded END pieces, so their DTS ends
    # exactly at the seam instead of leading it — the concat joins then
    # carry at most sub-frame rounding, not reordering overlap.
    encode_args = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-bf", "0"]
    if src.get("pix_fmt"):
        encode_args += ["-pix_fmt", src["pix_fmt"]]
    if profile in ("baseline", "main", "high"):
        encode_args += ["-profile:v", profile]
    # Uniform container timescale across all three pieces for the concat.
    scale_args = ["-video_track_timescale", "90000"]

    async def _run(cmd: List[str]) -> None:
        result = await asyncio.to_thread(_run_ffmpeg, cmd)
        if result.returncode != 0:
            raise RuntimeError(
                f"smart-cut step failed: {result.stderr.decode(errors='replace')[:300]}"
            )

    try:
        # Head partial GOP: start → first keyframe after start (re-encode).
        await _run(
            ["ffmpeg", "-y", "-ss", f"{start_sec:.3f}", "-to", f"{kf1:.6f}",
             "-i", str(source_path), *encode_args, *scale_args, str(head)]
        )
        # Middle: keyframe → keyframe, bit-for-bit copy (starts AT a
        # keyframe, so every copied frame is decodable). PRECISION MATTERS:
        # a seek timestamp even a microsecond BEFORE the keyframe lands the
        # copy on the PREVIOUS keyframe (measured: a 30fps source with a
        # keyframe at 8.333333s, cut with "8.333", copied from 0.0 and
        # inflated the clip by 8.3s — caught by _verify_clip). The +1ms
        # epsilon keeps the seek at-or-after kf1; the -1ms end keeps the
        # middle's last packet strictly before kf2, whose frame the
        # re-encoded tail provides.
        await _run(
            ["ffmpeg", "-y", "-ss", f"{kf1 + 0.001:.6f}", "-to", f"{kf2 - 0.001:.6f}",
             "-i", str(source_path), "-an", "-c:v", "copy",
             "-avoid_negative_ts", "make_zero", *scale_args, str(middle)]
        )
        # Tail partial GOP: last keyframe before end → end (re-encode).
        await _run(
            ["ffmpeg", "-y", "-ss", f"{kf2:.6f}", "-to", f"{end_sec:.3f}",
             "-i", str(source_path), *encode_args, *scale_args, str(tail)]
        )
        await _concat_clips([head, middle, tail], video_only)
        # ONE continuous audio pass over the whole range — no audio splices.
        await _run(
            ["ffmpeg", "-y", "-ss", f"{start_sec:.3f}", "-to", f"{end_sec:.3f}",
             "-i", str(source_path), "-vn", "-c:a", "aac", str(audio)]
        )
        await _run(
            ["ffmpeg", "-y", "-i", str(video_only), "-i", str(audio),
             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(output_path)]
        )
        expected = end_sec - start_sec
        if not await asyncio.to_thread(_verify_clip, output_path, expected):
            logger.warning(
                f"smart-cut verification failed for {source_path.name} "
                f"({start_sec:.1f}-{end_sec:.1f}); using full re-encode"
            )
            return False
        logger.info(
            f"smart-cut: {expected:.0f}s clip, copied middle {kf2 - kf1:.0f}s, "
            f"re-encoded {(kf1 - start_sec) + (end_sec - kf2):.1f}s"
        )
        return True
    finally:
        for piece in pieces:
            try:
                piece.unlink()
            except OSError:
                pass


async def _full_reencode_trim(source_path: Path, start_sec: float, end_sec: float, output_path: Path) -> None:
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
    # A single source segment can now contribute MULTIPLE non-contiguous
    # pieces (relevance-per-sub-topic splitting) — download its video once
    # and reuse the local copy for every piece trimmed from it, rather than
    # re-downloading the same file per piece.
    source_paths_by_segment: Dict[str, Path] = {}
    try:
        # CONCURRENT assembly (2026-09-03). The trims are independent
        # re-encodes of different byte ranges — running them sequentially
        # made a broad multi-recording answer's assembly time the sum of
        # its clip durations (measured: ~25s of a 40.9s live turn was this
        # loop). Downloads are gathered per unique segment, then every trim
        # runs at once (each is subprocess.run on its own thread via
        # asyncio.to_thread — the Windows-safe pattern this module already
        # uses). Order and the per-clip fail-soft contract are unchanged:
        # results are collected in the original clip order, and a single
        # clip's failure logs a warning and excludes THAT clip only.

        async def _download(segment) -> None:
            video_bytes = await storage_service.download_file(segment.video_key)
            source_path = work_dir / f"source_{segment.id}.mp4"
            source_path.write_bytes(video_bytes)
            source_paths_by_segment[segment.id] = source_path

        unique_segments = []
        seen_ids = set()
        for clip in expanded:
            segment = segments_by_id.get(clip.raw_segment_id)
            if segment is None or not segment.video_key:
                if clip.raw_segment_id not in seen_ids:
                    logger.warning(
                        f"No source video for segment {clip.raw_segment_id}; skipping clip"
                    )
                    seen_ids.add(clip.raw_segment_id)
                continue
            if segment.id not in seen_ids:
                seen_ids.add(segment.id)
                unique_segments.append(segment)
        dl_results = await asyncio.gather(
            *(_download(seg) for seg in unique_segments), return_exceptions=True
        )
        for seg, res in zip(unique_segments, dl_results):
            if isinstance(res, BaseException):
                logger.warning(f"Failed to download source for segment {seg.id}: {res}")

        async def _trim_one(i: int, clip) -> Optional[Path]:
            source_path = source_paths_by_segment.get(clip.raw_segment_id)
            if source_path is None:
                return None  # download failed/skipped — already logged
            try:
                trimmed_path = work_dir / f"trim_{i}.mp4"
                await _trim_clip(source_path, clip.start_sec, clip.end_sec, trimmed_path)
                return trimmed_path
            except Exception as e:
                logger.warning(
                    f"Failed to trim clip from segment {clip.raw_segment_id}: {e}"
                )
                return None

        results = await asyncio.gather(
            *(_trim_one(i, clip) for i, clip in enumerate(expanded))
        )
        trimmed_paths: List[Path] = [p for p in results if p is not None]

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
