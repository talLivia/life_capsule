"""
Full-archive reading — THE video-clip chat mode (Prompt 15,
"video_clips_v2"). Born as an A/B alternative to the Prompt 11-14
chunk-retrieval pipeline (v1 `video_clips`), which it then replaced
outright: the A/B settled it and v1 was removed on 2026-08-12
(docs/V1_REMOVAL_PLAN.md; tag `pre-v1-removal` holds the last tree with
both). Same input (question + producer archive + conversation), same
output shape (a single assembled clip URL, or NO_STORY_FALLBACK).

Where v1 ran a multi-step retrieval chain (coreference resolution ->
perspective normalization -> three-signal chunk matching -> leniency
retry -> per-candidate verify/pinpoint), THIS mode replaces all of it
with ONE LLM call that READS the entire annotated archive transcript
(plus an entity map and the recent conversation) and directly returns
which parts answer the question. The ffmpeg assembly downstream lives in
video_clip_assembler.py, shared then and now.

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
import zlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import InterviewSession, Message, RawSegment, TranscriptChunk
from app.services import (
    answer_cache,
    core_compression,
    entity_store,
    prefilter,
    response_assembler,
    retrieval_service,
    video_clip_assembler,
)
from app.services.cache import cache_service
from app.services.llm import llm_service
from app.services.response_assembler import (
    NO_STORY_FALLBACK,
    TRANSIENT_FAILURE_FALLBACK,
)
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
- A BROAD question answered by several distinct stretches is where \
including and offering meet: include the stretches that address the \
question's subject directly, and when one stretch is a SELF-CONTAINED \
side branch - a related but distinct aspect, origin, or group, often a \
stretch whose own recording answers a different interview question - \
prefer to leave that stretch out of the answer and make it the follow_up \
suggestion instead. The answer should feel complete without it; the \
offer is how the listener reaches it. This never shrinks a NARROW \
question's answer: when only a few units answer, include them all and \
this rule does not apply.
- If there is no such material, omit "follow_up" or set it to null. No \
suggestion is perfectly fine - never invent one to seem helpful.
{disambiguation_block}
Rules:
- Output ONLY a JSON object, nothing else, exactly: \
{{"unit_ids": ["{id_ex1}", "{id_ex2}", ...], "follow_up": {{"question": "...", \
"unit_ids": ["{id_ex3}"]}}}}  (or {{"unit_ids": [...], "follow_up": null}})
- Use ONLY unit ids of the form {id_form} that literally appear below, \
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
avoid an empty answer. With or without units selected, you may add \
"about": the name of the one person or place the question was asking about, \
copied EXACTLY as it is spelled in the archive - resolving it from the recent \
conversation when the question itself does not name them. Use "about": null \
whenever the question was not about a single named person or place, when you \
are not certain which one it was, or when the name is not one the archive \
already uses. It is only ever a name, never a description, and it changes \
nothing about which units you choose.

FULL ARCHIVE TRANSCRIPT:
{transcript_block}

ENTITY MAP (entity name -> the RECORDINGS that mention it):
{entity_map_block}"""


# Added to the prompt ONLY when this archive actually contains two people with
# similar names. An archive without them produces a byte-identical prompt to
# the one it produced before this feature existed — asserted by a test, and
# the strongest available form of "it cannot over-ask where there is nothing
# to ask about".
#
# Written to spend most of its words on when NOT to clarify. The obvious
# failure mode of teaching a model to ask "which one?" is that it starts
# asking when the answer was already obvious, and that is worse than the
# conflation being fixed: the conflation affects questions about one name,
# over-asking affects every question.
#
# THE EXAMPLES BELOW ARE LOAD-BEARING IN A DIRECTION NOBODY WOULD PREDICT.
# Isolated n=6, varying tag and instruction independently: the tags do nothing
# to `army-narrow` on their own (u11,u12 either way), while THIS BLOCK alone
# broadens it to u11-u14 — pulling in "I had good friends there", a unit with
# no name in it and no tag on it. The cause is the example "my friend from the
# army" sitting in the prompt while the question asks what role you served in;
# swapping it for "my neighbour" restores u11,u12 at 6/6.
#
# That swap was NOT made, because it costs more than it fixes: `school` drops
# from 8 units to 0 (4/4), losing a real answer about which schools he
# attended. Three of the four configurations tried do that. All five same-name
# cases are identical across the examples, so this wording moves ONLY the
# collateral — and this setting is the best of the four measured.
#
# Reword only with a measured before/after on BOTH sides, and note that the
# clarification gate stayed green through every one of the broken variants.
# See docs/ENTITY_DISAMBIGUATION.md §8.3 for the full table.
_DISAMBIGUATION_BLOCK = """
TWO PEOPLE WITH THE SAME NAME (this archive has some):
Some names in the transcript are followed by a tag in square brackets naming WHICH person that recording is about, e.g. "אמנון [אמנון נחום: דוד של הדובר מצד אבא]". The tag is an annotation added for you - it is NOT part of what the person said, is not in the video, and must never appear in a follow-up question or anywhere in your output.
- The tag tells you which of the same-named people a RECORDING is about. Two recordings tagged differently are about DIFFERENT people, however similar their words look. Never build ONE answer out of units about two different people who share a name.
- When the question makes clear which one is meant - it uses the fuller name, or names their role ("my uncle", "my friend from the army"), or the recent conversation was already about one of them - answer it NORMALLY from that person's recordings. Do not ask, and do not leave out units that genuinely answer it.
- Ask ONLY when the question is about a same-named person AND nothing in the question or the recent conversation decides which one. Then, and only then, output {{"unit_ids": [], "clarify": {{"question": "...", "options": ["...", "..."]}}}} - a short question naming the choices, in the storyteller's own voice and language. Never output units and "clarify" together.
- THIS CHANGES NOTHING ABOUT ANY OTHER QUESTION. A question that does not involve a same-named person is chosen for exactly as it would be otherwise. Never clarify about topics, dates, places, or what someone meant - only ever about which of two people who share a name. A tag existing somewhere in the archive is never a reason to narrow, doubt, or hold back an answer about anything else.
- NEVER return an empty selection because of any of this. If you cannot tell which person is meant, clarify. Otherwise answer. An empty selection still means only what it has always meant: nothing in the archive answers the question at all."""


