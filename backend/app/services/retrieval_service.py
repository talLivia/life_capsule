"""
Real-time retrieval pipeline (Prompt 6) — called when the avatar receives a
question during a live conversation with a family member (Prompt 9).

1. `primary_match` — three independent signals, unioned:
   a. TOPIC: classify the question's topic (a lightweight Claude/Gemini
      call, temperature=0) and deterministically find 'ready' segments in
      Postgres, scoped to this producer's archive only, whose topic_tags
      include that topic. A plain set-membership check, not another LLM
      judgment.
   b. ENTITY: extract any person/place named directly in the question
      (a second lightweight call, run concurrently with (a)), fuzzy-
      resolve each against the graph's actual entity nodes
      (`graph_memory.names_are_similar` — the same gate Prompt 5 uses at
      ingestion time), and find segments that directly MENTION them
      (`graph_memory.find_related_episodes`, max_hops=1). This exists
      because topic_tags are thematic ("military service"), never person
      names — a question naming someone directly ("tell me about Gila")
      must still find their segments even when its overall theme doesn't
      happen to overlap with how that segment was tagged at ingestion
      time. Added in Prompt 10 after the QA harness surfaced exactly this
      gap against real data.
   c. SEMANTIC: cosine similarity between the question's embedding and
      each ready segment's precomputed transcript embedding (the exact
      same mechanism Prompt 7's relevance_scorer.py already uses for
      candidate scoring, `embeddings.py`) — catches phrasing/synonym gaps
      exact topic-tag matching misses (e.g. "tell me about your wedding"
      vs a segment topic-tagged "marriage"/"relationship": the topic
      classifier's output for THIS phrasing just doesn't happen to
      string-match the stored tag, even though the underlying topic is
      obviously the same). SEMANTIC_MATCH_THRESHOLD was picked from real
      cosine-similarity numbers measured against Prompt 10's QA questions,
      not guessed — see the constant's own comment.
2. `expand_graph` — pull the entities Graphiti recorded for the primary
   segment(s) (`graph_memory.get_episode_entity_names`) and call
   `find_related_episodes_scored` (Prompt 3/6) to find other segments
   sharing those entities, up to `max_hops` out, excluding whatever this
   conversation session already surfaced (`session_visited_set` — Upstash
   Redis via cache_service, Prompt 2).
3. Only candidates meeting MIN_SHARED_ENTITY_COUNT survive — Graphiti's
   MENTIONS-based traversal has no numeric edge weight, so "how many of the
   primary segment(s)' entities does this candidate actually share" is the
   confidence proxy (see `find_related_episodes_scored`'s docstring).
4. Capped to MAX_CANDIDATES per turn.

Returns primary segment(s) + up to MAX_CANDIDATES related candidates, each
as only a short summary (never the full transcript) — Prompt 7 (relevance
scoring) only needs that much, and Prompt 8 (response assembly) does its
own targeted re-fetch of full transcript text for whatever it actually uses.

This module never writes the visited-set — only Prompt 8 knows which
segments actually made it into the assembled response, so updating Redis
with "all segment ids used" is explicitly its job (per the project plan),
not this read-only lookup's.

KNOWN LIMITATION (intentional, not a bug — revisit only if retrieval scope
expands): a question referencing someone purely by role/relationship
without naming them (e.g. "who was your commander?", "tell me about your
boss") gets none of primary_match's three signals for free — ENTITY finds
nothing because no proper name was mentioned, and it falls to TOPIC/
SEMANTIC alone. If the segment's transcript never happens to use that
same role word, and the question's topic/semantic similarity doesn't
independently clear their thresholds either, the question gets the
no-story fallback even though a human reading the transcript would
recognize who's meant. This is the deliberate boundary of the project's
"never invent or guess" principle (see response_assembler.py's zero-LLM-
call guarantee and NO_STORY_FALLBACK) — resolving an unnamed role to a
specific real person would require exactly the kind of inference this
project avoids throughout. Fixing it would mean either LLM-inferring "your
commander" -> a specific graph entity from context (an inference, not a
lookup) or teaching entity extraction to also capture role words at
ingestion time (a bigger, separate design change) — neither attempted
here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import InterviewSession, Message, RawSegment, TranscriptChunk
from app.services import embeddings, graph_memory
from app.services.cache import cache_service
from app.services.llm import llm_service

logger = logging.getLogger(__name__)

# Tunable constants — Prompt 10's QA harness is where these get tuned for
# real against actual retrieval quality, not guessed here.
MIN_SHARED_ENTITY_COUNT = 1  # conservative default: any real shared entity counts
MAX_CANDIDATES = 2
_SUMMARY_MAX_CHARS = 160

# How many of the most recent Message rows (both roles) _resolve_coreferences
# looks at — see that function's own docstring for why 2 (the last user+
# assistant pair), not the full MAX_CONTEXT_MESSAGES window websocket.py
# rehydrates for UI continuity: recency, not breadth, is what resolving a
# reference needs.
COREFERENCE_HISTORY_TURNS = 2

# Sanity cap on primary_match/primary_match_chunks' own result count,
# independent of the coreference fix above — confirmed live: an ambiguous,
# under-specified question (a bare pronoun follow-up, before the
# coreference fix existed) can carry no real topic/entity/semantic signal
# of its own, and primary_match_chunks' second-pass leniency (relaxed
# semantic threshold) then matched ALL 12 of 12 chunks in a real archive —
# strictly worse than matching nothing, since it would flood Prompt 13's
# per-candidate verification with the entire archive instead of failing
# cleanly. A future ambiguous question could still trigger this even after
# coreference resolution (e.g. a question that's simply too vague for any
# signal to apply), so this cap stands on its own: if a single pass's
# match count exceeds it, treat the match as untrustworthy and return
# nothing rather than flooding downstream — a legitimately broad question
# ("tell me about your childhood") could occasionally get capped away
# empty too; that's an acceptable false negative given the alternative.
MAX_PRIMARY_MATCHES = 6
# Raw cosine similarity (not min-max normalized like Prompt 7's combined
# score — there's no "candidate set" to normalize against here, just one
# question against every ready segment). Calibrated against real Gemini
# embeddings of Prompt 10's QA questions (all 30, against all 3 sample
# segments): genuinely unrelated pairs (childhood/hobbies/travel/cooking/
# pets/sports/food/parents/weekend/music questions vs any of the 3
# segments) topped out at 0.654; the wedding/wife phrasing-gap questions
# this threshold exists to catch scored 0.696-0.736 against the marriage
# segment. 0.68 sits in that gap with a real margin above the unrelated
# ceiling. One edge case ("מה הרגשת ביום החתונה?" / "how did you feel on
# your wedding day", 0.647) lands BELOW even the unrelated ceiling and
# stays unmatched by this signal — a genuine embedding-similarity limit,
# not something a single global threshold can fix without risking false
# positives on unrelated questions elsewhere; it may still match via the
# topic or entity signals depending on phrasing.
SEMANTIC_MATCH_THRESHOLD = 0.68

# Prompt 12 (original-video-clip mode, parallel to the above — never used by
# avatar-mode primary_match). Same embedder, same threshold as a starting
# point: chunk text is shorter than a whole segment's, so cosine-similarity
# behavior COULD differ, but re-tuning it needs its own QA-harness-style
# pass against real chunk-level data (Prompt 10 did this for
# SEMANTIC_MATCH_THRESHOLD; nothing analogous exists yet for chunks) —
# flagging rather than guessing a different number.
SEMANTIC_CHUNK_MATCH_THRESHOLD = SEMANTIC_MATCH_THRESHOLD

_TOPIC_CLASSIFY_SYSTEM_PROMPT_TEMPLATE = """\
You are a strict topic classifier for a personal life-story archive \
retrieval system. Given a question someone is asking about the \
storyteller's life, output ONLY a single short topic tag (1-3 words, \
lowercase, in {language}) describing what the question is actually \
about - using the same tagging style as segment classification: e.g. \
military service, childhood, family, career, loss, friendship. Do not \
include any commentary or text outside the single tag."""

