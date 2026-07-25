"""
Full-archive reading — an EXPERIMENTAL third chat mode (Prompt 15,
"video_clips_v2"), built ALONGSIDE the two existing modes (avatar,
video_clips), not replacing either. It is a straight A/B alternative to
the Prompt 11-14 chunk-retrieval video-clip pipeline: same input
(question + producer archive + conversation), same output shape (a single
assembled clip URL, or NO_STORY_FALLBACK), but a completely different
"which ranges answer this question" decision.

Where video_clip_assembler.py runs a multi-step retrieval chain
(coreference resolution -> perspective normalization -> three-signal chunk
matching -> leniency retry -> per-candidate verify/pinpoint), THIS mode
replaces all of it with ONE LLM call that READS the entire annotated
archive transcript (plus an entity map and the recent conversation) and
directly returns the answering time ranges. Deterministic, no-LLM
validation then snaps/rejects those ranges against the real word
timestamps before they reach the EXISTING ffmpeg trim/concat + caching +
storage code (reused wholesale from video_clip_assembler — only the
range-selection step is new here).

The model NEVER writes answer text — it only points at real ranges that
already contain the answer, so the "never invent, only replay verbatim
recorded speech" guarantee the whole feature rests on is preserved: an
invented time range that doesn't line up with actual transcribed words is
dropped by step 4's validation, and if nothing survives we return the same
NO_STORY_FALLBACK as every other mode.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import InterviewSession, RawSegment, TranscriptChunk
from app.services import graph_memory, retrieval_service, video_clip_assembler
from app.services.cache import cache_service
from app.services.llm import llm_service
from app.services.response_assembler import NO_STORY_FALLBACK
from app.services.video_clip_assembler import (
    CACHE_TTL_SECONDS,
    ExpandedClip,
    VideoClipResult,
)

logger = logging.getLogger(__name__)


@dataclass
class ArchiveSegment:
    """One 'ready' recording plus its chunks in chronological order — the
    unit the annotated transcript and range validation both work over."""

    segment: RawSegment
    chunks: List[TranscriptChunk]


# ── Step 3: the one LLM call's prompt ────────────────────────────────────────
#
# PROMPT-CACHING ORDERING (intentional — do not reorder): the LLM request is
# assembled as a STATIC system prompt (these instructions + the full archive
# transcript + the entity map, all identical across every question for a
# given producer until the archive itself changes) followed by a VARIABLE
# user message (the recent conversation + this question). Providers cache on
# a stable prefix, so keeping the large static archive first (in `system`)
# and the small variable question last (in the user turn) maximizes
# prompt-cache hits across successive questions in a conversation. Anthropic
# marks the system block with cache_control automatically in llm.py; Gemini
# caches implicitly on the same prefix.
_ARCHIVE_READER_SYSTEM_PROMPT_TEMPLATE = """\
You are a precise archival video editor for one person's recorded \
life-story archive. You are given the COMPLETE transcript of that \
archive, then an entity map. Each recording is a SEGMENT with a stable \
id and the interview question it answered; within a segment, every line \
is one transcribed phrase prefixed by its time range in seconds within \
that segment's own video: [START-END] phrase text. Most phrase lines are \
followed by an indented "word timings:" line giving EVERY word's exact \
time as word:START-END (e.g. "צבי:14.98-15.26 ואילנה:15.26-16.64").

Your ONLY job: given the user's question (and the recent conversation for \
context), return the exact time ranges from these recordings whose spoken \
words already answer the question, so a separate system can cut and stitch \
the REAL video. You never write, summarize, translate, or paraphrase an \
answer yourself - you only point at real ranges that already contain it.

CRITICAL - narrow to the relevant sub-topic(s), never the surrounding \
passage: a single phrase often runs SEVERAL DISTINCT SUB-TOPICS together \
with no pause between them (e.g. "I grew up in Tiberias, I have four \
siblings Nir Chen Adi and Raz, my parents are Zvi and Ilana, then I went \
away to boarding school" packs childhood-town, then siblings, then \
parents, then schooling into one span, with nothing marking the shifts). \
Identify the distinct sub-topics by where the SUBJECT MATTER genuinely \
changes - never by a length or duration threshold - then judge EACH \
sub-topic's relevance to the question on its own. A sub-topic counts only \
if it genuinely answers the question, NOT because it is adjacent to one \
that does. Return the time range of ONLY the relevant sub-topic(s):
- Set start_sec to the START time of the FIRST word of the relevant \
sub-topic, and end_sec to the END time of its LAST word, read DIRECTLY \
from the "word timings:" line - do not estimate, round, or interpolate \
when word timings are present. Find the exact words that carry the answer \
(e.g. for "who is Zvi?", the words naming Zvi) and use their times. Cover \
the WHOLE relevant sub-topic - do not clip its lead-in or trailing words - \
but stop where it ends and the next, irrelevant sub-topic begins. (Only if \
a phrase has NO word-timings line, fall back to estimating from its \
[START-END] span by proportional position.)
- NEVER include an irrelevant sub-topic's time, even when it sits BETWEEN \
two relevant ones: return the two relevant sub-topics as two SEPARATE \
ranges, not one range spanning the irrelevant middle.
- If EVERY sub-topic in a phrase is relevant (a genuinely broad question \
can legitimately need the whole phrase), returning the whole [START-END] \
span is correct. The length must always fall out of how much is actually \
relevant - never trimmed to shorten it, never padded to lengthen it.

