"""
Video-clip answer assembly (Prompt 13) — the video-clip-mode parallel to
response_assembler.py's text assembly, built ALONGSIDE it, not replacing
it (see docs/poc-claude-code-prompts.md's "Shared context" hard
constraint). Takes Prompt 12's chunk-level retrieval candidates and
produces a single, real, verbatim video clip (or a stitched sequence of
clips from non-contiguous moments, possibly across different recordings)
answering a question, instead of synthesizing an avatar.

`assemble_video_clip_response(question, group_id, recording_language, session_id)`:
1. retrieval_service.retrieve_chunks() (Prompt 12) for candidate chunks —
   already a flat, deduplicated primary+bridged list in that order.
2. ONE lightweight, temperature=0 LLM call per candidate, combining
   relevance verification + sub-phrase pinpointing + clause coverage (per
   the prompt's own latency/cost guidance) — rejects loose topic/embedding-
   only matches, locates the exact answering sub-phrase via Prompt 11's
   word-level timestamps, and reports which part(s) of a multi-clause
   question each surviving chunk addresses. Fail-soft: an LLM failure or a
   hallucinated (non-verbatim) substring falls back to treating the WHOLE
   chunk as relevant with its own full boundaries — never blocks the
   response, never invents text. A genuine "not relevant" verdict is a
   real rejection, not a failure, and is NOT fail-soft.
3. Each surviving chunk's PINPOINTED sub-range (not the whole chunk) is
   expanded outward to neighboring chunks (Prompt 11's sequence_index) up
   to a max duration, stopping at topic drift or a long silence gap.
4. Each expanded range is ffmpeg-trimmed from its parent recording and all
   of them concatenated, in the same primary-then-bridge order
   retrieve_chunks already returned, into one output file, uploaded to
   storage.
5. If nothing survives retrieval or verification, returns the response_
   assembler.NO_STORY_FALLBACK text (reused, not duplicated) with no
   video — matching the avatar path's "never invent" guarantee exactly.

Caching: identical questions (or different questions landing on the exact
same final chunk-range set) would otherwise reprocess through ffmpeg every
time, which has real latency/cost unlike the avatar path's near-instant
text assembly. Cached in cache_service (Redis, already wired up) keyed by
a deterministic hash of the final (chunk_id, start_sec, end_sec) tuples,
for CACHE_TTL_SECONDS — long enough to avoid repeat cost within/across
nearby conversations, short enough that a producer re-recording or
deleting a segment doesn't leave a permanently stale clip referenced
forever. This project has no explicit cache-invalidation hook for that
case (no code path currently deletes/re-analyzes a 'ready' segment in
place); a TTL is the pragmatic bound in lieu of assuming eternal validity
or building that invalidation plumbing now.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import RawSegment, TranscriptChunk
from app.services import embeddings, retrieval_service
from app.services.cache import cache_service
from app.services.llm import llm_service
from app.services.response_assembler import NO_STORY_FALLBACK
from app.services.retrieval_service import _parse_json_array
from app.services.storage import storage_service

logger = logging.getLogger(__name__)

TMPDIR = Path(tempfile.gettempdir())

# Boundary expansion (step 3 above) — deliberately plain module-level
# constants, not settings, matching retrieval_service.py/relevance_scorer.py's
# own convention: real tuning needs a QA-harness-style pass against actual
# assembled clips, not a guess here.
MAX_CLIP_DURATION_SEC = 30.0
MAX_SILENCE_GAP_SEC = 5.0
# Looser than retrieval's own SEMANTIC_CHUNK_MATCH_THRESHOLD (0.68) on
# purpose — this only guards against expanding into a CLEARLY unrelated
# neighbor, not re-matching the question; a neighbor a few points below the
# strict retrieval bar can still be a natural continuation of the same
# moment.
TOPIC_DRIFT_EMBEDDING_THRESHOLD = 0.5

CACHE_TTL_SECONDS = 24 * 60 * 60  # see module docstring's "Caching" section

# subprocess.run has no timeout by default — a stuck/hung ffmpeg process
# (malformed input, an unexpected interactive prompt despite -y, etc.) would
# otherwise block the whole WS turn forever with no exception ever raised,
# the same class of bug as an unbounded LLM client timeout (see llm.py's
# LLM_CALL_TIMEOUT_SECONDS). A single clip trim/concat here is always a
# short (MAX_CLIP_DURATION_SEC-bounded) piece of video, so this is generous.
FFMPEG_TIMEOUT_SECONDS = 60


_VERIFY_PINPOINT_SYSTEM_PROMPT_TEMPLATE = """\
You are analysing whether a short excerpt from someone's own recorded \
life story actually answers a question, for a system that assembles \
REAL video clips from those recordings - it must never guess, embellish, \
or invent, only report what is verifiably true about the given excerpt.