_ENTITY_NAME_QUESTION_SYSTEM_PROMPT = """\
You are a strict named-entity extractor for a personal life-story \
archive retrieval system. Given a QUESTION someone is asking about the \
storyteller's life, output ONLY a JSON array of distinct proper names of \
PEOPLE or PLACES explicitly named IN THE QUESTION ITSELF - never a role, \
relationship, or description that merely implies an unnamed person (e.g. \
"your commander", "your wife", "your manager"), even if a specific \
person is obviously meant. Written exactly as they appear in the \
question (same language/script). If no proper name is mentioned, output \
an empty array: []. Do not include any commentary or text outside the \
JSON array. Example: question "Tell me about Gila" -> ["Gila"]. Question \
"Who was your commander?" -> []."""

# Prompt 12: a family member's question is naturally 2nd person ("what did
# you do for work?") or 3rd person about the storyteller, but the
# storyteller's own transcripts (and every chunk embedding/topic tag
# computed from them) are first-person narration ("I worked as a
# carpenter"). Hebrew verb conjugation inflects person/gender directly in
# the verb (not just a separate pronoun the way English does), so a plain
# regex/pronoun substitution is fragile across the full range of Hebrew
# verb forms - an LLM rewrite (temperature=0, same "lightweight structured
# task" pattern as the topic/entity calls above) handles this reliably
# instead. Search-purposes ONLY: the caller keeps the ORIGINAL question for
# Prompt 13, which must answer what was actually asked, not this rewrite.
_PERSPECTIVE_NORMALIZE_SYSTEM_PROMPT_TEMPLATE = """\
You are rewriting a question for a search system - you are not answering \
it. The question is addressed TO a storyteller (2nd person, e.g. "what \
did you do for work?") or asks ABOUT them (3rd person, e.g. "what did he \
do for work?"), but the storyteller's own life-story transcripts are \
narrated in FIRST PERSON (e.g. "I worked as a carpenter"). Rewrite the \
question as if the storyteller were asking it about themselves, in FIRST \
PERSON, in {language}, preserving its exact original meaning - do not \
answer it, add information, or change anything except the grammatical \
person/verb conjugation. Output ONLY the rewritten question, no \
commentary, no quotation marks. Example: "מה עבדת?" -> "מה עבדתי?". \
Example: "what did you do for work?" -> "what did I do for work?"."""


