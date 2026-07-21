"""
Relevance scoring (Prompt 7) — the Generative Agents (Park et al. 2023)
memory-scoring formula, applied to retrieval_service.py's CANDIDATE
segments only (never the primary segment(s), which always proceed to
Prompt 8 unconditionally per the project plan):

    score = w_recency * recency_score + w_importance * importance_score
            + w_relevance * relevance_score

- recency_score: exponential decay based on how recently (minutes) this
  candidate's shared entities were last mentioned in the current
  conversation session (cache_service's entity-mention tracking, Prompt 2/
  7) — 0 if never mentioned this session. Computed in-code, no LLM call.
- importance_score: precomputed at ingestion time (analysis_graph.py's
  score_importance node, Prompt 5) — no LLM call here either.
- relevance_score: cosine similarity between the current question's
  embedding and the candidate's precomputed embedding (analysis_graph.py's
  embed_transcript node, Prompt 7) — a standard embedding model, not an LLM
  judge call.

Each raw score is min-max normalized to 0-1 across the current candidate
set before combining (per the original paper) — a candidate scored in
isolation has no "range" to normalize against, so this function operates on
the whole candidate list for one turn, not one candidate at a time.

Only candidates whose combined score clears RELEVANCE_THRESHOLD proceed to
Prompt 8 (response assembly). All constants below (weights + threshold +
decay rate) are deliberately plain module-level values, not settings —
Prompt 10's QA harness is where they get tuned against real retrieval
quality, not guessed here.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import RawSegment
from app.services import embeddings, graph_memory
from app.services.cache import cache_service
from app.services.retrieval_service import _short_summary

logger = logging.getLogger(__name__)

# Tunable constants — see module docstring.
W_RECENCY = 1.0
W_IMPORTANCE = 1.0
W_RELEVANCE = 1.0
RELEVANCE_THRESHOLD = 1.0  # out of a max combined score of w_recency+w_importance+w_relevance = 3.0

# Per-minute exponential decay for recency_score — higher means "older
# mentions stop mattering faster". 0.1 gives roughly a ~7-minute half-life,
# reasonable for a single conversation's turn-taking pace.
RECENCY_DECAY_PER_MINUTE = 0.1


@dataclass
class ScoredSegment:
    segment_id: str
    summary: str
    score: float
    recency_score: float
    importance_score: float
    relevance_score: float


def _min_max_normalize(values: List[float]) -> List[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        # No spread to normalize against — everyone's maximally-scored if
        # there's any real signal at all, otherwise no signal for anyone.
        return [1.0 if hi > 0 else 0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


async def _recency_raw_score(entity_names: List[str], session_id: str) -> float:
    """0 if none of `entity_names` were mentioned this session; otherwise
    exponential decay from the MOST RECENT of their last-mention times."""
    if not entity_names:
        return 0.0
    mentions = await cache_service.get_entity_last_mentioned(session_id, entity_names)
    if not mentions:
        return 0.0
    now = time.time()
    most_recent_minutes_ago = min((now - ts) / 60 for ts in mentions.values())
    return math.exp(-RECENCY_DECAY_PER_MINUTE * max(0.0, most_recent_minutes_ago))


async def _embed_question(question: str) -> Optional[List[float]]:
    try:
        return await embeddings.embed_text(question)
    except Exception as e:
        logger.warning(f"Question embedding failed, relevance_score will default to 0: {e}")
        return None


async def score_candidates(
    question: str,
    candidate_segment_ids: List[str],
    session_id: str,
    group_id: str,
) -> List[ScoredSegment]:
    """Score and filter retrieval_service.py's candidate segments. Returns
    only candidates that cleared RELEVANCE_THRESHOLD, sorted by score
    descending."""
    if not candidate_segment_ids:
        return []

    question_embedding = await _embed_question(question)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(RawSegment).where(RawSegment.id.in_(candidate_segment_ids))
        )
        segments_by_id = {seg.id: seg for seg in result.scalars().all()}

    raw = []
    for sid in candidate_segment_ids:
        seg = segments_by_id.get(sid)
        if seg is None:
            continue
        try:
            entity_names = await graph_memory.get_episode_entity_names(sid, group_id=group_id)
        except Exception as e:
            logger.warning(f"Could not fetch entities for segment {sid}: {e}")
            entity_names = []

        recency = await _recency_raw_score(entity_names, session_id)
        importance = seg.importance_score if seg.importance_score is not None else 0.0
        relevance = embeddings.cosine_similarity(question_embedding, seg.embedding)

        raw.append(
            {
                "segment_id": sid,
                "summary": _short_summary(seg.transcript),
                "recency": recency,
                "importance": importance,
                "relevance": relevance,
            }
        )

    if not raw:
        return []

    norm_recency = _min_max_normalize([r["recency"] for r in raw])
    norm_importance = _min_max_normalize([r["importance"] for r in raw])
    norm_relevance = _min_max_normalize([r["relevance"] for r in raw])

    scored: List[ScoredSegment] = []
    for item, nr, ni, nv in zip(raw, norm_recency, norm_importance, norm_relevance):
        combined = W_RECENCY * nr + W_IMPORTANCE * ni + W_RELEVANCE * nv
        scored.append(
            ScoredSegment(
                segment_id=item["segment_id"],
                summary=item["summary"],
                score=combined,
                recency_score=nr,
                importance_score=ni,
                relevance_score=nv,
            )
        )

    scored.sort(key=lambda s: s.score, reverse=True)
    return [s for s in scored if s.score >= RELEVANCE_THRESHOLD]