The question being asked has these distinct part(s):
{clauses_block}

The excerpt (verbatim, from the storyteller's own recording) is:
"{chunk_text}"

This excerpt may run several distinct sub-topics together with no pause \
between them (e.g. childhood home, then siblings, then parents, then \
schooling, all in one breath, with no punctuation or pause marking the \
shift). Identify the DISTINCT SUB-TOPICS actually present, based on where \
the subject matter itself genuinely changes - NEVER by a length or \
duration threshold - then judge EACH sub-topic's relevance to the \
question independently. A sub-topic is relevant only if it actually \
answers part of the question, not merely because it sits near a relevant \
sub-topic in the excerpt.

Output ONLY a JSON object with exactly these fields:
- "relevant": true or false - is AT LEAST ONE sub-topic in this excerpt \
ACTUALLY a good, specific answer to at least one part of the question, \
not just a loose topical or keyword overlap?
- "answer_substrings": a JSON array of the EXACT contiguous substring(s) \
of the excerpt above (character-for-character, verbatim, same language), \
one entry per distinct RELEVANT sub-topic - covering ONLY the sub-topic(s) \
that actually answer the question, never an irrelevant sub-topic's text \
even if it sits between two relevant ones. Do not merge two non-adjacent \
relevant sub-topics into a single entry. If EVERY sub-topic in the \
excerpt turns out relevant (a genuinely broad question can legitimately \
need the whole excerpt), that is fine - return the whole excerpt as one \
entry; the length of the answer must always fall out of how much is \
actually relevant, never be trimmed to shorten it or padded to lengthen \
it. NEVER invent, paraphrase, or alter text that is not literally present \
in the excerpt. Empty array if "relevant" is false.
- "covered_clause_indices": a JSON array of the 0-based indices (from the \
numbered list above) of which part(s) this excerpt actually addresses. \
Empty array if none - should be empty whenever "relevant" is false.

No commentary, no text outside the JSON object."""

_SPLIT_CLAUSES_SYSTEM_PROMPT_TEMPLATE = """\
You are splitting a question into its distinct parts for a search system \
- you are not answering it. If the question asks about only ONE \
distinct thing, output a JSON array containing just the question itself, \
unchanged, as its single element. If it asks about MULTIPLE distinct \
things (e.g. "what did you do for work, did you enjoy it, tell me \
stories" has three distinct parts: what the work was, whether they \
enjoyed it, and a request for stories), output a JSON array with one \
short string per distinct part, in {language}, staying as close to the \
original wording as possible. Output ONLY the JSON array, no commentary."""


@dataclass
class VerifiedChunk:
    chunk: TranscriptChunk
    # One or more non-contiguous (start_sec, end_sec) sub-ranges within this
    # chunk that actually answer the question — relevance-per-sub-topic,
    # never a duration split. Sorted, non-overlapping. A single-entry list
    # covering the whole chunk is the fail-soft/no-narrowing-needed case.
    answer_ranges: List[Tuple[float, float]]
    covered_clause_indices: List[int] = field(default_factory=list)


@dataclass
class ExpandedClip:
    raw_segment_id: str
    start_sec: float
    end_sec: float
    source_chunk_id: str  # the VerifiedChunk this expansion started from


@dataclass
class VideoClipResult:
    video_url: Optional[str]
    no_story: bool = False
    fallback_text: str = ""
    uncovered_clauses: List[str] = field(default_factory=list)
    # v2 (full_archive_retrieval) only: which utterance units this answer
    # played, as {key, unit_id, text}. Persisted on the assistant message so
    # the next turn knows what was already shown and what it said. Left empty
    # by v1, which has no unit concept.
    shown_units: List[dict] = field(default_factory=list)
    # v2 only: {"question": str} offering to continue with related material
    # that exists in the archive and hasn't been shown. Chat text ONLY — it is
    # never spoken and never part of the video, which stays verbatim footage.
    follow_up: Optional[dict] = None


def _parse_json_object(text: str) -> Optional[dict]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _split_question_into_clauses(question: str, recording_language: str) -> List[str]:
    """One-time (not per-candidate) split — every per-chunk verification
    call shares this SAME clause list so clause indices mean the same
    thing across all of them. Fails soft to a single clause (the whole
    question, unchanged) rather than blocking assembly."""
    system_prompt = _SPLIT_CLAUSES_SYSTEM_PROMPT_TEMPLATE.format(language=recording_language)
    try:
        raw = await llm_service.generate_response(
            messages=[{"role": "user", "content": question}],
            system_prompt=system_prompt,
            temperature=0,
        )
        clauses = _parse_json_array(raw)
    except Exception as e:
        logger.warning(f"Clause splitting failed, treating question as one clause: {e}")
        clauses = []
    return clauses if clauses else [question]


def _locate_substring_in_word_timestamps(
    substring: str, word_timestamps: List[dict]
) -> Optional[Tuple[float, float]]:
    """Best-effort character-offset mapping from a verbatim substring back
    to Prompt 11's word-level timestamps. Returns None (caller falls back
    to the chunk's own full boundaries) if the mapping can't be made
    confidently — this is inherently heuristic, not required to be exact,
    since fail-soft to whole-chunk boundaries is always a safe landing."""
    if not substring or not word_timestamps:
        return None

    words = [w.get("word", "") for w in word_timestamps]
    target = " ".join(substring.split())

    def _find(joined: str) -> int:
        return joined.find(target)

    joined = " ".join(w.strip() for w in words)
    idx = _find(joined)
    if idx == -1:
        return None

    end_idx = idx + len(target)
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None
    pos = 0
    for i, w in enumerate(words):
        w_stripped = w.strip()
        w_start, w_end = pos, pos + len(w_stripped)
        if start_sec is None and w_end > idx:
            start_sec = word_timestamps[i]["start_sec"]
        if w_start < end_idx:
            end_sec = word_timestamps[i]["end_sec"]
        pos = w_end + 1  # +1 for the single joining space
        if pos > end_idx and end_sec is not None:
            break

    if start_sec is None or end_sec is None:
        return None
    return (start_sec, end_sec)


# Sub-ranges within the SAME chunk closer than this are touching/adjacent
# in practice (word-boundary rounding, not a real gap) — merged into one
# contiguous piece so ffmpeg doesn't cut-and-reconcatenate across a seam
# that isn't actually there. This is NOT MAX_SILENCE_GAP_SEC (that governs
# whether _expand_chunk_boundaries bridges into a NEIGHBORING chunk, a
# fundamentally different decision) — this is only for collapsing
# essentially-touching model-provided sub-ranges within one chunk.
_ADJACENT_RANGE_MERGE_EPSILON_SEC = 0.15


def _merge_adjacent_ranges(ranges: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not ranges:
        return []
    ranges = sorted(ranges)
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        last_start, last_end = merged[-1]
        if start - last_end <= _ADJACENT_RANGE_MERGE_EPSILON_SEC:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


async def _verify_and_pinpoint_chunk(
    chunk: TranscriptChunk, original_question: str, clauses: List[str]
) -> Optional[VerifiedChunk]:
    """The ORIGINAL (non-normalized) question is used here — Prompt 12's
    perspective-normalized rewrite was for search only; the actual answer
    must address what the family member really asked."""
    system_prompt = _VERIFY_PINPOINT_SYSTEM_PROMPT_TEMPLATE.format(
        clauses_block="\n".join(f"{i}. {c}" for i, c in enumerate(clauses)),
        chunk_text=chunk.text,
    )
    try:
        raw = await llm_service.generate_response(
            messages=[{"role": "user", "content": original_question}],
            system_prompt=system_prompt,
            temperature=0,
        )
        data = _parse_json_object(raw)
        if data is None:
            raise ValueError(f"unparseable verification response: {raw!r}")
    except Exception as e:
        # Fail-soft (per spec): an LLM/parse FAILURE treats the whole chunk
        # as relevant with its own full boundaries — this is different from
        # a genuine "relevant: false" verdict below, which IS a real
        # rejection, not a technical failure.
        logger.warning(
            f"Verification/pinpointing call failed for chunk {chunk.id}, "
            f"treating whole chunk as relevant: {e}"
        )
        return VerifiedChunk(
            chunk=chunk,
            answer_ranges=[(chunk.start_sec, chunk.end_sec)],
            covered_clause_indices=[],  # unknown — never claim coverage we can't verify
        )

    if not data.get("relevant"):
        return None

    substrings = data.get("answer_substrings") or []
    raw_covered = data.get("covered_clause_indices") or []
    covered = [i for i in raw_covered if isinstance(i, int) and 0 <= i < len(clauses)]

    ranges: List[Tuple[float, float]] = []
    for substring in substrings:
        substring = (substring or "").strip()
        if not substring:
            continue
        if substring not in chunk.text:
            # Hallucinated/non-verbatim sub-range — drop just this one
            # entry (per spec), not the whole candidate; the "relevant:
            # true" verdict and any OTHER valid sub-range still stand.
            logger.warning(
                f"Chunk {chunk.id}: an answer_substrings entry was not found "
                f"verbatim in chunk text, dropping that entry: {substring!r}"
            )
            continue
        located = _locate_substring_in_word_timestamps(substring, chunk.word_timestamps or [])
        if located:
            ranges.append(located)

    if not ranges:
        # Either the model returned nothing usable or every entry was
        # hallucinated/unlocatable — fail soft to the whole chunk's own
        # boundaries (per spec), but the "relevant: true" verdict stands;
        # this candidate is NOT rejected.
        ranges = [(chunk.start_sec, chunk.end_sec)]
    else:
        ranges = _merge_adjacent_ranges(ranges)

    return VerifiedChunk(
        chunk=chunk,
        answer_ranges=ranges,
        covered_clause_indices=covered,
    )


def _topics_overlap_or_similar(a: TranscriptChunk, b: TranscriptChunk) -> bool:
    """Guards boundary expansion against drifting into an unrelated
    neighbor. Conservative when there's no signal either way (no shared
    topic_tags AND no embeddings on either side) — refuses to expand
    rather than guessing continuity we can't confirm."""
    a_topics = {t.strip().lower() for t in (a.topic_tags or [])}
    b_topics = {t.strip().lower() for t in (b.topic_tags or [])}
    if a_topics and b_topics:
        return bool(a_topics & b_topics)
    if a.embedding and b.embedding:
        return embeddings.cosine_similarity(a.embedding, b.embedding) >= TOPIC_DRIFT_EMBEDDING_THRESHOLD
    return False