@dataclass
class RetrievedSegment:
    segment_id: str
    summary: str


@dataclass
class RetrievalResult:
    primary: List[RetrievedSegment] = field(default_factory=list)
    candidates: List[RetrievedSegment] = field(default_factory=list)


def _short_summary(transcript: Optional[str]) -> str:
    """A short, deterministic preview — never the full transcript. Prompt 8
    re-fetches full text for whichever segments are actually used."""
    text = (transcript or "").strip()
    if len(text) <= _SUMMARY_MAX_CHARS:
        return text
    return text[:_SUMMARY_MAX_CHARS].rsplit(" ", 1)[0] + "…"


async def _classify_topic(question: str, recording_language: str) -> Optional[str]:
    """Single lightweight Claude/Gemini call, temperature=0 (deterministic)
    — per the Prompt 6 spec, the only inference step in this whole
    pipeline. Output must use the same tag vocabulary/language as Prompt 5's
    extract_topics so the plain set-membership check in primary_match can
    actually overlap."""
    system_prompt = _TOPIC_CLASSIFY_SYSTEM_PROMPT_TEMPLATE.format(language=recording_language)
    try:
        raw = await llm_service.generate_response(
            messages=[{"role": "user", "content": question}],
            system_prompt=system_prompt,
            temperature=0,
        )
    except Exception as e:
        logger.warning(f"Topic classification failed: {e}")
        return None
    topic = raw.strip().strip('"').strip("'").lower()
    return topic or None


def _parse_json_array(text: str) -> List[str]:
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


async def _extract_entity_names_from_question(question: str) -> List[str]:
    """A second lightweight call, run concurrently with _classify_topic: a
    question naming someone directly ("tell me about Gila") must find their
    segments even when the question's overall THEME doesn't happen to
    overlap with how that segment was topic-tagged at ingestion time —
    topic_tags are thematic ("military service"), never person names.
    Mirrors analysis_graph.py's ingestion-time entity extraction, but tuned
    for a short question instead of a full transcript: an implied-but-
    unnamed role ("your commander") must NOT be treated as a name."""
    try:
        raw = await llm_service.generate_response(
            messages=[{"role": "user", "content": question}],
            system_prompt=_ENTITY_NAME_QUESTION_SYSTEM_PROMPT,
            temperature=0,
        )
    except Exception as e:
        logger.warning(f"Entity-name extraction failed for question: {e}")
        return []
    return _parse_json_array(raw)