Rules:
- Output ONLY a JSON array, nothing else. Each element is exactly \
{{"segment_id": "<id>", "start_sec": <number>, "end_sec": <number>}}.
- Use ONLY segment ids and times that literally appear below. For every \
range, start_sec < end_sec, and both must fall within the time markers \
shown for that segment.
- Multiple ranges are allowed, including across DIFFERENT segments, listed \
in the order they should be played and stitched together.
- Resolve pronouns and follow-ups ("did you love her?", "is he still \
alive?") using the recent conversation - the archive is first-person, but \
the question may be second- or third-person about the storyteller.
- If NOTHING in the archive answers the question, output an empty array: \
[]. Never invent, force, or approximate a range to avoid an empty answer.

FULL ARCHIVE TRANSCRIPT:
{transcript_block}

ENTITY MAP (entity name -> segment ids that mention it):
{entity_map_block}"""


# ── Step 1: load + format the annotated transcript ──────────────────────────


async def _load_archive(group_id: str) -> List[ArchiveSegment]:
    """Every 'ready' segment in this producer's archive (chronological by
    created_at) with its chunks ordered by sequence_index. Same scoping
    join retrieval_service._load_ready_chunks uses, just grouped/ordered
    for a whole-archive read instead of a flat candidate list.

    Deliberately UNCAPPED: the whole point of this mode is that the model
    reads the entire archive at once. ~1-2 hours of recording per producer
    is only ~35-40K tokens, which fits comfortably in context, so no
    filtering/windowing is applied.
    TODO: if a real producer's archive ever exceeds ~150K tokens, this is
    where a COARSE pre-filter would go (e.g. embedding-rank segments and
    read only the top-K) — do NOT build that speculatively; add it only
    when an actual archive hits that size."""
    async with AsyncSessionLocal() as db:
        seg_result = await db.execute(
            select(RawSegment)
            .join(InterviewSession, RawSegment.interview_session_id == InterviewSession.id)
            .where(InterviewSession.user_id == group_id, RawSegment.status == "ready")
            .order_by(RawSegment.created_at)
        )
        segments = list(seg_result.scalars().all())
        if not segments:
            return []

        chunk_result = await db.execute(
            select(TranscriptChunk)
            .where(TranscriptChunk.raw_segment_id.in_([s.id for s in segments]))
            .order_by(TranscriptChunk.raw_segment_id, TranscriptChunk.sequence_index)
        )
        chunks_by_segment: Dict[str, List[TranscriptChunk]] = {}
        for chunk in chunk_result.scalars().all():
            chunks_by_segment.setdefault(chunk.raw_segment_id, []).append(chunk)

    archive = [
        ArchiveSegment(segment=seg, chunks=chunks_by_segment.get(seg.id, []))
        for seg in segments
    ]
    # A segment with no chunks yet contributes nothing readable — skip it
    # rather than emitting an empty, confusing block.
    return [a for a in archive if a.chunks]


def _format_word_timings(word_timestamps: Optional[List[dict]]) -> str:
    """Compact `word:START-END` list from Prompt 11's word_timestamps, so the
    model can pin a range to a specific word's EXACT time instead of
    estimating its position within a long phrase (the fix for v2 landing on
    the wrong sub-topic for mid-phrase entities like Ilana/Tzvi). Empty
    string when a chunk has no word timing (nullable) — the caller then
    omits the line and the model falls back to the phrase [START-END]."""
    if not word_timestamps:
        return ""
    parts: List[str] = []
    for w in word_timestamps:
        try:
            word = str(w["word"]).strip()
            s = float(w["start_sec"])
            e = float(w["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if word:
            parts.append(f"{word}:{s:.2f}-{e:.2f}")
    return " ".join(parts)


def _format_annotated_transcript(archive: List[ArchiveSegment]) -> str:
    lines: List[str] = []
    for item in archive:
        seg = item.segment
        lines.append(f"[segment {seg.id}] Interview question: {seg.question_asked}")
        for chunk in item.chunks:
            lines.append(f"  [{chunk.start_sec:.2f}-{chunk.end_sec:.2f}] {chunk.text}")
            word_timings = _format_word_timings(chunk.word_timestamps)
            if word_timings:
                lines.append(f"    word timings: {word_timings}")
        lines.append("")  # blank line between segments
    return "\n".join(lines).rstrip()


# ── Step 2: entity map from Graphiti (read-only) ────────────────────────────


async def _build_entity_map(archive: List[ArchiveSegment], group_id: str) -> Dict[str, List[str]]:
    """entity name -> the segment ids that mention it. Built by inverting
    graph_memory.get_episode_entity_names per segment (the same read-only
    graph access expand_graph_chunks already uses — no graph changes).
    Fail-soft per segment: a Graphiti hiccup on one segment just omits its
    entities rather than failing the whole read; the entity map is an aid
    to the model, never a hard dependency."""
    entity_to_segments: Dict[str, List[str]] = {}
    for item in archive:
        seg_id = item.segment.id
        try:
            names = await graph_memory.get_episode_entity_names(seg_id, group_id=group_id)
        except Exception as e:
            logger.warning(f"Entity lookup failed for segment {seg_id}, omitting from map: {e}")
            continue
        for name in names:
            entity_to_segments.setdefault(name, [])
            if seg_id not in entity_to_segments[name]:
                entity_to_segments[name].append(seg_id)
    return entity_to_segments


def _format_entity_map(entity_map: Dict[str, List[str]]) -> str:
    if not entity_map:
        return "(none extracted)"
    return "\n".join(
        f"- {name}: {', '.join(seg_ids)}" for name, seg_ids in sorted(entity_map.items())
    )


# ── Step 3: the single range-selection LLM call ─────────────────────────────


def _parse_ranges_json(text: str) -> List[dict]:
    """Extract the JSON array of {segment_id, start_sec, end_sec} objects.
    Skips malformed elements here; step 4's validation is the real gate."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: List[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        seg_id = item.get("segment_id")
        start = item.get("start_sec")
        end = item.get("end_sec")
        if not isinstance(seg_id, str):
            continue
        try:
            out.append({"segment_id": seg_id, "start_sec": float(start), "end_sec": float(end)})
        except (TypeError, ValueError):
            continue
    return out