async def _load_segment_chunks(raw_segment_id: str) -> List[TranscriptChunk]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TranscriptChunk)
            .where(TranscriptChunk.raw_segment_id == raw_segment_id)
            .order_by(TranscriptChunk.sequence_index)
        )
        return list(result.scalars().all())


async def _expand_chunk_boundaries(verified: VerifiedChunk) -> List[ExpandedClip]:
    """Walks outward from the OUTERMOST edges of the pinpointed sub-range(s)
    (not the chunk's own full boundaries) to neighboring chunks via
    sequence_index, extending while under MAX_CLIP_DURATION_SEC, no topic
    drift, and no silence gap over MAX_SILENCE_GAP_SEC — in each direction
    independently, so a drift/gap stopping backward expansion doesn't also
    stop forward expansion.

    A chunk can now carry MULTIPLE non-contiguous relevant sub-ranges
    (relevance-per-sub-topic, not a duration split — see
    _VERIFY_PINPOINT_SYSTEM_PROMPT_TEMPLATE). Neighbor-expansion only ever
    extends the FIRST range's start and the LAST range's end; any gap
    BETWEEN sub-ranges within this same chunk is a deliberate exclusion
    (an irrelevant sub-topic sitting between two relevant ones) and must
    never be bridged — only skipped, trimming each sub-range as its own
    piece and concatenating in order."""
    chunk = verified.chunk
    siblings = await _load_segment_chunks(chunk.raw_segment_id)
    by_index = {c.sequence_index: c for c in siblings}

    ranges = list(verified.answer_ranges)
    first_start = ranges[0][0]
    last_end = ranges[-1][1]

    start_sec = first_start
    anchor = chunk
    idx = chunk.sequence_index - 1
    while idx in by_index:
        neighbor = by_index[idx]
        if last_end - neighbor.start_sec > MAX_CLIP_DURATION_SEC:
            break
        if start_sec - neighbor.end_sec > MAX_SILENCE_GAP_SEC:
            break
        if not _topics_overlap_or_similar(anchor, neighbor):
            break
        start_sec = neighbor.start_sec
        anchor = neighbor
        idx -= 1

    end_sec = last_end
    anchor = chunk
    idx = chunk.sequence_index + 1
    while idx in by_index:
        neighbor = by_index[idx]
        if neighbor.end_sec - start_sec > MAX_CLIP_DURATION_SEC:
            break
        if neighbor.start_sec - end_sec > MAX_SILENCE_GAP_SEC:
            break
        if not _topics_overlap_or_similar(anchor, neighbor):
            break
        end_sec = neighbor.end_sec
        anchor = neighbor
        idx += 1

    # Extend only the outer edges; any middle sub-ranges (3+ total) are
    # kept exactly as pinpointed — their surrounding gaps are the whole
    # point of this feature, not something to fill back in.
    pieces = list(ranges)
    pieces[0] = (start_sec, pieces[0][1])
    pieces[-1] = (pieces[-1][0], end_sec)

    return [
        ExpandedClip(
            raw_segment_id=chunk.raw_segment_id,
            start_sec=s,
            end_sec=e,
            source_chunk_id=chunk.id,
        )
        for s, e in pieces
    ]


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
    the whole point of a word-timestamp-precise pinpointed answer."""
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


async def assemble_video_clip_response(
    question: str, group_id: str, recording_language: str, session_id: str
) -> VideoClipResult:
    candidates = await retrieval_service.retrieve_chunks(
        question, group_id, recording_language, session_id
    )
    if not candidates:
        return VideoClipResult(video_url=None, no_story=True, fallback_text=NO_STORY_FALLBACK)

    clauses = await _split_question_into_clauses(question, recording_language)

    verified: List[VerifiedChunk] = []
    for chunk in candidates:
        result = await _verify_and_pinpoint_chunk(chunk, question, clauses)
        if result is not None:
            verified.append(result)

    if not verified:
        return VideoClipResult(video_url=None, no_story=True, fallback_text=NO_STORY_FALLBACK)

    # Clause coverage bookkeeping — logging/QA only, never invented or
    # forced (same never-invent principle as NO_STORY_FALLBACK itself).
    covered_indices = {i for v in verified for i in v.covered_clause_indices}
    uncovered = [clauses[i] for i in range(len(clauses)) if i not in covered_indices]
    if uncovered:
        logger.info(f"Question clauses with no verified chunk coverage: {uncovered}")

    # Each verified chunk can now expand into MULTIPLE pieces (relevance-
    # per-sub-topic splitting, not a duration split) — flatten rather than
    # a 1:1 list.
    expanded: List[ExpandedClip] = []
    for v in verified:
        expanded.extend(await _expand_chunk_boundaries(v))

    cache_key = _clip_cache_key(group_id, expanded)
    cached_url = await cache_service.get(cache_key)
    if cached_url:
        return VideoClipResult(video_url=cached_url, uncovered_clauses=uncovered)

    video_url = await _assemble_and_upload_clip(expanded, group_id, session_id)
    if video_url is None:
        return VideoClipResult(video_url=None, no_story=True, fallback_text=NO_STORY_FALLBACK)

    await cache_service.set(cache_key, video_url, ttl=CACHE_TTL_SECONDS)
    await cache_service.add_visited(session_id, list({c.raw_segment_id for c in expanded}))

    return VideoClipResult(video_url=video_url, uncovered_clauses=uncovered)