# ── Step 1: load + format the annotated transcript ──────────────────────────


async def _load_archive(group_id: str) -> List[ArchiveSegment]:
    """Every 'ready' segment in this producer's archive (chronological by
    created_at) with its chunks ordered by sequence_index — the same
    InterviewSession.user_id + status='ready' scoping join the segment-level
    readers use, grouped/ordered for a whole-archive read.

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


def _id_atoms() -> Dict[str, str]:
    """The id-form fragments formatted into the reader prompt
    (UNIT_ID_STABILITY_PLAN §2). Under the default global scheme these
    render the EXACT bytes the prompt has always contained — pinned by
    test_the_prompt_is_byte_identical_when_no_two_people_share_a_name."""
    from app.config import settings

    if settings.UNIT_ID_SCHEME == "scoped":
        return dict(
            id_ex1="r2u3", id_ex2="r2u4", id_ex3="r5u1",
            id_form="r<recording>u<number> (for example r7u3)",
        )
    return dict(id_ex1="u3", id_ex2="u4", id_ex3="u9", id_form="u<number>")


def _unit_id_for(item: "ArchiveSegment", global_index: int, local_index: int) -> str:
    """The rendered unit id, per settings.UNIT_ID_SCHEME
    (UNIT_ID_STABILITY_PLAN §1-2):

      global (today's default): u<global_index> — one monotonic sequence
        across the archive in load order; any new recording renumbers
        everything after its insertion point.
      scoped: r<recording_no>u<local_index> — anchored to the recording's
        ingestion-assigned number, so ids never renumber. Refuses a ready
        segment without recording_no rather than inventing an anchor.

    `global_index` stays on every unit REGARDLESS of scheme, as the
    in-memory ordering/contiguity key (_group_selected_runs) — it is never
    persisted and never rendered under `scoped`."""
    from app.config import settings

    if settings.UNIT_ID_SCHEME == "scoped":
        rec_no = item.segment.recording_no
        if rec_no is None:
            raise RuntimeError(
                f"segment {item.segment.id} has no recording_no — migration "
                "0028 assigns it; a scoped unit id cannot be invented"
            )
        return f"r{rec_no}u{local_index}"
    return f"u{global_index}"


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
    local = 1

    def _emit(words: List[Tuple[str, float, float]]) -> None:
        nonlocal index, local
        if not words:
            return
        units.append(
            UtteranceUnit(
                unit_id=_unit_id_for(item, index, local),
                segment_id=item.segment.id,
                index=index,
                start_sec=words[0][1],
                end_sec=words[-1][2],
                text=" ".join(w for w, _, _ in words),
            )
        )
        index += 1
        local += 1

    for chunk in item.chunks:
        words = _chunk_words(chunk)
        if not words:
            if chunk.end_sec > chunk.start_sec and (chunk.text or "").strip():
                units.append(
                    UtteranceUnit(
                        unit_id=_unit_id_for(item, index, local),
                        segment_id=item.segment.id,
                        index=index,
                        start_sec=chunk.start_sec,
                        end_sec=chunk.end_sec,
                        text=chunk.text.strip(),
                    )
                )
                index += 1
                local += 1
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


def _format_shown_block(
    archive: List[ArchiveSegment], units: List[UtteranceUnit], shown_keys: set
) -> str:
    """The shown-state block for the per-turn user message
    (SHOWN_STATE_PLACEMENT=message). Ids are CURRENT ids resolved from the
    persisted stable keys, in archive order — the same render-time key→id
    resolution the history block does, so a renumbering ingest can never
    make the list point at the wrong units. Empty state renders NOTHING
    (no vestigial header), which is what keeps the empty-shown user
    message byte-identical to today's.

    GROUPED PER RECORDING, with completeness stated outright. The first
    (flat-list) rendering FAILED its gate: on the exhaustion corners the
    model re-served 9-12 already-shown units instead of clarifying —
    replicated under `message` across cache epochs, absent in the inline
    control. The inline mark sits adjacent to the unit text at the moment
    of judgment; a flat id list demands a join the "everything about this
    is already shown" inference didn't survive. Grouping by the SAME
    recording ordinals the transcript prints, and saying "ALL units"
    explicitly, hands the model the exhaustion fact instead of asking it
    to derive one by counting ids.

    The bridging phrase maps the block onto the convention the system
    prompt already establishes — the system template is deliberately NOT
    reworded per mode (its bytes are the cacheable prefix and the whole
    point is that they never change)."""
    if not shown_keys:
        return ""
    shown_units = [
        u for u in units if _unit_key(u.segment_id, u.start_sec) in shown_keys
    ]
    if not shown_units:
        return ""
    by_segment_all = _units_by_segment(units)
    ordinals = _recording_ordinals(archive, units)
    by_segment_shown: Dict[str, List[UtteranceUnit]] = {}
    for u in shown_units:
        by_segment_shown.setdefault(u.segment_id, []).append(u)
    lines = [
        "ALREADY SHOWN (played earlier in this conversation — treat these"
        " units exactly as if marked [ALREADY SHOWN] in the transcript):"
    ]
    # Archive order, matching the transcript's own recording order.
    for item in archive:
        seg_id = item.segment.id
        shown_here = by_segment_shown.get(seg_id)
        if not shown_here or seg_id not in ordinals:
            continue
        ids = ", ".join(u.unit_id for u in shown_here)
        total = len(by_segment_all.get(seg_id, []))
        if len(shown_here) == total:
            suffix = " — ALL units of this recording already shown"
        else:
            suffix = f" ({len(shown_here)} of {total} units)"
        lines.append(f"RECORDING {ordinals[seg_id]}: {ids}{suffix}")
    return "\n".join(lines)


def _build_user_message(
    question: str, history_block: str, shown_block: str = ""
) -> str:
    """The variable half of the prompt. With shown_block empty this
    renders byte-for-byte what the inline path always sent — pinned by
    test, because it is the Phase A blast-radius boundary."""
    user_parts: List[str] = []
    if history_block:
        user_parts.append(f"Recent conversation:\n{history_block}\n")
    if shown_block:
        user_parts.append(f"{shown_block}\n")
    user_parts.append(f"Question:\n{question}")
    return "\n".join(user_parts)


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
    from app.config import settings

    if settings.UNIT_ID_SCHEME == "scoped":
        # Stable recording numbers, gaps allowed (a deleted recording's
        # number is simply absent — honest, and the prompt already forbids
        # the model outputting recording numbers as ids).
        return {item.segment.id: item.segment.recording_no for item in printed}
    return {item.segment.id: i for i, item in enumerate(printed, start=1)}


# ── Telling two people with one name apart, at query time ───────────────────
#
# THE TRANSCRIPT CANNOT DO THIS ON ITS OWN. It says "אמנון" whether the speaker
# meant his uncle or his army friend, so an answer about one gets assembled
# from the other's recordings — which it was. The archive is not missing the
# distinction: `entity_mentions` points each mention at exactly one entity, so
# it knows which אמנון every RECORDING means. Nothing was reading it.
#
# Why inline rather than in the entity map: the entity map is already in the
# prompt and is inert. Measured this session — real map / no map / both names
# merged produced byte-identical unit selections across two runs each, and
# accuracy is 0.9987 with and without. Adding to a block the model demonstrably
# ignores would change nothing. This goes where the model is already reading.
#
# NOTHING IS WRITTEN TO THE ARCHIVE. The annotation exists only in the string
# handed to the model for one call; `TranscriptChunk.text`, `word_timestamps`
# and `RawSegment.transcript` are untouched, and the video is cut from unit
# ids and word times that never saw a tag.

#: Only these, so a tag can never be mistaken for something the person said.
#: Same convention as [ALREADY SHOWN], which the prompt already establishes.
_TAG_OPEN, _TAG_CLOSE = "[", "]"

#: Hebrew glues single-letter prefixes onto names — "ואמנון", "לאמנון". Matching
#: the bare name would miss those, which are common in speech.
_HEBREW_PREFIXES = "והלבכמש"

#: A summary is one sentence of context, not a biography. Long enough to tell
#: an uncle from an army friend, short enough not to swamp the words spoken.
_TAG_SUMMARY_CHARS = 70


@dataclass(frozen=True)
class _NameTag:
    """What to write next to one name, in one recording."""

    #: The surface forms to look for, longest first — the spoken word is often
    #: the SHORT one even where the entity carries the longer name ("...דוד
    #: שקוראים לו אמנון" is the row stored as "אמנון נחום").
    surfaces: Tuple[str, ...]
    label: str


def _tag_label(member: "entity_store.ConfusableEntity") -> str:
    summary = " ".join((member.summary or "").split())
    if len(summary) > _TAG_SUMMARY_CHARS:
        summary = summary[:_TAG_SUMMARY_CHARS].rstrip() + "…"
    return f"{member.name}: {summary}" if summary else member.name


def _build_name_tags(
    groups: List[List["entity_store.ConfusableEntity"]],
) -> Dict[str, List[_NameTag]]:
    """segment id -> the tags to apply inside that recording's units.

    A recording is tagged only when it links to EXACTLY ONE member of a
    confusable group. Two members means the recording genuinely talks about
    both people, and there is nothing in the data saying which occurrence of
    the name is which — so it is left alone. Guessing there would put a
    confident wrong label in front of the model, which is worse than the
    ambiguity it is trying to fix.
    """
    tags: Dict[str, List[_NameTag]] = {}
    for group in groups:
        surfaces = tuple(
            sorted({m.name for m in group}, key=lambda n: (-len(n), n))
        )
        for member in group:
            for segment_id in member.segment_ids:
                if sum(1 for m in group if segment_id in m.segment_ids) > 1:
                    continue
                tags.setdefault(segment_id, []).append(
                    _NameTag(surfaces=surfaces, label=_tag_label(member))
                )
    return tags


def _annotate_names(text: str, tags: List[_NameTag]) -> str:
    """Write each tag next to the name it explains, inside one unit's text."""
    for tag in tags:
        for surface in tag.surfaces:
            if not surface.strip():
                continue
            # A single optional Hebrew prefix letter, and no Hebrew letter
            # either side — so "אמנון" matches inside "ואמנון" but not inside a
            # longer word that merely contains it.
            pattern = (
                rf"(?<![א-ת])([{_HEBREW_PREFIXES}]?){re.escape(surface)}"
                rf"(?![א-ת])"
            )
            replaced, count = re.subn(
                pattern,
                lambda m: f"{m.group(1)}{surface} {_TAG_OPEN}{tag.label}{_TAG_CLOSE}",
                text,
            )
            if count:
                # First surface that matches wins. Continuing would tag
                # "אמנון" a second time inside the label just written.
                text = replaced
                break
    return text


async def _build_name_tags_for(group_id: str) -> Dict[str, List[_NameTag]]:
    """The same-name tags for one producer, or {} when nothing is confusable.

    Fail-soft, exactly like the entity map above it and for the same reason:
    this is an aid to the model, not a dependency. A lookup failure must cost
    the disambiguation, never the answer — and {} is also the pre-feature
    behaviour, so the failure mode is "as it was before" rather than "broken".
    """
    try:
        async with AsyncSessionLocal() as db:
            groups = await entity_store.confusable_entities(db, group_id)
    except Exception as e:
        logger.warning(
            f"Confusable-name lookup failed for {group_id}, "
            f"continuing without same-name tags: {e}"
        )
        return {}
    return _build_name_tags(groups)


def _format_annotated_transcript(
    archive: List[ArchiveSegment],
    units: List[UtteranceUnit],
    shown_keys: Optional[set] = None,
    name_tags: Optional[Dict[str, List[_NameTag]]] = None,
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
    name_tags = name_tags or {}
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
        tags = name_tags.get(seg.id) or []
        for unit in seg_units:
            mark = (
                " [ALREADY SHOWN]"
                if _unit_key(unit.segment_id, unit.start_sec) in shown_keys
                else ""
            )
            # Annotated for the model only. `unit.text` itself is left alone —
            # it is what the answer's spoken text is built from, and a tag
            # reaching that would put a bracketed note in the chat beside a
            # video that never says it.
            text = _annotate_names(unit.text, tags) if tags else unit.text
            lines.append(
                f"  {unit.unit_id} [{unit.start_sec:.2f}-{unit.end_sec:.2f}]{mark} {text}"
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
    turns: List[Dict[str, str]],
    per_turn_units: List[List[dict]],
    key_to_id: Optional[Dict[str, str]] = None,
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
            # Resolve each unit's CURRENT id from its stable key
            # (segment_id:start_sec) rather than trusting the id persisted
            # at play time - persisted ids go stale on any renumbering
            # (they already had, before the scoped scheme existed). The
            # stored id is only a fallback for pre-key rows.
            def _rid(u: dict) -> str:
                if key_to_id:
                    resolved = key_to_id.get(u.get("key", ""))
                    if resolved:
                        return resolved
                return u.get("unit_id", "?")

            rendered = "; ".join(
                f"{_rid(u)}: \"{u.get('text', '')}\"" for u in units
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
    #: segment id -> the same-name tags to write inside it. Cached with the
    #: rest because it is derived from the same recordings and changes only
    #: when they do — an entity is renamed by confirming a recording, which
    #: moves the version.
    name_tags: Dict[str, List["_NameTag"]]


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
) -> Tuple[
    List[ArchiveSegment],
    Dict[str, List[str]],
    List["UtteranceUnit"],
    Dict[str, List["_NameTag"]],
]:
    """(archive, entity_map, units, name_tags) for a producer — rebuilt only when their
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
        return cached.archive, cached.entity_map, cached.units, cached.name_tags

    archive = await _load_archive(group_id)
    if not archive:
        _ARCHIVE_CACHE.pop(group_id, None)
        return [], {}, [], {}
    entity_map = await _build_entity_map(archive, group_id)
    units = _build_units(archive)
    name_tags = await _build_name_tags_for(group_id)
    if version is not None:
        _ARCHIVE_CACHE[group_id] = _CachedArchive(
            version, archive, entity_map, units, name_tags
        )
    logger.info(
        f"Archive cache rebuilt for {group_id}: "
        f"{len(archive)} recordings, {len(units)} units, {len(entity_map)} entities, "
        f"{len(name_tags)} recordings with same-name tags"
    )
    return archive, entity_map, units, name_tags


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
        archive, entity_map, units, _tags = await asyncio.wait_for(
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


def _parse_clarify(text: str) -> Optional[dict]:
    """The optional {"question", "options"} from the SAME reply as the answer.

    Returns None when absent or malformed. Two options is the minimum that
    means anything: "which אמנון?" with one option is not a choice, and the
    model producing one is a sign it did not actually find two people.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("clarify")
    if not isinstance(raw, dict):
        return None
    question = raw.get("question")
    options = raw.get("options")
    if not isinstance(question, str) or not question.strip():
        return None
    if not isinstance(options, list):
        return None
    cleaned = [o.strip() for o in options if isinstance(o, str) and o.strip()]
    if len(cleaned) < 2:
        return None
    return {"question": question.strip(), "options": cleaned}


def _parse_about(text: str) -> Optional[str]:
    """The optional subject NAME accompanying an empty selection.

    A bare name and nothing else. It is not validated here — `_resolve_about`
    checks it against the archive's real entities, because a name the model
    produced is a claim about the archive and has to be checked against the
    archive, not against itself.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    about = data.get("about")
    if not isinstance(about, str) or not about.strip():
        return None
    return about.strip()


def _resolve_about(
    raw: Optional[str], entity_map: Dict[str, List[str]]
) -> Optional[Tuple[str, List[str]]]:
    """(the archive's own spelling, the recordings that mention it), or None.

    THE NAME MUST ALREADY EXIST. "I don't have another story about X" is a
    statement about the archive, so X has to be something the archive actually
    holds — otherwise a model that answered "what pets did you have?" by
    naming a plausible-sounding pet would have us assert it. Matched on the
    normalised key so a final-letter form or a stray space still resolves, and
    the STORED spelling is what gets shown, never the model's.
    """
    if not raw:
        return None
    key = entity_store.normalize_entity_name(raw)
    if not key:
        return None
    for name, segment_ids in entity_map.items():
        if entity_store.normalize_entity_name(name) == key:
            return name, segment_ids
    logger.info(f"Archive read named {raw!r} as the subject; no such entity — using the generic line")
    return None


def _parse_unit_selection(text: str) -> Tuple[List[str], int]:
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
                return [], 0

    from app.config import settings

    scoped = settings.UNIT_ID_SCHEME == "scoped"
    out: List[str] = []
    malformed = 0
    for item in candidates:
        if isinstance(item, str) and item.strip():
            token = item.strip()
            if scoped and not re.fullmatch(r"r\d+u\d+", token):
                # Wrong shape under the scoped scheme (bare number, bare
                # u<number>, invented prefix). Counted for the
                # copy-reliability measurement (UNIT_ID_STABILITY_PLAN §4)
                # and dropped — a malformed id can never be resolved.
                malformed += 1
                logger.info(f"malformed unit id from model: {token!r}")
                continue
            out.append(token)
        elif isinstance(item, int):
            if scoped:
                # A bare number is AMBIGUOUS under scoped ids (r?u<n>) —
                # the global scheme's rescue would have to guess.
                malformed += 1
                logger.info(f"malformed unit id from model: bare int {item}")
                continue
            # Tolerate a bare number where an id was asked for.
            out.append(f"u{item}")
    return out, malformed


@dataclass
class ArchiveRead:
    """One archive-read call's outcome.

    A dataclass rather than a tuple because the tuple had grown to four
    elements and was about to grow a fifth — and the fifth is the one that
    matters most, so it must not be the easiest to drop on the floor at a call
    site.

    `failed` IS THE POINT. It separates "the model read the archive and chose
    nothing" from "the read never happened". Those produced an identical value
    until now, so an outage was reported to a family member as "your relative
    has no story about that" — a false statement about somebody's life. It
    also destroyed three measurements in one day before anyone noticed the two
    were the same value.
    """

    unit_ids: List[str]
    follow_up: Optional[dict] = None
    clarify: Optional[dict] = None
    about: Optional[str] = None
    failed: bool = False
    # Model outputs that failed id-shape parsing (UNIT_ID_STABILITY_PLAN
    # §4's copy-reliability instrument). Server-side measurement only.
    malformed_ids: int = 0


async def _read_archive_for_ranges(
    question: str,
    transcript_block: str,
    entity_map_block: str,
    history_block: str,
    recording_language: str,
    disambiguation: bool = False,
    shown_block: str = "",
    group_id: Optional[str] = None,
    archive_version: Optional[tuple] = None,
) -> ArchiveRead:
    """The ONE LLM call. Still fail-soft — a live turn must produce a sentence
    rather than a stack trace — but the failure is now REPORTED rather than
    disguised as an empty selection. Never a guessed clip either way, which is
    the never-invent contract the other modes' LLM steps also keep.

    See _ARCHIVE_READER_SYSTEM_PROMPT_TEMPLATE's comment for why the static
    transcript/entity-map live in the system prompt and the variable
    conversation/question live in the user message (prompt-cache ordering)."""
    # The JSON form for "clarify" lives INSIDE the block rather than in the
    # Rules section, and that placement is load-bearing rather than tidy. As a
    # trailing rule it landed straight after "If NOTHING answers the question,
    # output {"unit_ids": []}" — two consecutive rules modelling an empty
    # answer, immediately before the transcript. Measured: `school` fell from
    # 8 units to 0 and "what did אמנון do in the army" from 4 to 0, both 3/3,
    # returning empty selections rather than clarifications. Neither question
    # was even about an ambiguous name in the first place.
    # Id-form atoms per scheme (UNIT_ID_STABILITY_PLAN §2): under the
    # default global scheme these render the EXACT bytes the prompt has
    # always contained; scoped swaps only the atoms, nothing else.
    system_prompt = _ARCHIVE_READER_SYSTEM_PROMPT_TEMPLATE.format(
        transcript_block=transcript_block,
        entity_map_block=entity_map_block,
        disambiguation_block=_DISAMBIGUATION_BLOCK if disambiguation else "",
        **_id_atoms(),
    )

    user_message = _build_user_message(question, history_block, shown_block)

    async def _call(cached_content: Optional[str] = None) -> str:
        return await llm_service.generate_response(
            messages=[{"role": "user", "content": user_message}],
            system_prompt=system_prompt,
            temperature=0,
            # Per-call model override (settings.ARCHIVE_READ_MODEL). This is
            # the one call in the pipeline doing hard reasoning over the whole
            # archive; the rest stay on the cheaper default. Empty => default.
            model=settings.ARCHIVE_READ_MODEL or None,
            # Latency lever, NOT a determinism one — see the setting's comment.
            thinking_budget=settings.ARCHIVE_READ_THINKING_BUDGET or None,
            cached_content=cached_content,
            # The id-list output grows with the archive; thinking counts
            # inside the cap. 2000 truncated mid-JSON at 188 ids (live).
            max_output_tokens=settings.ARCHIVE_READ_MAX_TOKENS,
            # The whole-archive read outlives the 30s guard meant for short
            # classifier calls — see ARCHIVE_READ_TIMEOUT_SECONDS.
            timeout=settings.ARCHIVE_READ_TIMEOUT_SECONDS or None,
        )

    try:
        # Phase B (GEMINI_CONTEXT_CACHING_PLAN): reference the per-producer
        # explicit cache when active. gemini_cache is fail-soft throughout —
        # off/missing/expired/errored all degrade to the plain uncached call.
        if (
            settings.GEMINI_CONTEXT_CACHE == "on"
            and group_id is not None
            and archive_version is not None
        ):
            from app.services import gemini_cache

            raw, _used_cache = await gemini_cache.read_with_cache(
                group_id, archive_version, system_prompt, _call
            )
        else:
            raw = await _call()
    except Exception as e:
        # error, not warning: this is now a reportable failure rather than a
        # quiet degradation, and it is the line to look for when a listener
        # says the archive claimed to have nothing.
        logger.error(f"Archive-read LLM call FAILED (reported as transient): {e}")
        return ArchiveRead(unit_ids=[], failed=True)
    # `clarify` is only ever read when the prompt actually offered it. A model
    # that invents the key on an archive with no same-named people is
    # returning something it was never told about, and honouring it would turn
    # an answerable question into a question back.
    clarify = _parse_clarify(raw) if disambiguation else None
    return ArchiveRead(
        unit_ids=(parsed := _parse_unit_selection(raw))[0],
        malformed_ids=parsed[1],
        follow_up=_parse_follow_up(raw),
        clarify=clarify,
        about=_parse_about(raw),
    )


# ── Step 4: deterministic validation + word-boundary snapping (no LLM) ──────


#: A pause long enough to separate two PASSAGES inside one recording, in
#: seconds. A deliberate constant, unlike the per-recording word-gap
#: percentile that cuts units: passage breaks per recording are far too few
#: to characterise a distribution from, so a derived threshold here would be
#: noise wearing a percentile's clothes. Measured on the live archive
#: (2026-08-10): unit gaps INSIDE every real passage are <= 1.6s, and the one
#: real passage boundary — family enumeration -> the uncle story in
#: RECORDING 14 — is a 3.3s pause. Too small only under-fills (today's
#: behaviour); too large lets an unrelated sub-topic of the SAME recording
#: about the SAME person ride along — a bounded miss, never a wrong person.
_PASSAGE_GAP_SECONDS = 2.0


def _expand_about_passages(
    unit_ids: List[str],
    raw_about: Optional[str],
    entity_map: Dict[str, List[str]],
    units: List[UtteranceUnit],
) -> List[str]:
    """Complete the passages of an answer ABOUT someone — code, not prompt.

    THE GAP THIS CLOSES: "breadth falls out of the question" works through
    the interview-question anchor, and a person who appears INCIDENTALLY in
    recordings has no recording whose interview question is about them. So a
    generic "tell me about X" anchors only on the units that literally say
    X's name and comes back as fragments — "יש את אמנון שאני חבר שלו עד היום"
    with no air force, no three years, no antecedent for anything — while a
    SPECIFIC question matches passage content and gets the whole story. The
    323f88d prompt bullet attacked exactly this and was reverted for flipping
    an unrelated state-dependent turn onto the wrong person; this is the same
    intent as a deterministic post-step, where wording cannot leak.

    WHY IT CANNOT CROSS A PERSON BOUNDARY, which is the bar the bullet
    failed: units are only ever ADDED from a recording that (a) the model
    already selected from, and (b) the archive itself attributes to the named
    entity (`entity_map`, the same mention data the name tags are built
    from). Expansion never reaches into a recording the model did not pick,
    so it can amplify an answer but never redirect one — the live confusion
    was the model SELECTING the other אמנון's recordings, and nothing here
    selects. A bare "אמנון" naming the friend while the selection sits in the
    uncle's recording intersects to nothing and is a no-op.

    Within a qualifying recording, the selection grows to the whole PASSAGE
    around each selected unit — contiguous units up to the nearest pause
    longer than _PASSAGE_GAP_SECONDS — so the uncle's story grows to the
    uncle's story, not to the family enumeration sharing his recording.

    Everything downstream inherits the expansion for free: clips, the spoken
    text, shown-unit metadata and follow-up validation all read the expanded
    list, so they cannot drift from what actually plays.
    """
    if not unit_ids:
        return unit_ids
    resolved = _resolve_about(raw_about, entity_map)
    if resolved is None:
        return unit_ids
    name, segment_ids = resolved

    by_id = {u.unit_id: u for u in units}
    selected_segments = {by_id[uid].segment_id for uid in unit_ids if uid in by_id}
    expandable = set(segment_ids) & selected_segments

    passage_of: Dict[str, List[UtteranceUnit]] = {}
    for seg_id in expandable:
        seg_units = [u for u in units if u.segment_id == seg_id]
        start = 0
        for i in range(1, len(seg_units)):
            if seg_units[i].start_sec - seg_units[i - 1].end_sec > _PASSAGE_GAP_SECONDS:
                for u in seg_units[start:i]:
                    passage_of[u.unit_id] = seg_units[start:i]
                start = i
        for u in seg_units[start:]:
            passage_of[u.unit_id] = seg_units[start:]

    out: List[str] = []
    emitted: set = set()
    for uid in unit_ids:
        passage = passage_of.get(uid)
        if passage is None:
            # Outside the named person's recordings (or an unknown id, which
            # resolve_units_to_clips drops with its own warning) — kept as the
            # model ordered it, untouched.
            if uid not in emitted:
                emitted.add(uid)
                out.append(uid)
            continue
        for u in passage:
            if u.unit_id not in emitted:
                emitted.add(u.unit_id)
                out.append(u.unit_id)

    added = [uid for uid in out if uid not in set(unit_ids)]
    if added:
        logger.info(
            f"Completed passages about {name!r}: {unit_ids} + {added}"
        )
    return out


def _group_selected_runs(
    selected: List[UtteranceUnit],
) -> List[List[UtteranceUnit]]:
    """Group already-resolved units into CONTIGUOUS RUNS of original speech.

    THE shared definition of "continuous" for every renderer — the video
    path merges each run into one clip, the spoken path (avatar mode) joins
    a run's texts as flowing speech and places bridges only BETWEEN runs —
    so what counts as uninterrupted can never drift between the two
    (docs/AVATAR_SHARED_ENGINE_PLAN.md §1.2).

    A unit joins the current run iff it has the same segment AND the next
    global index relative to the PREVIOUS unit in selection order, strictly
    forward — a reversed pair (u2 then u1) is two runs, and index adjacency
    across a segment boundary never merges (global indices are contiguous
    across recordings; segments are not)."""
    runs: List[List[UtteranceUnit]] = []
    prev: Optional[UtteranceUnit] = None
    for unit in selected:
        if (
            prev is not None
            and unit.segment_id == prev.segment_id
            and unit.index == prev.index + 1
        ):
            runs[-1].append(unit)
        else:
            runs.append([unit])
        prev = unit
    return runs


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
    preserved; a run of consecutive units (per _group_selected_runs, the
    grouping shared with the spoken renderer) plays as one uninterrupted
    clip — intra-run pauses included, so they play naturally — and
    non-consecutive selections stay separate pieces."""
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

    return [
        ExpandedClip(
            raw_segment_id=run[0].segment_id,
            start_sec=run[0].start_sec,
            end_sec=run[-1].end_sec,
            # No single source chunk in this mode — a clip can span several.
            # Marked so it's never mistaken for a v1 chunk id.
            source_chunk_id=f"archive-read:{run[0].segment_id}",
        )
        for run in _group_selected_runs(selected)
    ]


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
    # {"question": str, "options": [str, ...]} when the question could have
    # meant either of two people who share a name. Mutually exclusive with an
    # answer: when this is set, `clips` is empty by construction (see
    # select_units) — a guess plus a question is the conflation this feature
    # exists to remove.
    clarify: Optional[dict] = None
    # The no-story line, already naming who the question was about, when that
    # could be established and checked against the archive. None means the
    # generic NO_STORY_FALLBACK — which is still the right answer for a
    # question about nobody in particular.
    no_story_text: Optional[str] = None
    # The archive read did not happen — an API failure, not an answer. Callers
    # MUST NOT present this as "nothing in the archive answers that".
    read_failed: bool = False


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
    archive, entity_map, units, name_tags = archive_bundle
    if not archive or not units:
        return UnitSelection([], [], False)

    shown_keys, per_turn_units = shown

    # Semantic answer cache (answer_cache.py): with the toggle off this is
    # a provable no-op (no embedding call, no DB read — pinned by test).
    # A hit reconstructs the EXACT downstream contract a fresh read builds:
    # clips from resolve_units_to_clips, the offer through the same
    # _validate_follow_up gate. ac_embedding doubles as the store gate —
    # non-None exactly when this turn is cacheable.
    ac_version = _ARCHIVE_CACHE[group_id].version if group_id in _ARCHIVE_CACHE else None
    # Speculative follow-up prefetch (milestone 2): exact-match, session-
    # scoped, one-shot — computed WITH this session's context the moment the
    # offer shipped, so it may serve into a turn that has shown-state. Its
    # offer still passes _validate_follow_up against CURRENT shown keys.
    ac_hit = await answer_cache.take_speculative(
        question, group_id, session_id, ac_version, units
    )
    ac_embedding = None
    if ac_hit is None:
        ac_embedding, ac_hit = await answer_cache.try_lookup(
            question, group_id, ac_version, units, name_tags, shown_keys, turns
        )
    if ac_hit is not None:
        hit_by_id = {u.unit_id: u for u in units}
        return UnitSelection(
            clips=resolve_units_to_clips([u.unit_id for u in ac_hit.units], units),
            selected_units=ac_hit.units,
            follow_up=_validate_follow_up(
                ac_hit.raw_follow_up, hit_by_id, ac_hit.units, shown_keys
            ),
        )

    # Recording pre-filter (PREFILTER_PLAN): None = no filtering, and the
    # f_* views below are the originals — byte-identical rendering. Active
    # only when the archive exceeds the budget; unit ids/keys are global
    # and untouched either way (filtering happens after _build_units).
    bundle_version = (
        _ARCHIVE_CACHE[group_id].version if group_id in _ARCHIVE_CACHE else None
    )
    pf = await prefilter.apply(
        question, session_id, archive, name_tags, shown_keys, bundle_version
    )
    if pf is None:
        f_archive, f_units, f_entity_map, f_tags = archive, units, entity_map, name_tags
    else:
        f_archive = [a for a in archive if a.segment.id in pf.admitted]
        f_units = [u for u in units if u.segment_id in pf.admitted]
        f_tags = {k: v for k, v in name_tags.items() if k in pf.admitted}
        f_entity_map = {
            n: kept
            for n, segs in entity_map.items()
            if (kept := [s for s in segs if s in pf.admitted])
        }

    # Phase A toggle (GEMINI_CONTEXT_CACHING_PLAN): under `message` the
    # transcript never carries per-turn marks — it is the stable cacheable
    # prefix — and the shown facts travel in the user message instead.
    inline_shown = settings.SHOWN_STATE_PLACEMENT != "message"
    transcript_block = _format_annotated_transcript(
        f_archive, f_units, shown_keys if inline_shown else set(), f_tags
    )
    shown_block = (
        "" if inline_shown else _format_shown_block(f_archive, f_units, shown_keys)
    )
    # Same ordinals the transcript block just printed — see _recording_ordinals.
    entity_map_block = _format_entity_map(
        f_entity_map, _recording_ordinals(f_archive, f_units)
    )
    history_block = (
        _format_history_block(
            turns,
            per_turn_units,
            # Full (unfiltered) map: history may reference units outside the
            # admitted set; their stored ids still resolve.
            {_unit_key(u.segment_id, u.start_sec): u.unit_id for u in units},
        )
        if turns
        else ""
    )

    read = await _read_archive_for_ranges(
        question,
        transcript_block,
        entity_map_block,
        history_block,
        recording_language,
        disambiguation=bool(f_tags),
        shown_block=shown_block,
        group_id=group_id,
        # The version the bundle was just validated/rebuilt against — read
        # from the in-process cache to avoid a second aggregate query. A
        # cold miss (None) simply skips the explicit cache this turn. When
        # the pre-filter is active the admitted-set hash joins the cache
        # identity (PREFILTER_PLAN §1.2): a different pinned set is a
        # different prefix, so it must be a different cache.
        archive_version=(
            None
            if bundle_version is None
            else (tuple(bundle_version) + ((pf.set_hash,) if pf else ()))
        ),
    )
    if read.failed:
        # Straight out. Nothing below this line can say anything true about
        # the archive, because the archive was never consulted.
        return UnitSelection(clips=[], selected_units=[], read_failed=True)

    unit_ids, raw_follow_up, clarify, raw_about = (
        read.unit_ids, read.follow_up, read.clarify, read.about
    )

    # CLARIFY BLOCKS THE ANSWER. A best guess accompanied by "or did you mean
    # the other one?" is the conflation this exists to remove, wearing a
    # question mark — the listener still gets one person's footage presented
    # as the answer. So a clarification replaces the answer rather than
    # decorating it, and units are dropped even if the model sent some.
    if clarify:
        if unit_ids:
            logger.info(
                f"Archive read returned clarify AND {len(unit_ids)} units; "
                "dropping the units — a clarification replaces the answer"
            )
        return UnitSelection(clips=[], selected_units=[], clarify=clarify)

    # A non-empty selection about a named person is completed to whole
    # passages HERE, deterministically — see _expand_about_passages for why
    # this is code rather than prompt wording, and why it cannot reach
    # another person's recordings.
    unit_ids = _expand_about_passages(unit_ids, raw_about, f_entity_map, f_units)

    # Validation is against the ADMITTED units: the model can only cite what
    # it was shown, and an id from outside the admitted set (impossible in a
    # well-formed response) drops here rather than resolving blind.
    by_id = {u.unit_id: u for u in f_units}
    selected = [by_id[uid] for uid in dict.fromkeys(unit_ids) if uid in by_id]

    # Conversation-sizing (core_compression.py): code-gated on playable
    # duration; under the threshold this is a no-op and the flow below is
    # byte-identical. Over it, the isolated call narrows the served core
    # and its remainder becomes the offer candidate — which then passes
    # through the SAME validation as any offer (real units, not shown, no
    # overlap with the answer).
    selected, compressed_fu, was_compressed = await core_compression.maybe_compress(
        question, selected, recording_language,
        # RULE 1 data: each recording's interview category (question_id is
        # "<category>_<n>"; imports/legacy rows without one map to None and
        # are exempt from the category rule). RULE 3 data comes from
        # group_id (close-family lookup happens only above the threshold).
        categories={
            a.segment.id: (
                a.segment.question_id.rsplit("_", 1)[0]
                if a.segment.question_id
                else None
            )
            for a in f_archive
        },
        group_id=group_id,
    )
    if was_compressed:
        unit_ids = [u.unit_id for u in selected]
        if compressed_fu is not None:
            raw_follow_up = compressed_fu

    clips = resolve_units_to_clips(unit_ids, f_units)
    follow_up = _validate_follow_up(raw_follow_up, by_id, selected, shown_keys)
    # `about` has TWO readers, split by whether anything plays: with units it
    # drove the passage expansion above; with none it feeds the named no-story
    # line below. Never both.
    #
    # PREFILTER GUARD (PREFILTER_PLAN §2): a filtered read may never assert
    # archive-wide absence. The specific no-story line is allowed only when
    # no filtering happened, or the person's recordings were ALL admitted
    # and the ranking was trusted — resolved against the FULL entity map,
    # because the claim is about the whole archive.
    no_story_text = None
    if not clips:
        about_resolved = _resolve_about(raw_about, entity_map)
        if about_resolved is None or prefilter.covers_entity(pf, about_resolved[1]):
            no_story_text = _no_story_line(
                raw_about, entity_map, units, shown_keys, question
            )
    # Store a cacheable fresh-conversation answer (answer_cache.py).
    # ac_embedding is non-None exactly when the lookup gates passed and
    # missed; clarify/failed paths returned earlier, empty answers are
    # skipped inside store(). The offer is stored as unit KEYS (ids
    # renumber per ingest; keys are the persistable identity).
    if ac_embedding is not None and clips:
        fu_store = None
        if follow_up and follow_up.get("unit_ids"):
            fu_units = [by_id[i] for i in follow_up["unit_ids"] if i in by_id]
            fu_store = {
                "question": follow_up.get("question"),
                "unit_keys": [
                    _unit_key(u.segment_id, u.start_sec) for u in fu_units
                ],
            }
        await answer_cache.store(
            question, ac_embedding, group_id, ac_version, selected, fu_store
        )
    return UnitSelection(
        clips=clips,
        selected_units=selected,
        follow_up=follow_up,
        no_story_text=no_story_text,
    )


def _no_story_line(
    raw_about: Optional[str],
    entity_map: Dict[str, List[str]],
    units: List[UtteranceUnit],
    shown_keys: set,
    question: str,
) -> Optional[str]:
    """"I don't have another story about אמנון", or None for the generic line.

    WHETHER IT IS "ANOTHER" IS DECIDED HERE, not by the model, because the
    archive already knows: if any unit from this person's recordings has been
    played in this session then the honest sentence is "nothing MORE". Asking
    the model would be asking it to remember something the session record
    holds exactly.

    The wording is a stable function of the question, so the same question
    always produces the same sentence — a random pick would make the retrieval
    eval flaky for a reason that has nothing to do with retrieval.
    """
    resolved = _resolve_about(raw_about, entity_map)
    if resolved is None:
        return None
    name, segment_ids = resolved

    # NEVER ASSERT SOMETHING THE ARCHIVE CONTRADICTS.
    #
    # The model returning no units means "nothing here answers THAT question".
    # It does not mean the archive is out of material about this person, and
    # the difference is not the model's to judge — it is a fact we hold. Said
    # live: with five of אמנון's twelve units played, "אין לי עוד סיפור על
    # אמנון" went out while u11-u13, the whole army story, sat unplayed.
    #
    # The generic line is vague. This one was FALSE, and a specific falsehood
    # is worse than a vague truth: it tells the listener to stop asking about
    # someone the archive still has stories about. So when anything about this
    # person remains unseen, we say the vague thing.
    #
    # Deliberately NOT solved by playing the leftover units. "Never invent,
    # force, or approximate a selection to avoid an empty answer" (the Rules
    # section, and CLAUDE.md) applies exactly as much when the motive is a
    # nicer sentence.
    entity_units = [u for u in units if u.segment_id in set(segment_ids)]
    unseen = [
        u for u in entity_units if _unit_key(u.segment_id, u.start_sec) not in shown_keys
    ]
    if unseen:
        logger.info(
            f"Archive read found nothing for this question about {name!r}, but "
            f"{len(unseen)}/{len(entity_units)} of their units are unplayed — "
            "using the generic line rather than claiming there is nothing more"
        )
        return None

    return response_assembler.no_story_about(
        name, variant=zlib.crc32(question.encode("utf-8"))
    )


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
    # unit_ids are the validated survivors, exposed for SERVER-SIDE use
    # only (the eval harness's conservation metric, and any future offer
    # bookkeeping). The WS layer strips them before any client event —
    # clients get the question text, never raw ids.
    return {"question": raw["question"], "unit_ids": [u.unit_id for u in kept]}


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
    # Before the no-story branch, and that ordering is the whole point: a
    # clarification is an empty selection, so falling through would tell the
    # listener the archive has nothing about אמנון when it has two people by
    # that name and simply needs to know which.
    # Before every other branch: an outage is not an answer, and each branch
    # below asserts something about the archive that a failed read cannot
    # support.
    if selection.read_failed:
        return VideoClipResult(
            video_url=None,
            no_story=True,
            fallback_text=TRANSIENT_FAILURE_FALLBACK,
            read_failed=True,
        )
    if selection.clarify:
        return VideoClipResult(video_url=None, clarify=selection.clarify)
    if not clips:
        return VideoClipResult(
            video_url=None,
            no_story=True,
            # Names the subject when the archive could confirm one AND has
            # nothing left about them; otherwise the generic line, unchanged.
            fallback_text=selection.no_story_text or NO_STORY_FALLBACK,
            # THE OFFER SURVIVES AN EMPTY ANSWER. It never used to: this
            # branch dropped `follow_up` on the floor, so a turn that found no
            # direct answer but DID know about related material still said
            # only "I don't have a story about that". "No answer, but there is
            # this" is a different outcome from "no answer", and it is the one
            # the listener can act on.
            follow_up=selection.follow_up,
        )

    # Which life periods the answer draws on — computed from the SAME clips
    # that make the video, so the gallery can never disagree with the footage.
    photo_categories = await video_clip_assembler.photo_categories_for_segments(
        [c.raw_segment_id for c in clips]
    )

    cache_key = video_clip_assembler._clip_cache_key(group_id, clips)
    cached_url = await cache_service.get(cache_key)
    if cached_url:
        return VideoClipResult(video_url=cached_url, photo_categories=photo_categories)

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
        photo_categories=photo_categories,
        shown_units=[
            {
                "key": _unit_key(u.segment_id, u.start_sec),
                "unit_id": u.unit_id,
                "text": u.text,
            }
            for u in selection.selected_units
        ],
    )