async def _embed_question_for_primary_match(question: str) -> Optional[List[float]]:
    """Same embedder Prompt 7's relevance_scorer.py uses (embeddings.py) —
    degrades to "no semantic signal" rather than failing primary_match
    outright, matching how the topic/entity signals already degrade."""
    try:
        return await embeddings.embed_text(question)
    except Exception as e:
        logger.warning(f"Question embedding failed for semantic primary match: {e}")
        return None


async def _resolve_entity_names(names: List[str], group_id: str) -> List[str]:
    """Fuzzy-resolve each name extracted from the question against the
    graph's actual entity nodes (graph_memory.names_are_similar — the same
    token-aware lexical gate Prompt 5 uses at ingestion time), so e.g.
    "גילה" still finds segments even if the graph's canonical node ended
    up named "גילה כהן" after a human-in-the-loop resolution. Returns the
    graph's own node names (not the raw extracted text) — find_related_
    episodes matches exactly, so it needs the name as the graph knows it."""
    resolved: set = set()
    for name in names:
        candidates = await graph_memory.get_entity_candidates(name, group_id=group_id)
        for c in candidates:
            if graph_memory.names_are_similar(name, c["name"]):
                resolved.add(c["name"])
    return list(resolved)


# ── Coreference resolution — shared by primary_match and primary_match_chunks ──
#
# Confirmed live (real archive, real LLM calls, no mocks): a follow-up
# question like "did you love her?" right after "who is Gila?" carries no
# topic/entity/semantic signal of its own once asked in isolation — exactly
# what primary_match/primary_match_chunks did before this fix, since neither
# has ever taken conversation history into account. Observed failure modes
# on real data: a bare pronoun ("is he still alive?" right after "who most
# influenced you as a child?") matched either nothing, or — via
# primary_match_chunks' second-pass leniency — ALL 12 of 12 chunks in the
# whole archive (see MAX_PRIMARY_MATCHES above for the guard against that).
#
# This runs BEFORE _normalize_to_first_person: resolving "her" -> "Gila"
# doesn't depend on grammatical person, and perspective normalization should
# act on the ALREADY-resolved text, not the other way around.

_COREFERENCE_RESOLVE_SYSTEM_PROMPT_TEMPLATE = """\
You are rewriting a question for a search system - you are not answering \
it. Below is the most recent part of a conversation. The NEW QUESTION may \
refer back to something mentioned in it using a pronoun or vague \
reference ("her", "him", "them", "it", "that", "he") instead of naming it \
directly. If so, rewrite the NEW QUESTION replacing ONLY that reference \
with the specific name/thing it refers to from the conversation below, in \
{language}, changing nothing else about the question. If the NEW QUESTION \
already stands on its own (no unresolved reference), or nothing in the \
conversation below actually resolves it, output the NEW QUESTION EXACTLY \
UNCHANGED. Never invent a name or detail that isn't actually present in \
the conversation below. Output ONLY the (possibly rewritten) question, no \
commentary, no quotation marks.

Recent conversation:
{history_block}"""


def _render_turn_for_history(role: str, content: str) -> str:
    """video_clip_assembler's assistant turns persist a raw video URL as
    `content` (there's no text caption of what the clip actually said) —
    feeding that to the coreference LLM call as if it were narration would
    be actively misleading rather than merely unhelpful. Rendered as a
    neutral placeholder instead; the antecedent we actually need ("Gila")
    almost always comes from the family member's OWN prior question anyway,
    not the assistant's reply."""
    if content.startswith("http://") or content.startswith("https://"):
        return f"{role}: (showed a video clip)"
    return f"{role}: {content}"