async def _read_archive_for_ranges(
    question: str,
    transcript_block: str,
    entity_map_block: str,
    history: List[Dict[str, str]],
    recording_language: str,
) -> List[dict]:
    """The ONE LLM call. Fail-soft to [] (an LLM/parse failure yields the
    no-story fallback, never a guessed clip) — same never-invent contract
    as the other modes' own LLM steps.

    See _ARCHIVE_READER_SYSTEM_PROMPT_TEMPLATE's comment for why the static
    transcript/entity-map live in the system prompt and the variable
    conversation/question live in the user message (prompt-cache ordering)."""
    system_prompt = _ARCHIVE_READER_SYSTEM_PROMPT_TEMPLATE.format(
        transcript_block=transcript_block, entity_map_block=entity_map_block
    )

    user_parts: List[str] = []
    if history:
        rendered = "\n".join(
            retrieval_service._render_turn_for_history(t["role"], t["content"]) for t in history
        )
        user_parts.append(f"Recent conversation:\n{rendered}\n")
    user_parts.append(f"Question:\n{question}")
    user_message = "\n".join(user_parts)

    try:
        raw = await llm_service.generate_response(
            messages=[{"role": "user", "content": user_message}],
            system_prompt=system_prompt,
            temperature=0,
        )
    except Exception as e:
        logger.warning(f"Archive-read LLM call failed, treating as no-story: {e}")
        return []
    return _parse_ranges_json(raw)


# ── Step 4: deterministic validation + word-boundary snapping (no LLM) ──────


def _word_intervals_for_segment(chunks: List[TranscriptChunk]) -> List[Tuple[float, float]]:
    """Flat, sorted list of (start_sec, end_sec) speech intervals for a
    segment — one per word when word_timestamps exist, falling back to the
    whole-chunk boundary when a chunk has no word-level timing (Prompt 11's
    word_timestamps is nullable). These are the boundaries ranges snap to."""
    intervals: List[Tuple[float, float]] = []
    for chunk in chunks:
        words = chunk.word_timestamps or []
        chunk_words: List[Tuple[float, float]] = []
        for w in words:
            try:
                ws = float(w["start_sec"])
                we = float(w["end_sec"])
            except (KeyError, TypeError, ValueError):
                continue
            if we > ws:
                chunk_words.append((ws, we))
        if chunk_words:
            intervals.extend(chunk_words)
        else:
            # Phrase-level boundary fallback (still a real transcribed span).
            if chunk.end_sec > chunk.start_sec:
                intervals.append((chunk.start_sec, chunk.end_sec))
    intervals.sort()
    return intervals


