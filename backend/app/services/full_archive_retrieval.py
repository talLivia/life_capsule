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
directly returns which parts answer the question.

SELECTION IS BY UTTERANCE UNIT, NOT BY TIME. The transcript is first split
into utterance units — uninterrupted stretches of speech cut at the pauses
the speaker actually took, with the pause threshold derived per-recording
from that recording's own inter-word gap distribution (see _GAP_PERCENTILE)
rather than any fixed constant. The model returns UNIT IDS; the final times
are derived deterministically from the selected units. Because half a unit
cannot be selected, a mid-sentence/mid-thought cut is structurally
impossible — which no amount of prompt wording could guarantee back when
the model emitted raw {start_sec, end_sec} pairs. Units are numbered across
the WHOLE archive, so one answer can freely draw on several recordings.
Resolved ranges then reach the EXISTING ffmpeg trim/concat + caching +
storage code (reused wholesale from video_clip_assembler — only the
selection step is new here).

The model NEVER writes answer text — it only points at real ranges that
already contain the answer, so the "never invent, only replay verbatim
recorded speech" guarantee the whole feature rests on is preserved: an
invented time range that doesn't line up with actual transcribed words is
dropped by step 4's validation, and if nothing survives we return the same
NO_STORY_FALLBACK as every other mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import InterviewSession, Message, RawSegment, TranscriptChunk
from app.services import entity_store, retrieval_service, video_clip_assembler
from app.services.cache import cache_service
from app.services.llm import llm_service
from app.services.response_assembler import NO_STORY_FALLBACK
from app.services.video_clip_assembler import (
    CACHE_TTL_SECONDS,
    ExpandedClip,
    VideoClipResult,
)

logger = logging.getLogger(__name__)

# NOTE: there is deliberately no "I already told you that" response. A
# standalone question ALWAYS gets a real answer, even when the units that
# answer it were played earlier in the conversation. As an archive grows
# people naturally revisit the same subject from different angles ("tell me
# about your family", then "who is your mother?"), and each angle deserves
# its own answer — refusing one because the footage overlaps something shown
# earlier reads as the system being broken. Repetition ACROSS different
# questions is expected and fine; the only place already-shown material is
# actually withheld is the follow-up SUGGESTION (see _validate_follow_up),
# where the whole point is to offer something MORE.


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
archive, then an entity map.

The archive is a series of RECORDINGS. Each recording is the person's \
answer to ONE interview question from a fixed life-story interview \
(childhood, military service, relationships, career, family). That \
interview question is shown above each recording and is ESSENTIAL \
CONTEXT, not a label: it tells you what the recording is ABOUT, and \
therefore what the words inside it refer to.

A question may have been answered in SEVERAL recordings, marked "(take N \
of M of this question)" and shown one after another. Those takes are ONE \
answer given in more than one sitting - the person came back and added \
what they had left out, or told it again more fully - NOT separate \
subjects. Read them together: the interview question applies to all of \
them equally, so an unnamed "she" or "my commander" in a later take means \
the same person it means in the first. When several takes answer the \
question, select units from whichever of them actually answer it, in the \
order they should be played.

USE THE INTERVIEW QUESTION TO UNDERSTAND WHO OR WHAT A UNIT IS ABOUT, \
especially when the spoken words alone do not name them. People are \
constantly referred to without being named - "the right person", "my \
commander", "my best friend", "she", "him". The interview question is \
usually the only thing that identifies them, so read every unit in the \
light of the question its recording answered. For example, a unit saying \
only "I just knew this was the right person", inside a recording whose \
interview question was "tell me about the moment you knew you'd found the \
right person", is about the storyteller's SPOUSE/PARTNER - and it \
therefore answers questions about their wife/husband/partner even though \
the words "wife", "husband" or her/his name never appear in it. Treat \
such a unit as a match. Never require the question's own words to appear \
literally in the unit.

The transcript is split into UTTERANCE UNITS. A unit is one uninterrupted \
stretch of speech, cut at the natural pauses the speaker actually took. \
Each unit has an id (u1, u2, ...) unique across the WHOLE archive, and \
its exact time range inside its recording's video:
  u7 [12.30-18.90] the words spoken in that unit

Your ONLY job: given the user's question (and the recent conversation for \
context), choose WHICH UNITS to play, so a separate system can cut and \
stitch the REAL video from them. You never write, summarize, translate, or \
paraphrase an answer yourself - you only point at units that already \
contain it.

HOW TO CHOOSE UNITS - this is the core of the task:
- Judge relevance ONE WHOLE UNIT AT A TIME: either a unit belongs in the \
answer or it does not. NEVER select part of a unit, and never try to \
exclude a clause inside a unit that you consider less relevant. A unit is \
the smallest thing you can pick. If a unit is mostly relevant, take it \
whole.
- Take the COMPLETE THOUGHT, not just the words that literally match the \
question. If an included unit's thought continues or is completed in the \
units that follow it - a consequence, an outcome, a "and then...", a \
"and when I came back...", an explanation of what was just said - those \
continuation units are PART OF THE ANSWER and must be included too. \
Stopping while the thought is still unfinished produces an answer that \
cuts off mid-story, which is always wrong.
- Skip a unit only when it genuinely moves on to a DIFFERENT subject that \
the question did not ask about. A unit does not qualify merely by sitting \
next to a relevant one, but a unit that finishes the relevant thought does \
qualify.
- Units that are NOT consecutive are fine: if two separated passages both \
answer the question, select both and simply omit the unrelated units \
between them. Selecting units from SEVERAL DIFFERENT recordings in one \
answer is expected and encouraged whenever more than one recording speaks \
to the question.
- Let the question set the breadth by itself. A narrow question ("what \
unit did you serve in?") is answered by very few units; a broad question \
("tell me about your time in the army") is answered by many. Never trim to \
make an answer shorter, and never pad to make it longer - include exactly \
the units that genuinely answer what was asked.