async def _recent_turns(session_id: str, limit: int) -> List[Dict[str, str]]:
    """Last `limit` Message rows for this session (both roles), oldest
    first — the same rehydration query websocket.py's _load_session_data
    already runs for UI continuity on reconnect, just windowed much
    smaller. Works identically for both the avatar and video-clip paths,
    since both persist every turn via ConnectionManager._persist_message."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Message.role, Message.content)
            .where(Message.session_id == session_id)
            .where(Message.role.in_(("user", "assistant")))
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        rows = list(result.all())[::-1]
    return [{"role": row.role, "content": row.content} for row in rows]


async def _resolve_coreferences(question: str, session_id: str, recording_language: str) -> str:
    """Rewrites a context-dependent follow-up ("did you love her?") into a
    self-contained question ("did you love Gila?") using the last
    COREFERENCE_HISTORY_TURNS messages of this session. Fail-soft to the
    ORIGINAL question — same contract as _normalize_to_first_person: no
    history yet (first turn of a session), an LLM failure, or the model
    finding nothing to resolve must never block retrieval or invent a
    reference that wasn't actually there."""
    history = await _recent_turns(session_id, COREFERENCE_HISTORY_TURNS)
    if not history:
        return question

    history_block = "\n".join(_render_turn_for_history(t["role"], t["content"]) for t in history)
    system_prompt = _COREFERENCE_RESOLVE_SYSTEM_PROMPT_TEMPLATE.format(
        language=recording_language, history_block=history_block
    )
    try:
        rewritten = await llm_service.generate_response(
            messages=[{"role": "user", "content": question}],
            system_prompt=system_prompt,
            temperature=0,
        )
    except Exception as e:
        logger.warning(f"Coreference resolution failed, using original question: {e}")
        return question
    rewritten = rewritten.strip().strip('"').strip("'")
    return rewritten or question


async def primary_match(
    question: str, group_id: str, recording_language: str, session_id: str
) -> List[RawSegment]:
    """Three independent signals, unioned — see module docstring:
    (a) classify the question's topic, deterministically matched against
    'ready' segments' topic_tags; (b) extract any person/place named
    directly in the question and find segments that mention them in the
    graph; (c) cosine similarity between the question's embedding and each
    ready segment's embedding, catching phrasing/synonym gaps (a) misses.
    Any signal alone is enough to surface a segment.

    `question` is resolved against recent conversation history FIRST (see
    _resolve_coreferences) — a bare pronoun follow-up otherwise carries none
    of these three signals at all."""
    question = await _resolve_coreferences(question, session_id, recording_language)

    topic, extracted_names, question_embedding = await asyncio.gather(
        _classify_topic(question, recording_language),
        _extract_entity_names_from_question(question),
        _embed_question_for_primary_match(question),
    )

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(RawSegment)
            .join(InterviewSession, RawSegment.interview_session_id == InterviewSession.id)
            .where(InterviewSession.user_id == group_id, RawSegment.status == "ready")
        )
        segments = result.scalars().all()

    matched: Dict[str, RawSegment] = {}

    if topic:
        for seg in segments:
            if seg.topic_tags and topic in {t.strip().lower() for t in seg.topic_tags}:
                matched[seg.id] = seg

    if extracted_names:
        resolved_names = await _resolve_entity_names(extracted_names, group_id)
        if resolved_names:
            entity_segment_ids = await graph_memory.find_related_episodes(
                entity_names=resolved_names, exclude_ids=[], group_id=group_id, max_hops=1
            )
            segments_by_id = {seg.id: seg for seg in segments}
            for sid in entity_segment_ids:
                if sid in segments_by_id:
                    matched[sid] = segments_by_id[sid]

    if question_embedding:
        for seg in segments:
            if (
                seg.embedding
                and embeddings.cosine_similarity(question_embedding, seg.embedding)
                >= SEMANTIC_MATCH_THRESHOLD
            ):
                matched[seg.id] = seg

    if len(matched) > MAX_PRIMARY_MATCHES:
        logger.warning(
            f"primary_match matched {len(matched)} segments (> MAX_PRIMARY_MATCHES="
            f"{MAX_PRIMARY_MATCHES}) for question {question!r} — treating as an "
            "untrustworthy match rather than flooding downstream scoring/assembly."
        )
        return []

    return list(matched.values())