def _snap_range_to_words(
    start: float, end: float, intervals: List[Tuple[float, float]]
) -> Optional[Tuple[float, float]]:
    """Snap a model-proposed [start, end] to real word/phrase boundaries:
    the range is kept only if it OVERLAPS actual transcribed speech, and its
    edges are moved to the start of the first overlapping word and the end
    of the last overlapping word (so a clip never begins or ends mid-word,
    and every returned range provably covers real speech). Returns None if
    the range overlaps no transcribed speech at all — that range is dropped."""
    if end <= start or not intervals:
        return None
    overlapping = [(ws, we) for (ws, we) in intervals if ws < end and we > start]
    if not overlapping:
        return None
    snapped_start = min(ws for ws, _ in overlapping)
    snapped_end = max(we for _, we in overlapping)
    if snapped_end <= snapped_start:
        return None
    return (snapped_start, snapped_end)


def validate_ranges(raw_ranges: List[dict], archive: List[ArchiveSegment]) -> List[ExpandedClip]:
    """Deterministic, no-LLM gate on the model's proposed ranges. Drops any
    range that references a segment not in THIS producer's archive, or that
    doesn't overlap real transcribed speech; snaps survivors to word
    boundaries. Preserves the model's stitch order. Returns ExpandedClip
    objects so the existing video_clip_assembler assembly/caching code can
    consume them unchanged."""
    intervals_by_segment = {
        item.segment.id: _word_intervals_for_segment(item.chunks) for item in archive
    }

    validated: List[ExpandedClip] = []
    for r in raw_ranges:
        seg_id = r["segment_id"]
        intervals = intervals_by_segment.get(seg_id)
        if intervals is None:
            logger.warning(f"Archive-read returned unknown/foreign segment id {seg_id!r}; dropping")
            continue
        snapped = _snap_range_to_words(r["start_sec"], r["end_sec"], intervals)
        if snapped is None:
            logger.warning(
                f"Archive-read range {r['start_sec']}-{r['end_sec']} in {seg_id} "
                f"overlaps no transcribed speech; dropping"
            )
            continue
        start, end = snapped
        validated.append(
            ExpandedClip(
                raw_segment_id=seg_id,
                start_sec=start,
                end_sec=end,
                # No single source chunk in this mode — a range can span
                # several. Marked so it's never mistaken for a v1 chunk id.
                source_chunk_id=f"archive-read:{seg_id}",
            )
        )
    return validated


async def read_and_validate_ranges(
    question: str, group_id: str, recording_language: str, session_id: str
) -> List[ExpandedClip]:
    """Steps 1-4: everything up to (but not including) ffmpeg assembly —
    the actual 'which ranges' decision that differs from v1. Exposed
    separately so the comparison harness (and tests) can inspect the chosen
    ranges without running ffmpeg."""
    archive = await _load_archive(group_id)
    if not archive:
        return []

    transcript_block = _format_annotated_transcript(archive)
    entity_map = await _build_entity_map(archive, group_id)
    entity_map_block = _format_entity_map(entity_map)
    history = await retrieval_service._recent_turns(
        session_id, retrieval_service.COREFERENCE_HISTORY_TURNS
    )

    raw_ranges = await _read_archive_for_ranges(
        question, transcript_block, entity_map_block, history, recording_language
    )
    return validate_ranges(raw_ranges, archive)


# ── Step 5: orchestrate, reusing the existing assembly/caching/storage ──────


async def assemble_video_clip_response_v2(
    question: str, group_id: str, recording_language: str, session_id: str
) -> VideoClipResult:
    """The v2 parallel to video_clip_assembler.assemble_video_clip_response.
    Identical return contract (a clip URL or NO_STORY_FALLBACK) so the WS
    handler and frontend treat both modes the same; only the range decision
    (read_and_validate_ranges) differs. Assembly, caching, and storage are
    the EXACT same code the v1 path uses."""
    clips = await read_and_validate_ranges(question, group_id, recording_language, session_id)
    if not clips:
        return VideoClipResult(video_url=None, no_story=True, fallback_text=NO_STORY_FALLBACK)

    cache_key = video_clip_assembler._clip_cache_key(group_id, clips)
    cached_url = await cache_service.get(cache_key)
    if cached_url:
        return VideoClipResult(video_url=cached_url)

    video_url = await video_clip_assembler._assemble_and_upload_clip(clips, group_id, session_id)
    if video_url is None:
        return VideoClipResult(video_url=None, no_story=True, fallback_text=NO_STORY_FALLBACK)

    await cache_service.set(cache_key, video_url, ttl=CACHE_TTL_SECONDS)
    await cache_service.add_visited(session_id, list({c.raw_segment_id for c in clips}))

    return VideoClipResult(video_url=video_url)
