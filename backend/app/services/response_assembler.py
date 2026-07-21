"""
Bridge phrase generation and response assembly (Prompt 8) — the final step
of the retrieval pipeline (Prompts 6-8), producing the exact text the
avatar speaks (Prompt 9 hands this straight to TTS, nothing else).

`assemble_response(question, group_id, recording_language, session_id)`:
1. Runs retrieval_service.retrieve() (Prompt 6) and, for its candidates,
   relevance_scorer.score_candidates() (Prompt 7).
2. If no primary segment was found (the question's topic never matched
   anything in this producer's archive), returns a fixed fallback string —
   NEVER an LLM call to invent filler content.
3. Otherwise: the primary segment's transcript(s), VERBATIM. For each
   approved candidate (Prompt 7's already-filtered/capped output), a bridge
   phrase from a small fixed template bank with only the shared entity's
   name injected — never generated content about what actually happened in
   that segment — followed by that candidate's transcript, VERBATIM.
4. Updates the session's visited-set (cache_service, Prompt 2) with every
   segment id actually used, and records entity mentions for whichever
   entities bridged a candidate in (cache_service's recency tracking,
   Prompt 7) — the write side both retrieval_service.py and
   relevance_scorer.py deferred to this exact step.

Zero LLM calls anywhere in this module: every word that reaches the family
member is either the storyteller's own verbatim transcript or a fixed
template string, by construction — nothing here is generated.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import RawSegment
from app.services import graph_memory, relevance_scorer, retrieval_service
from app.services.cache import cache_service

logger = logging.getLogger(__name__)

# Fixed answer when no primary segment matches the question's topic at
# all — never an LLM-generated apology/filler, exactly per the project plan.
NO_STORY_FALLBACK = "אין לי סיפור על זה"

# Fixed bridge-phrase bank (Hebrew) — {entity} is the only thing ever
# injected; the phrase itself never varies with what actually happened in
# the related segment.
BRIDGE_PHRASE_TEMPLATES = [
    "זה מזכיר לי גם את {entity}...",
    "אגב, יש עוד סיפור על {entity}...",
    "וזה גורם לי לחשוב גם על {entity}...",
]


def _pick_bridge_phrase(index: int) -> str:
    return BRIDGE_PHRASE_TEMPLATES[index % len(BRIDGE_PHRASE_TEMPLATES)]


async def _fetch_transcripts(segment_ids: List[str]) -> Dict[str, str]:
    if not segment_ids:
        return {}
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(RawSegment).where(RawSegment.id.in_(segment_ids)))
        return {
            seg.id: seg.transcript.strip()
            for seg in result.scalars().all()
            if seg.transcript and seg.transcript.strip()
        }


async def _shared_entity_name(
    primary_entities: Set[str], candidate_id: str, group_id: str
) -> Optional[str]:
    try:
        candidate_entities = set(
            await graph_memory.get_episode_entity_names(candidate_id, group_id=group_id)
        )
    except Exception as e:
        logger.warning(f"Could not fetch entities for candidate {candidate_id}: {e}")
        return None
    shared = primary_entities & candidate_entities
    return next(iter(shared), None)


async def assemble_response(
    question: str, group_id: str, recording_language: str, session_id: str
) -> str:
    retrieval = await retrieval_service.retrieve(question, group_id, recording_language, session_id)

    if not retrieval.primary:
        return NO_STORY_FALLBACK

    scored = await relevance_scorer.score_candidates(
        question,
        [c.segment_id for c in retrieval.candidates],
        session_id,
        group_id,
    )

    primary_ids = [s.segment_id for s in retrieval.primary]
    approved_ids = [s.segment_id for s in scored]

    transcripts = await _fetch_transcripts(primary_ids + approved_ids)

    parts: List[str] = [transcripts[pid] for pid in primary_ids if pid in transcripts]
    if not parts:
        # Primary segment(s) matched by topic but had no usable transcript
        # (a data-integrity edge case, not expected in normal operation) —
        # still never fabricate an answer.
        return NO_STORY_FALLBACK

    used_segment_ids: List[str] = [pid for pid in primary_ids if pid in transcripts]
    used_entity_names: Set[str] = set()

    if approved_ids:
        primary_entities: Set[str] = set()
        for pid in primary_ids:
            try:
                primary_entities.update(
                    await graph_memory.get_episode_entity_names(pid, group_id=group_id)
                )
            except Exception as e:
                logger.warning(f"Could not fetch entities for primary segment {pid}: {e}")

        bridge_index = 0
        for cid in approved_ids:
            if cid not in transcripts:
                continue
            shared_name = await _shared_entity_name(primary_entities, cid, group_id)
            if not shared_name:
                # expand_graph only surfaced this candidate because it DID
                # share an entity with the primary segment(s) — if that
                # can't be confirmed here, skip rather than inject a
                # placeholder that could misrepresent the connection.
                logger.warning(f"No shared entity found for candidate {cid}; skipping bridge")
                continue
            parts.append(_pick_bridge_phrase(bridge_index).format(entity=shared_name))
            parts.append(transcripts[cid])
            used_segment_ids.append(cid)
            used_entity_names.add(shared_name)
            bridge_index += 1

    final_text = " ".join(parts)

    await cache_service.add_visited(session_id, used_segment_ids)
    if used_entity_names:
        await cache_service.record_entity_mentions(session_id, list(used_entity_names))

    return final_text