async def expand_graph(
    primary_segments: List[RawSegment],
    session_visited_set: set,
    group_id: str,
    max_hops: int = 1,
) -> List[RetrievedSegment]:
    """Entities from the primary segment(s) -> related episodes, filtered by
    minimum shared-entity count and capped to MAX_CANDIDATES."""
    if not primary_segments:
        return []

    entity_names: set = set()
    for seg in primary_segments:
        names = await graph_memory.get_episode_entity_names(seg.id, group_id=group_id)
        entity_names.update(names)

    if not entity_names:
        return []

    # Exclude both the session's visited-set AND the primary segments
    # themselves — a primary segment trivially "shares" its own entities and
    # must never reappear as one of its own related candidates.
    exclude_ids = set(session_visited_set) | {seg.id for seg in primary_segments}
    scored = await graph_memory.find_related_episodes_scored(
        entity_names=list(entity_names),
        exclude_ids=list(exclude_ids),
        max_hops=max_hops,
        group_id=group_id,
        limit=MAX_CANDIDATES * 4,  # headroom before the threshold+cap below
    )

    qualifying = [c for c in scored if c["shared_entity_count"] >= MIN_SHARED_ENTITY_COUNT]
    top_ids = [c["segment_id"] for c in qualifying[:MAX_CANDIDATES]]
    if not top_ids:
        return []

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(RawSegment).where(RawSegment.id.in_(top_ids)))
        segments_by_id = {seg.id: seg for seg in result.scalars().all()}

    # Preserve score-ranked order; a candidate could be absent if it was
    # deleted/re-recorded since the graph link was made.
    return [
        RetrievedSegment(segment_id=sid, summary=_short_summary(segments_by_id[sid].transcript))
        for sid in top_ids
        if sid in segments_by_id
    ]


async def retrieve(
    question: str, group_id: str, recording_language: str, session_id: str
) -> RetrievalResult:
    """Orchestrates the full Prompt 6 pipeline for one incoming question."""
    primary_segments = await primary_match(question, group_id, recording_language, session_id)
    if not primary_segments:
        return RetrievalResult()

    visited = await cache_service.get_visited(session_id)
    candidates = await expand_graph(primary_segments, visited, group_id)

    return RetrievalResult(
        primary=[
            RetrievedSegment(segment_id=seg.id, summary=_short_summary(seg.transcript))
            for seg in primary_segments
        ],
        candidates=candidates,
    )


# ── Prompt 12: chunk-level retrieval for the original-video-clip mode ───────
#
# Everything below is a PARALLEL path for the new video-clip chat mode only
# — none of it is called by, or changes the behavior of, primary_match/
# expand_graph/retrieve above (the existing avatar path). Same three-signal
# union logic, operating over TranscriptChunk rows instead of whole
# RawSegment rows, using a first-person-normalized question (see
# _PERSPECTIVE_NORMALIZE_SYSTEM_PROMPT_TEMPLATE above for why).


async def _normalize_to_first_person(question: str, recording_language: str) -> str:
    """Search-purposes-only rewrite — fails soft to the ORIGINAL question
    (better than blocking retrieval on a transient LLM error) rather than
    raising."""
    system_prompt = _PERSPECTIVE_NORMALIZE_SYSTEM_PROMPT_TEMPLATE.format(
        language=recording_language
    )
    try:
        raw = await llm_service.generate_response(
            messages=[{"role": "user", "content": question}],
            system_prompt=system_prompt,
            temperature=0,
        )
    except Exception as e:
        logger.warning(f"Perspective normalization failed, using original question: {e}")
        return question
    normalized = raw.strip().strip('"').strip("'")
    return normalized or question