ALREADY SHOWN: units marked [ALREADY SHOWN] were played earlier in THIS \
conversation. This mark is ONLY a tie-breaker about which units to prefer. \
It NEVER makes a question unanswerable:
- ALWAYS answer the question fully. If the units that best answer it are \
already shown, select them anyway - a fresh question deserves a real \
answer, and people naturally revisit the same subject from different \
angles ("tell me about your family", then "who is your mother?"). Each \
angle gets its own answer; repeating footage across DIFFERENT questions is \
expected and correct.
- When an unseen unit and an already-shown unit answer the question equally \
well, prefer the unseen one. That is all this mark is for.
- A marked unit does NOT mean its topic is finished. For a follow-up like \
"tell me something else about her", look for OTHER units on that same \
subject - very often in a DIFFERENT recording, whose interview question \
approaches the same person or period from another angle - rather than \
concluding it was covered.
- Returning an empty selection is correct ONLY when no unit in the archive \
answers the question at all.

FOLLOW-UP SUGGESTION (optional): after choosing the answer, check whether \
the archive holds OTHER material genuinely related to what you just \
answered - the same person, place, or period - in units you did NOT \
include and that are not marked [ALREADY SHOWN]. If it does, add a \
"follow_up": a SHORT, natural question offering to continue (in the same \
language as the storyteller's transcript), plus the unit ids that would \
answer it. For example, after answering how they met their partner, the \
recording about the moment they knew they'd found the right person is \
exactly such material.
- Word it in the STORYTELLER'S OWN VOICE, speaking to the listener: it \
offers to tell them something of THEIRS. So "want to hear about the moment \
I knew?" - first person for the storyteller ("my parents", "I knew"), \
never third person about them ("his parents") and never second person as \
if addressing them ("what you discovered").
- Another TAKE of the same interview question is a legitimate place to \
find such material: a later take often adds something the first one left \
out, and those units are unseen. But offer it only when it genuinely says \
something NEW. If a later take just retells what you already showed, in \
other words, that is not a follow-up - offering to repeat a story as \
though it were more would be worse than offering nothing.
- The suggestion must be answerable by REAL units you list - never offer \
something the archive cannot actually show.
- Its unit ids must not overlap the answer's own unit_ids, and must not be \
already shown.
- If there is no such material, omit "follow_up" or set it to null. No \
suggestion is perfectly fine - never invent one to seem helpful.

Rules:
- Output ONLY a JSON object, nothing else, exactly: \
{{"unit_ids": ["u3", "u4", ...], "follow_up": {{"question": "...", \
"unit_ids": ["u9"]}}}}  (or {{"unit_ids": [...], "follow_up": null}})
- Use ONLY unit ids of the form u<number> that literally appear below, \
listed in the order they should be played and stitched together. NEVER \
output a recording number, an interview question, a time, or any other \
kind of id - unit ids only.
- Resolve pronouns and follow-ups ("did you love her?", "is he still \
alive?", "anything else about her?") using the recent conversation, which \
shows which units were already played and what they said - the referent is \
usually the PERSON discussed in the previous answer. Prefer a person over \
an inanimate reading whenever the previous turn was about a person. The \
archive is first-person, but the question may be second- or third-person \
about the storyteller.
- If NOTHING in the archive answers the question, output \
{{"unit_ids": []}}. Never invent, force, or approximate a selection to \
avoid an empty answer.

FULL ARCHIVE TRANSCRIPT:
{transcript_block}

ENTITY MAP (entity name -> the RECORDINGS that mention it):
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
    return _group_siblings([a for a in archive if a.chunks])


def _group_siblings(archive: List[ArchiveSegment]) -> List[ArchiveSegment]:
    """Put takes of the same interview question next to each other.

    A question can hold several recordings, and `created_at` alone does not
    place them together: answering Q1, then Q2, then going back to add a
    second take to Q1 leaves the two Q1 takes separated by Q2 in the
    transcript. The model would then read one interview question twice, in
    two places, with no signal they belong together — the exact context the
    prompt leans on hardest to work out who an unnamed "she" or "my
    commander" is.

    STABLE: groups are ordered by their earliest recording and takes within
    a group stay in recording order, so an archive WITHOUT siblings comes out
    in exactly the order it went in. That is deliberate — it keeps this from
    silently re-cutting existing archives, where the eval baseline lives.
    """
    order: List[int] = []
    by_question: Dict[int, List[ArchiveSegment]] = {}
    for item in archive:
        q = item.segment.question_index
        if q not in by_question:
            by_question[q] = []
            order.append(q)
        by_question[q].append(item)
    return [item for q in order for item in by_question[q]]


# ── Utterance units: the unit of SELECTION ──────────────────────────────────
#
# The model selects whole utterance units by id, never raw times. That makes a
# mid-sentence cut STRUCTURALLY impossible (half a unit isn't selectable),
# which is what a purely time-based selection could never guarantee no matter
# how the prompt was worded — the previous prompt's "stop where the relevant
# sub-topic ends" actively produced mid-thought cuts.
#
# Units are cut at the pauses the speaker actually took. The pause threshold is
# NOT a hardcoded number: it's a high percentile of THIS recording's own
# inter-word gap distribution, so it adapts to how fast or slowly a given
# person speaks (and to a given recording's pacing) instead of imposing one
# constant on everyone. The percentile itself is the single general knob —
# there is deliberately no per-question, per-length, or per-topic rule
# anywhere in this file.
_GAP_PERCENTILE = 90.0


@dataclass
class UtteranceUnit:
    """One uninterrupted stretch of speech — the atom the model selects.

    `index` is GLOBAL across the whole archive (not per segment) so units from
    different recordings share one namespace and the model can select across
    recordings in a single answer; it also makes "are these two units
    consecutive?" a plain integer check when merging."""

    unit_id: str
    segment_id: str
    index: int
    start_sec: float
    end_sec: float
    text: str


def _percentile(values: List[float], pct: float) -> float:
    """Linear-interpolated percentile. Small local helper rather than a numpy
    dependency — this runs over one recording's word gaps, not a big array."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def _chunk_words(chunk: TranscriptChunk) -> List[Tuple[str, float, float]]:
    """(word, start, end) triples for a chunk, dropping entries with missing
    or non-monotonic timing. Empty when the chunk has no word timing at all
    (Prompt 11's word_timestamps is nullable)."""
    out: List[Tuple[str, float, float]] = []
    for w in chunk.word_timestamps or []:
        try:
            word = str(w["word"]).strip()
            start = float(w["start_sec"])
            end = float(w["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if word and end > start:
            out.append((word, start, end))
    return out


def _segment_gap_threshold(chunks: List[TranscriptChunk]) -> Optional[float]:
    """The pause length that separates two utterances IN THIS RECORDING,
    derived from the recording's own gap distribution.

    Only gaps between consecutive words WITHIN a chunk are measured — a chunk
    boundary is a transcription-level break, not a measured pause, so folding
    it in would pollute the distribution. Returns None when there aren't
    enough gaps to characterise a distribution, in which case the caller does
    not split inside chunks at all (rather than splitting on a number derived
    from one or two samples)."""
    gaps: List[float] = []
    for chunk in chunks:
        words = _chunk_words(chunk)
        for prev, nxt in zip(words, words[1:]):
            # Clamp: overlapping word timings would otherwise contribute
            # negative "gaps" and drag the percentile down.
            gaps.append(max(0.0, nxt[1] - prev[2]))
    if len(gaps) < 2:
        return None
    return _percentile(gaps, _GAP_PERCENTILE)


def _split_segment_into_units(
    item: ArchiveSegment, next_index: int
) -> Tuple[List[UtteranceUnit], int]:
    """Split one recording into utterance units, continuing the archive-wide
    unit numbering from `next_index`.

    A new unit starts at a chunk boundary (a separately transcribed phrase) or
    wherever the speaker paused longer than this recording's own threshold.
    A chunk with no word timing contributes exactly one unit spanning the
    whole chunk — still a real transcribed span, just not sub-divisible."""
    threshold = _segment_gap_threshold(item.chunks)
    units: List[UtteranceUnit] = []
    index = next_index

    def _emit(words: List[Tuple[str, float, float]]) -> None:
        nonlocal index
        if not words:
            return
        units.append(
            UtteranceUnit(
                unit_id=f"u{index}",
                segment_id=item.segment.id,
                index=index,
                start_sec=words[0][1],
                end_sec=words[-1][2],
                text=" ".join(w for w, _, _ in words),
            )
        )
        index += 1

    for chunk in item.chunks:
        words = _chunk_words(chunk)
        if not words:
            if chunk.end_sec > chunk.start_sec and (chunk.text or "").strip():
                units.append(
                    UtteranceUnit(
                        unit_id=f"u{index}",
                        segment_id=item.segment.id,
                        index=index,
                        start_sec=chunk.start_sec,
                        end_sec=chunk.end_sec,
                        text=chunk.text.strip(),
                    )
                )
                index += 1
            continue

        current: List[Tuple[str, float, float]] = [words[0]]
        for prev, nxt in zip(words, words[1:]):
            gap = max(0.0, nxt[1] - prev[2])
            if threshold is not None and gap > threshold:
                _emit(current)
                current = [nxt]
            else:
                current.append(nxt)
        _emit(current)

    _clamp_overlaps(units)
    return units, index


def _clamp_overlaps(units: List[UtteranceUnit]) -> None:
    """Stop consecutive units in a recording from overlapping in time.

    CONFIRMED IN REAL DATA: Deepgram sometimes gives the last word of an
    utterance an end time that runs past the first word of the next one — in
    this archive "ורז" ended at 14.64 while "להורים" already started at 13.82,
    a 0.81s overlap. A word cannot still be sounding once the next has begun,
    so the earlier END is the wrong number, and left alone it makes a stitched
    answer replay the same fraction of a second twice.

    Trims the EARLIER unit's end rather than pushing the later unit's start:
    that can only ever shorten a range, never invent speech that wasn't
    claimed. A unit that would collapse to nothing is left untouched — better
    a small overlap than a zero-length range that validation would drop."""
    for earlier, later in zip(units, units[1:]):
        if earlier.end_sec > later.start_sec > earlier.start_sec:
            logger.debug(
                f"Trimming overlapping unit {earlier.unit_id} "
                f"{earlier.end_sec:.2f} -> {later.start_sec:.2f}"
            )
            earlier.end_sec = later.start_sec


def _build_units(archive: List[ArchiveSegment]) -> List[UtteranceUnit]:
    """Every utterance unit in the archive, numbered sequentially ACROSS all
    recordings (see UtteranceUnit.index) and in archive order."""
    units: List[UtteranceUnit] = []
    next_index = 1
    for item in archive:
        seg_units, next_index = _split_segment_into_units(item, next_index)
        units.extend(seg_units)
    return units


def _unit_key(segment_id: str, start_sec: float) -> str:
    """Stable identity for a unit ACROSS reads. Unit ids (u1, u2, …) are
    positional — ingesting a new recording renumbers everything — so anything
    persisted between turns (the shown-units history) must key on the
    recording + the unit's own start time, which come from real word
    timestamps and don't move."""
    return f"{segment_id}:{start_sec:.2f}"


def _units_by_segment(units: List[UtteranceUnit]) -> Dict[str, List[UtteranceUnit]]:
    by_segment: Dict[str, List[UtteranceUnit]] = {}
    for unit in units:
        by_segment.setdefault(unit.segment_id, []).append(unit)
    return by_segment


def _recording_ordinals(
    archive: List[ArchiveSegment], units: List[UtteranceUnit]
) -> Dict[str, int]:
    """segment id -> the RECORDING number the prompt prints it as.

    Shared by the transcript block and the entity map so the two cannot
    disagree. They previously used different labelling schemes entirely
    (ordinals vs raw UUIDs), and the entity map's pointers were therefore
    unresolvable; deriving both from one function is what stops that
    recurring.

    A recording whose units were all filtered out is not printed and gets no
    ordinal — so anything referring to it must omit the reference rather than
    invent a number for a heading that is not there.
    """
    by_segment = _units_by_segment(units)
    printed = [item for item in archive if by_segment.get(item.segment.id)]
    return {item.segment.id: i for i, item in enumerate(printed, start=1)}


def _format_annotated_transcript(
    archive: List[ArchiveSegment],
    units: List[UtteranceUnit],
    shown_keys: Optional[set] = None,
) -> str:
    """The archive as numbered utterance units grouped under their recording.

    Segment UUIDs are deliberately NOT printed: they were the only other
    id-shaped thing in the prompt, and the model would sometimes return one
    where a unit id was asked for — which validation then dropped, silently
    turning an answerable question into a no-story. Recordings are labelled
    with a plain ordinal instead, so unit ids are the only ids in view.

    Word-level timings are likewise not shown: the model no longer picks
    times, so exposing them would only invite time-arithmetic back in.

    Units already played in this conversation are marked so the model can
    prefer unseen material on a follow-up (see the prompt's ALREADY SHOWN
    section)."""
    shown_keys = shown_keys or set()
    by_segment = _units_by_segment(units)

    # Takes of the same interview question, so a repeated question reads as
    # one answer in several parts rather than as two unrelated recordings
    # that happen to share a heading. Only counts recordings that will
    # actually be printed — a sibling whose units were all filtered out is
    # not a "take 1 of 2" the model can see.
    printed = [item for item in archive if by_segment.get(item.segment.id)]
    takes_per_question: Dict[int, int] = {}
    for item in printed:
        q = item.segment.question_index
        takes_per_question[q] = takes_per_question.get(q, 0) + 1
    take_number: Dict[int, int] = {}

    lines: List[str] = []
    ordinal = 0
    for item in printed:
        seg = item.segment
        seg_units = by_segment[seg.id]
        ordinal += 1
        # Byte-identical to the old single-recording line when a question has
        # exactly one take — which is every question in an archive recorded
        # before this existed, so nothing about those prompts changes.
        total_takes = takes_per_question[seg.question_index]
        if total_takes > 1:
            n = take_number.get(seg.question_index, 0) + 1
            take_number[seg.question_index] = n
            suffix = f" (take {n} of {total_takes} of this question)"
        else:
            suffix = ""
        lines.append(f"RECORDING {ordinal} — interview question: {seg.question_asked}{suffix}")
        for unit in seg_units:
            mark = (
                " [ALREADY SHOWN]"
                if _unit_key(unit.segment_id, unit.start_sec) in shown_keys
                else ""
            )
            lines.append(
                f"  {unit.unit_id} [{unit.start_sec:.2f}-{unit.end_sec:.2f}]{mark} {unit.text}"
            )
        lines.append("")  # blank line between recordings
    return "\n".join(lines).rstrip()


# ── Shown-unit history: what this conversation has already played ───────────
#
# Persisted on the assistant Message row (message_metadata JSON) rather than
# in the Redis visited-set, for two reasons: it needs UNIT granularity (the
# visited-set is per segment, so showing one unit would blank out a whole
# recording), and it must survive a Redis-less deployment — REDIS_URL isn't
# running in local dev, where cache_service silently no-ops. The DB is the
# same place the conversation itself lives, so the two can never disagree.


async def _load_shown_units(session_id: str) -> Tuple[set, List[List[dict]]]:
    """(all unit keys shown in this session, per-assistant-turn unit lists in
    chronological order). Fail-soft: any DB problem yields empty history
    rather than breaking the turn."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Message)
                .where(Message.session_id == session_id, Message.role == "assistant")
                .order_by(Message.created_at)
            )
            rows = list(result.scalars().all())
    except Exception as e:
        logger.warning(f"Could not load shown-unit history for {session_id}: {e}")
        return set(), []

    per_turn: List[List[dict]] = []
    keys: set = set()
    for row in rows:
        meta = row.message_metadata or {}
        shown = meta.get("shown_units") or []
        clean = [u for u in shown if isinstance(u, dict) and u.get("key")]
        per_turn.append(clean)
        keys.update(u["key"] for u in clean)
    return keys, per_turn


def _format_history_block(
    turns: List[Dict[str, str]], per_turn_units: List[List[dict]]
) -> str:
    """Render the recent conversation so a follow-up has a real antecedent.

    An assistant turn used to render as "(showed a video clip)" — the model
    knew *a* clip played but not what it said, so a pronoun like "her" had
    nothing to bind to. Now each assistant turn names the units it played and
    quotes their words.

    The unit lists are aligned to the assistant turns from the END (both are
    chronological, and only turns that actually produced a clip persist an
    assistant row). If they can't be aligned, the neutral placeholder is used
    for the unmatched ones rather than risking attaching the wrong text."""
    assistant_count = sum(1 for t in turns if t["role"] == "assistant")
    aligned = per_turn_units[-assistant_count:] if assistant_count else []
    if len(aligned) < assistant_count:
        aligned = [[]] * (assistant_count - len(aligned)) + aligned

    lines: List[str] = []
    seen_assistant = 0
    for turn in turns:
        role, content = turn["role"], turn["content"]
        if role != "assistant":
            lines.append(f"{role}: {content}")
            continue
        units = aligned[seen_assistant] if seen_assistant < len(aligned) else []
        seen_assistant += 1
        if units:
            rendered = "; ".join(
                f"{u.get('unit_id', '?')}: \"{u.get('text', '')}\"" for u in units
            )
            lines.append(f"assistant: (played {rendered})")
        elif content and not content.startswith(("http://", "https://")):
            # No per-unit record (a v1 turn, or one persisted before units were
            # tracked) but the message body is the readable spoken text — same
            # data, just without the unit ids. Still far better than a
            # placeholder, which leaves a follow-up pronoun with no antecedent.
            lines.append(f"assistant: {content}")
        else:
            lines.append("assistant: (showed a video clip)")
    return "\n".join(lines)


# ── Step 2: entity map from Postgres ───────────────────────────────────────


# ── per-producer archive cache ──────────────────────────────────────────────
#
# The archive load, the entity map and the unit split are all derived PURELY
# from the producer's ready recordings, which change only at ingestion — yet
# they were rebuilt on every single question. MEASURED over three identical
# passes: _build_entity_map 4.13s mean (45% of the whole turn) and, worse,
# ALL of the variance — it ranged 1.35s to 9.55s because it makes one Neo4j
# round-trip PER RECORDING against a remote free-tier instance. _load_archive
# added another 1.22s. The LLM call people assume is the bottleneck was 2.99s
# with a 0.42s spread.
#
# Deliberately in-process rather than via cache_service: Redis is not running
# in this deployment, so cache_service silently no-ops and a cache built on it
# would do nothing (the same trap the shown-unit history avoided).
#
# NOT a TTL. Freshness is checked against a cheap version derived from the
# recordings themselves, so a newly ingested recording can never be silently
# missing from answers — the failure mode a TTL would allow. The check is one
# indexed aggregate (~0.3s) in place of ~5.3s of rebuilding, and because it
# reads shared state it stays correct across worker processes too, which an
# invalidation hook alone would not.
_ARCHIVE_CACHE: Dict[str, "_CachedArchive"] = {}


@dataclass
class _CachedArchive:
    version: tuple
    archive: List[ArchiveSegment]
    entity_map: Dict[str, List[str]]
    units: List["UtteranceUnit"]


async def _archive_version(group_id: str) -> tuple:
    """Cheap fingerprint of a producer's ready recordings. Changes whenever a
    recording is added, removed, or re-analysed (status/updated_at move), which
    is exactly when the derived data must be rebuilt."""
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(
                    func.count(RawSegment.id),
                    func.max(RawSegment.updated_at),
                    func.max(RawSegment.created_at),
                )
                .select_from(RawSegment)
                .join(InterviewSession, RawSegment.interview_session_id == InterviewSession.id)
                .where(InterviewSession.user_id == group_id, RawSegment.status == "ready")
            )
        ).one()
    return (row[0], str(row[1]), str(row[2]))


async def _archive_bundle(
    group_id: str,
) -> Tuple[List[ArchiveSegment], Dict[str, List[str]], List["UtteranceUnit"]]:
    """(archive, entity_map, units) for a producer — rebuilt only when their
    recordings have actually changed."""
    try:
        version = await _archive_version(group_id)
    except Exception as e:
        # Can't confirm freshness -> rebuild rather than risk serving stale
        # data. Costly, not wrong.
        logger.warning(f"Archive version check failed for {group_id}, rebuilding: {e}")
        version = None

    cached = _ARCHIVE_CACHE.get(group_id)
    if cached is not None and version is not None and cached.version == version:
        return cached.archive, cached.entity_map, cached.units

    archive = await _load_archive(group_id)
    if not archive:
        _ARCHIVE_CACHE.pop(group_id, None)
        return [], {}, []
    entity_map = await _build_entity_map(archive, group_id)
    units = _build_units(archive)
    if version is not None:
        _ARCHIVE_CACHE[group_id] = _CachedArchive(version, archive, entity_map, units)
    logger.info(
        f"Archive cache rebuilt for {group_id}: "
        f"{len(archive)} recordings, {len(units)} units, {len(entity_map)} entities"
    )
    return archive, entity_map, units


def invalidate_archive_cache(group_id: Optional[str] = None) -> None:
    """Drop cached archive data. Called when a recording finishes ingesting so
    the next question sees it immediately, without waiting on the version
    check to notice. Passing None clears every producer."""
    if group_id is None:
        _ARCHIVE_CACHE.clear()
    else:
        _ARCHIVE_CACHE.pop(group_id, None)


# Rebuilding used to cost up to ~15s, almost all of it Neo4j; it is now well
# under a second. The bound stays anyway — it guards the whole warm (archive
# load, entity map, unit split), and a failed warm just means the next
# question rebuilds, which is the old behaviour.
_WARM_TIMEOUT_SECONDS = 60


async def warm_archive_cache(group_id: str) -> bool:
    """Rebuild a producer's cache NOW, so the cost lands on ingestion (offline,
    nobody waiting) instead of on whoever asks the first question afterwards —
    that person was paying the full ~15s rebuild.

    Fail-soft by design: if this doesn't succeed the cache is simply empty and
    the next question rebuilds exactly as before, so warming can never make
    things worse than not warming."""
    started = time.perf_counter()
    try:
        archive, entity_map, units = await asyncio.wait_for(
            _archive_bundle(group_id), timeout=_WARM_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"Archive cache warm for {group_id} timed out after "
            f"{_WARM_TIMEOUT_SECONDS}s; next question will rebuild"
        )
        return False
    except Exception as e:
        logger.warning(f"Archive cache warm for {group_id} failed: {e}")
        return False
    logger.info(
        f"Archive cache warmed for {group_id} in {time.perf_counter() - started:.2f}s "
        f"({len(archive)} recordings, {len(units)} units, {len(entity_map)} entities)"
    )
    return True


async def _build_entity_map(archive: List[ArchiveSegment], group_id: str) -> Dict[str, List[str]]:
    """entity name -> the segment ids that mention it.

    ONE Postgres query for the whole archive. This used to be one Neo4j round
    trip PER RECORDING, which measured 4.13s mean — 45% of a turn — and
    carried ALL of the turn's latency variance (1.35s-9.55s across identical
    passes). The per-segment shape was the cost, not the database, so the fix
    is the bulk read rather than the move on its own.

    Still fail-soft: the entity map is an aid to the model, never a hard
    dependency, and an empty one costs measured accuracy of 0.991 vs 0.991.
    Now it fails soft as a whole rather than per segment, which is the honest
    shape for a single query — there is no longer a per-segment failure that
    could omit one recording while keeping the rest.
    """
    try:
        async with AsyncSessionLocal() as db:
            by_segment = await entity_store.get_entity_names_for_segments(
                db, [item.segment.id for item in archive], group_id
            )
    except Exception as e:
        logger.warning(f"Entity map lookup failed for {group_id}, continuing without it: {e}")
        return {}

    entity_to_segments: Dict[str, List[str]] = {}
    # Iterate the ARCHIVE, not the query result, so the segment order in each
    # entry follows the archive's own order rather than the database's.
    for item in archive:
        for name in by_segment.get(item.segment.id, []):
            entity_to_segments.setdefault(name, [])
            if item.segment.id not in entity_to_segments[name]:
                entity_to_segments[name].append(item.segment.id)
    return entity_to_segments


def _format_entity_map(
    entity_map: Dict[str, List[str]], ordinals: Dict[str, int]
) -> str:
    """The entity map as the model can actually use it.

    THE POINT OF `ordinals`: this used to print raw segment UUIDs while the
    transcript block labelled the same recordings `RECORDING 1..12`. The model
    had no mapping between them — it could read `אילנה: 502fb283…` and had no
    way to discover that this was RECORDING 1. Every pointer in this block was
    unresolvable, which is the most likely reason accuracy measured 0.991 with
    AND without the map: the only surviving signal was the bare existence and
    spelling of the names.

    It also contradicted a deliberate decision made in
    `_format_annotated_transcript`, which goes out of its way NOT to print
    segment UUIDs, because the model would return one where a unit id was
    expected and silently produce a no-story. This block was feeding it those
    UUIDs anyway.

    Entities whose recordings are all filtered out of the transcript block are
    OMITTED rather than printed without a pointer — a name pointing at a
    recording the model cannot see is the same unresolvable reference in a
    quieter form.
    """
    if not entity_map:
        return "(none extracted)"
    lines = []
    for name, seg_ids in sorted(entity_map.items()):
        refs = [f"RECORDING {ordinals[s]}" for s in seg_ids if s in ordinals]
        if refs:
            lines.append(f"- {name}: {', '.join(refs)}")
    return "\n".join(lines) if lines else "(none extracted)"


# ── Step 3: the single range-selection LLM call ─────────────────────────────


def _parse_follow_up(text: str) -> Optional[dict]:
    """The optional {"question", "unit_ids"} suggestion from the SAME reply as
    the answer — deliberately not a second LLM call. Returns None when absent
    or malformed; validation against real units happens in select_units."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    fu = data.get("follow_up")
    if not isinstance(fu, dict):
        return None
    question = fu.get("question")
    raw_ids = fu.get("unit_ids")
    if not isinstance(question, str) or not question.strip():
        return None
    if not isinstance(raw_ids, list):
        return None
    ids = [i.strip() for i in raw_ids if isinstance(i, str) and i.strip()]
    if not ids:
        return None
    return {"question": question.strip(), "unit_ids": ids}


def _parse_unit_selection(text: str) -> List[str]:
    """Extract the selected unit ids, preserving the model's stitch order.

    Accepts the documented {"unit_ids": [...]} object and also a bare JSON
    array of ids, so a model that drops the wrapper still parses rather than
    silently yielding a no-story. Unknown ids are NOT filtered here — step 4
    is the real gate."""
    candidates: List[str] = []
    obj_match = re.search(r"\{.*\}", text, re.DOTALL)
    if obj_match:
        try:
            data = json.loads(obj_match.group(0))
            if isinstance(data, dict):
                raw = data.get("unit_ids")
                if isinstance(raw, list):
                    candidates = raw
        except json.JSONDecodeError:
            candidates = []
    if not candidates:
        arr_match = re.search(r"\[.*\]", text, re.DOTALL)
        if arr_match:
            try:
                data = json.loads(arr_match.group(0))
                if isinstance(data, list):
                    candidates = data
            except json.JSONDecodeError:
                return []

    out: List[str] = []
    for item in candidates:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, int):
            # Tolerate a bare number where an id was asked for.
            out.append(f"u{item}")
    return out


async def _read_archive_for_ranges(
    question: str,
    transcript_block: str,
    entity_map_block: str,
    history_block: str,
    recording_language: str,
) -> Tuple[List[str], Optional[dict]]:
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
    if history_block:
        user_parts.append(f"Recent conversation:\n{history_block}\n")
    user_parts.append(f"Question:\n{question}")
    user_message = "\n".join(user_parts)

    try:
        raw = await llm_service.generate_response(
            messages=[{"role": "user", "content": user_message}],
            system_prompt=system_prompt,
            temperature=0,
            # Per-call model override (settings.ARCHIVE_READ_MODEL). This is
            # the one call in the pipeline doing hard reasoning over the whole
            # archive; the rest stay on the cheaper default. Empty => default.
            model=settings.ARCHIVE_READ_MODEL or None,
            # Latency lever, NOT a determinism one — see the setting's comment.
            thinking_budget=settings.ARCHIVE_READ_THINKING_BUDGET or None,
        )
    except Exception as e:
        logger.warning(f"Archive-read LLM call failed, treating as no-story: {e}")
        return [], None
    return _parse_unit_selection(raw), _parse_follow_up(raw)


# ── Step 4: deterministic validation + word-boundary snapping (no LLM) ──────


def resolve_units_to_clips(
    unit_ids: List[str], units: List[UtteranceUnit]
) -> List[ExpandedClip]:
    """Turn the model's selected unit ids into playable ranges — deterministic
    and LLM-free.

    Times are DERIVED from the units (which came from real word timestamps),
    never taken from the model, so there is nothing to snap or clamp: a
    selected unit's boundaries are exact by construction. This is why the old
    range-snapping step is gone rather than kept alongside — there is exactly
    one boundary mechanism now.

    Unknown ids are dropped with a warning. The model's stitch order is
    preserved; a unit that IMMEDIATELY follows the previous selected one (same
    recording, next index) extends it into a single continuous clip, so a run
    of consecutive units plays as one uninterrupted piece and non-consecutive
    selections stay separate pieces."""
    by_id = {u.unit_id: u for u in units}

    selected: List[UtteranceUnit] = []
    seen: set[str] = set()
    for uid in unit_ids:
        unit = by_id.get(uid)
        if unit is None:
            logger.warning(f"Archive-read returned unknown unit id {uid!r}; dropping")
            continue
        if uid in seen:
            continue  # a repeated id would otherwise duplicate footage
        seen.add(uid)
        selected.append(unit)

    clips: List[ExpandedClip] = []
    prev: Optional[UtteranceUnit] = None
    for unit in selected:
        if (
            prev is not None
            and unit.segment_id == prev.segment_id
            and unit.index == prev.index + 1
        ):
            # Consecutive in the original speech — extend rather than emit a
            # second clip, so the pause between them plays naturally.
            clips[-1] = ExpandedClip(
                raw_segment_id=clips[-1].raw_segment_id,
                start_sec=clips[-1].start_sec,
                end_sec=unit.end_sec,
                source_chunk_id=clips[-1].source_chunk_id,
            )
        else:
            clips.append(
                ExpandedClip(
                    raw_segment_id=unit.segment_id,
                    start_sec=unit.start_sec,
                    end_sec=unit.end_sec,
                    # No single source chunk in this mode — a clip can span
                    # several. Marked so it's never mistaken for a v1 chunk id.
                    source_chunk_id=f"archive-read:{unit.segment_id}",
                )
            )
        prev = unit
    return clips


async def read_and_validate_ranges(
    question: str, group_id: str, recording_language: str, session_id: str
) -> List[ExpandedClip]:
    """Steps 1-4: everything up to (but not including) ffmpeg assembly —
    the actual 'which ranges' decision that differs from v1. Exposed
    separately so the comparison harness (and tests) can inspect the chosen
    ranges without running ffmpeg."""
    return (await select_units(question, group_id, recording_language, session_id)).clips


@dataclass
class UnitSelection:
    """What the read produced: the playable clips, the units behind them, and
    whether every one of them had already been played this session (which is
    'nothing NEW to show', a different answer from 'nothing relevant exists')."""

    clips: List[ExpandedClip]
    selected_units: List[UtteranceUnit]
    # {"question": str} when the model offered a follow-up that SURVIVED
    # validation, else None. Chat text only — never spoken, never in the video.
    follow_up: Optional[dict] = None


async def select_units(
    question: str, group_id: str, recording_language: str, session_id: str
) -> UnitSelection:
    # These three reads are INDEPENDENT — the archive bundle keys off the
    # producer, the other two off the session — and none of them feeds any of
    # the others. Run sequentially they cost the SUM of three round trips to
    # Neon; concurrently they cost the slowest one.
    #
    # This is round-trip latency, not query work: a trivial `SELECT 1` against
    # the same instance measures ~0.17s, and each of these was ~0.36s of a
    # 7.6s turn. Nothing here is CPU- or IO-bound locally, so gathering is
    # pure win — the queries were already isolated in their own sessions.
    #
    # The empty-archive early return now happens AFTER the other two reads
    # rather than before, so a producer with no ready recordings pays two
    # pointless queries. That path returns the no-story fallback regardless
    # and is not on any hot path worth branching for.
    archive_bundle, shown, turns = await asyncio.gather(
        _archive_bundle(group_id),
        _load_shown_units(session_id),
        retrieval_service._recent_turns(
            session_id, retrieval_service.COREFERENCE_HISTORY_TURNS
        ),
    )
    archive, entity_map, units = archive_bundle
    if not archive or not units:
        return UnitSelection([], [], False)

    shown_keys, per_turn_units = shown

    transcript_block = _format_annotated_transcript(archive, units, shown_keys)
    # Same ordinals the transcript block just printed — see _recording_ordinals.
    entity_map_block = _format_entity_map(entity_map, _recording_ordinals(archive, units))
    history_block = _format_history_block(turns, per_turn_units) if turns else ""

    unit_ids, raw_follow_up = await _read_archive_for_ranges(
        question, transcript_block, entity_map_block, history_block, recording_language
    )

    by_id = {u.unit_id: u for u in units}
    selected = [by_id[uid] for uid in dict.fromkeys(unit_ids) if uid in by_id]
    clips = resolve_units_to_clips(unit_ids, units)
    follow_up = _validate_follow_up(raw_follow_up, by_id, selected, shown_keys)
    return UnitSelection(clips=clips, selected_units=selected, follow_up=follow_up)


def _validate_follow_up(
    raw: Optional[dict],
    by_id: Dict[str, UtteranceUnit],
    answer_units: List[UtteranceUnit],
    shown_keys: set,
) -> Optional[dict]:
    """Same deterministic gate the answer gets: a suggestion survives only if
    it points at REAL units that are not part of this answer and were not
    already played. Anything else is dropped SILENTLY — no suggestion is a
    perfectly good outcome, and a broken one would offer content we can't
    actually show."""
    if not raw:
        return None
    answer_keys = {_unit_key(u.segment_id, u.start_sec) for u in answer_units}
    kept: List[UtteranceUnit] = []
    for uid in dict.fromkeys(raw["unit_ids"]):
        unit = by_id.get(uid)
        if unit is None:
            logger.info(f"Follow-up suggestion referenced unknown unit {uid!r}; dropping suggestion")
            return None
        key = _unit_key(unit.segment_id, unit.start_sec)
        if key in answer_keys or key in shown_keys:
            continue  # already covered — not "more"
        kept.append(unit)
    if not kept:
        return None
    return {"question": raw["question"]}


# ── Step 5: orchestrate, reusing the existing assembly/caching/storage ──────


async def assemble_video_clip_response_v2(
    question: str, group_id: str, recording_language: str, session_id: str
) -> VideoClipResult:
    """The v2 parallel to video_clip_assembler.assemble_video_clip_response.
    Identical return contract (a clip URL or NO_STORY_FALLBACK) so the WS
    handler and frontend treat both modes the same; only the range decision
    (read_and_validate_ranges) differs. Assembly, caching, and storage are
    the EXACT same code the v1 path uses."""
    selection = await select_units(question, group_id, recording_language, session_id)
    clips = selection.clips
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

    # Carried back so the WS handler can persist WHICH units this answer
    # played (on the assistant Message's metadata) — that record is what the
    # next turn reads for both the already-shown marks and the history block.
    return VideoClipResult(
        video_url=video_url,
        follow_up=selection.follow_up,
        shown_units=[
            {
                "key": _unit_key(u.segment_id, u.start_sec),
                "unit_id": u.unit_id,
                "text": u.text,
            }
            for u in selection.selected_units
        ],
    )
