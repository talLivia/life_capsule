"""
Real-time retrieval pipeline (Prompt 6) — called when the avatar receives a
question during a live conversation with a family member (Prompt 9).

1. `primary_match` — classify the question's topic (a single lightweight
   Claude/Gemini call, temperature=0) and deterministically find 'ready'
   segments in Postgres, scoped to this producer's archive only, whose
   topic_tags include that topic. The classification is the only inference
   step; the match itself is a plain set-membership check, not another LLM
   judgment.
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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import InterviewSession, RawSegment
from app.services import graph_memory
from app.services.cache import cache_service
from app.services.llm import llm_service

logger = logging.getLogger(__name__)

# Tunable constants — Prompt 10's QA harness is where these get tuned for
# real against actual retrieval quality, not guessed here.
MIN_SHARED_ENTITY_COUNT = 1  # conservative default: any real shared entity counts
MAX_CANDIDATES = 2
_SUMMARY_MAX_CHARS = 160

_TOPIC_CLASSIFY_SYSTEM_PROMPT_TEMPLATE = """\
You are a strict topic classifier for a personal life-story archive \
retrieval system. Given a question someone is asking about the \
storyteller's life, output ONLY a single short topic tag (1-3 words, \
lowercase, in {language}) describing what the question is actually \
about - using the same tagging style as segment classification: e.g. \
military service, childhood, family, career, loss, friendship. Do not \
include any commentary or text outside the single tag."""


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


async def primary_match(
    question: str, group_id: str, recording_language: str
) -> List[RawSegment]:
    """Classify the question's topic, then deterministically find 'ready'
    segments in this producer's archive whose topic_tags include it."""
    topic = await _classify_topic(question, recording_language)
    if not topic:
        return []

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(RawSegment)
            .join(InterviewSession, RawSegment.interview_session_id == InterviewSession.id)
            .where(InterviewSession.user_id == group_id, RawSegment.status == "ready")
        )
        segments = result.scalars().all()

    return [
        seg
        for seg in segments
        if seg.topic_tags and topic in {t.strip().lower() for t in seg.topic_tags}
    ]


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
    primary_segments = await primary_match(question, group_id, recording_language)
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