async def _load_ready_chunks(group_id: str) -> List[TranscriptChunk]:
    """Every TranscriptChunk belonging to a 'ready' segment in this
    producer's archive — mirrors primary_match's own RawSegment scoping
    join exactly (InterviewSession.user_id == group_id, status == 'ready')."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TranscriptChunk)
            .join(RawSegment, TranscriptChunk.raw_segment_id == RawSegment.id)
            .join(InterviewSession, RawSegment.interview_session_id == InterviewSession.id)
            .where(InterviewSession.user_id == group_id, RawSegment.status == "ready")
        )
        return list(result.scalars().all())


def _match_chunks(
    chunks: List[TranscriptChunk],
    topic: Optional[str],
    resolved_entity_names: List[str],
    question_embedding: Optional[List[float]],
    semantic_threshold: float,
) -> Dict[str, TranscriptChunk]:
    """Pure/synchronous: applies the three-signal union filter against an
    already-loaded chunk list and already-computed signals. Deliberately
    not async and takes no DB/LLM/embedding dependencies itself, so
    primary_match_chunks can call this twice (a strict pass, then a
    relaxed-semantic second pass) without redoing any of that work."""
    matched: Dict[str, TranscriptChunk] = {}

    if topic:
        for chunk in chunks:
            if chunk.topic_tags and topic in {t.strip().lower() for t in chunk.topic_tags}:
                matched[chunk.id] = chunk

    if resolved_entity_names:
        resolved_lower = {n.strip().lower() for n in resolved_entity_names}
        for chunk in chunks:
            mentioned = chunk.mentioned_entities or []
            if resolved_lower & {n.strip().lower() for n in mentioned}:
                matched[chunk.id] = chunk

    if question_embedding:
        for chunk in chunks:
            if (
                chunk.embedding
                and embeddings.cosine_similarity(question_embedding, chunk.embedding)
                >= semantic_threshold
            ):
                matched[chunk.id] = chunk

    return matched


async def primary_match_chunks(
    question: str, group_id: str, recording_language: str, session_id: str
) -> List[TranscriptChunk]:
    """Chunk-level parallel to primary_match, for the video-clip mode only —
    same three independent signals, unioned: (a) topic classification
    against each chunk's own topic_tags (Prompt 11); (b) entity names
    extracted from the question, fuzzy-resolved against the graph, matched
    against each chunk's mentioned_entities (Prompt 11's substring-tagged
    traceability field); (c) cosine similarity between the question's
    embedding and each chunk's own (contextually-computed) embedding.

    `question` is resolved against recent conversation history FIRST (see
    _resolve_coreferences), THEN first-person-normalized for the three
    signals above — the caller keeps the true original question text for
    Prompt 13 (coreference-resolved, but not perspective-flipped, since
    Prompt 13 needs to know who "her" was, not answer in the storyteller's
    own grammatical voice).

    Second-pass leniency (Prompt 12 step 3): if the strict pass matches
    NOTHING across all three signals, retry once with the semantic bar
    relaxed to half — surfacing borderline candidates a strict first pass
    filtered out. Only ever widens the net; never invents or fabricates a
    match. If still nothing, the caller gets an empty list — Prompt 13's
    NO_STORY_FALLBACK-equivalent path is what eventually handles that, not
    this function. MAX_PRIMARY_MATCHES caps BOTH passes: confirmed live,
    an under-specified question can make the relaxed second pass match the
    ENTIRE archive — worse than matching nothing.
    """
    question = await _resolve_coreferences(question, session_id, recording_language)
    normalized_question = await _normalize_to_first_person(question, recording_language)

    topic, extracted_names, question_embedding = await asyncio.gather(
        _classify_topic(normalized_question, recording_language),
        _extract_entity_names_from_question(normalized_question),
        _embed_question_for_primary_match(normalized_question),
    )
    resolved_names = await _resolve_entity_names(extracted_names, group_id) if extracted_names else []

    chunks = await _load_ready_chunks(group_id)

    matched = _match_chunks(
        chunks, topic, resolved_names, question_embedding, SEMANTIC_CHUNK_MATCH_THRESHOLD
    )

    if not matched and question_embedding:
        matched = _match_chunks(
            chunks, topic, resolved_names, question_embedding, SEMANTIC_CHUNK_MATCH_THRESHOLD / 2
        )

    if len(matched) > MAX_PRIMARY_MATCHES:
        logger.warning(
            f"primary_match_chunks matched {len(matched)} chunks (> MAX_PRIMARY_MATCHES="
            f"{MAX_PRIMARY_MATCHES}) for question {question!r} — treating as an "
            "untrustworthy match rather than flooding downstream verification."
        )
        return []

    return list(matched.values())


async def expand_graph_chunks(
    primary_chunks: List[TranscriptChunk],
    session_visited_set: set,
    group_id: str,
    max_hops: int = 1,
) -> List[TranscriptChunk]:
    """Chunk-level parallel to expand_graph, for the video-clip mode only.

    Added after review flagged a real gap: primary_match_chunks alone only
    surfaces chunks that directly clear the topic/entity/semantic bar
    against the QUESTION's own text — unlike expand_graph, which bridges
    to OTHER content sharing an entity with what already matched,
    regardless of whether that other content's own topic/semantic profile
    resembles the question. For a broad, multi-part question (e.g. "what
    did you do for work, did you enjoy it, tell me stories"), the vague
    follow-up clauses get no targeted signal of their own in EITHER path
    (the whole question is classified/embedded once as a blend) — but the
    avatar path gets a second chance to surface related content anyway via
    shared entities; the chunk path didn't, until now.

    Graphiti's entity/episode graph is per-SEGMENT (Prompt 11 didn't change
    that — a chunk has no graph presence of its own), so this still looks
    up entities via the primary chunks' PARENT segments, exactly like
    expand_graph. The difference: what gets returned is chunk-granular, not
    whole segments. For each segment expand_graph's own logic would have
    surfaced, only ITS chunks whose OWN mentioned_entities (Prompt 11)
    overlaps the bridging entity set are included — never a whole related
    segment's worth of chunks indiscriminately. A related segment where
    Graphiti's episode-level extraction found the shared entity but no
    individual chunk's substring-tagging happened to catch it contributes
    nothing here (fail-soft: no precise moment to point to beats guessing
    one)."""
    if not primary_chunks:
        return []

    entity_names: set = set()
    for chunk in primary_chunks:
        names = await graph_memory.get_episode_entity_names(chunk.raw_segment_id, group_id=group_id)
        entity_names.update(names)

    if not entity_names:
        return []

    # Exclude both the session's visited-set AND the primary chunks' OWN
    # parent segments — a primary chunk's segment trivially "shares" its
    # own entities and must never bridge back to itself.
    primary_segment_ids = {chunk.raw_segment_id for chunk in primary_chunks}
    exclude_ids = set(session_visited_set) | primary_segment_ids
    scored = await graph_memory.find_related_episodes_scored(
        entity_names=list(entity_names),
        exclude_ids=list(exclude_ids),
        max_hops=max_hops,
        group_id=group_id,
        limit=MAX_CANDIDATES * 4,  # headroom before the threshold+cap below
    )

    qualifying_segment_ids = [
        c["segment_id"] for c in scored if c["shared_entity_count"] >= MIN_SHARED_ENTITY_COUNT
    ]
    if not qualifying_segment_ids:
        return []

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TranscriptChunk).where(TranscriptChunk.raw_segment_id.in_(qualifying_segment_ids))
        )
        chunks_by_segment: Dict[str, List[TranscriptChunk]] = {}
        for chunk in result.scalars().all():
            chunks_by_segment.setdefault(chunk.raw_segment_id, []).append(chunk)

    entity_names_lower = {n.strip().lower() for n in entity_names}
    related_chunks: List[TranscriptChunk] = []
    # Preserve score-ranked segment order from `scored`; within a segment,
    # only chunks that textually mention one of the bridging entities.
    for sid in qualifying_segment_ids:
        for chunk in chunks_by_segment.get(sid, []):
            mentioned = {n.strip().lower() for n in (chunk.mentioned_entities or [])}
            if mentioned & entity_names_lower:
                related_chunks.append(chunk)

    return related_chunks[:MAX_CANDIDATES]


async def retrieve_chunks(
    question: str, group_id: str, recording_language: str, session_id: str
) -> List[TranscriptChunk]:
    """Chunk-level parallel to retrieve() — orchestrates primary_match_chunks
    + expand_graph_chunks for the video-clip mode. Unlike retrieve(), this
    returns one flat, deduplicated list rather than a primary/candidates
    split: per Prompt 13's own description ("for EACH matched chunk...
    relevance verification"), every chunk gets the same per-candidate LLM
    verification regardless of how it was found, unlike the avatar path
    where primary segments proceed unconditionally and only expand_graph's
    candidates get scored/filtered. Scoring (score_chunk_candidates) is
    deliberately NOT applied here either — mirroring retrieve() itself,
    which doesn't call score_candidates internally; that's the caller's
    job (response_assembler.py today; Prompt 13's assembly step for this
    mode)."""
    primary_chunks = await primary_match_chunks(question, group_id, recording_language, session_id)
    if not primary_chunks:
        return []

    visited = await cache_service.get_visited(session_id)
    related_chunks = await expand_graph_chunks(primary_chunks, visited, group_id)

    seen_ids = {chunk.id for chunk in primary_chunks}
    combined = list(primary_chunks) + [c for c in related_chunks if c.id not in seen_ids]
    return combined
