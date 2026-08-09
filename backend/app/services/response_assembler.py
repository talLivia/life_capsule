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
from app.services import entity_store, relevance_scorer, retrieval_service
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


# "I don't have a story about that" is true but sounds like the system failed
# to understand the question. Asked "what else did you do together?" about a
# specific person, it reads as a shrug — when the honest and much warmer answer
# is "זה כל מה שיש לי על אמנון".
#
# SAME RULE AS THE BRIDGE PHRASES ABOVE, and for the same reason: {entity} is
# the only thing ever injected, and it is a name the archive really holds,
# validated before it gets here. The sentence itself never varies with what
# happened in any recording, so nothing is generated on the storyteller's
# behalf — the constraint NO_STORY_FALLBACK exists to enforce.
#
# ONE BANK, AND ONLY THE "NOTHING MORE" ONE. There was briefly a second bank
# for "I have nothing about X at all", and it was unreachable and dangerous in
# the same breath: the caller now refuses to name a subject while ANY of that
# person's units are still unplayed (see _no_story_line), because an empty
# selection means "nothing answers THAT question", never "the archive is out
# of stories about this person". Every entity in the map has units, so
# "nothing at all about X" can only be said when everything about X has
# already been played — which is precisely what this bank says.
#
# That happened live: with five of אמנון's twelve units played, the archive
# told the listener there was nothing more while the entire army story sat
# unplayed. A specific falsehood is worse than a vague truth — it tells
# someone to stop asking about a person the archive still has stories about.
NO_MORE_STORY_ABOUT_TEMPLATES = [
    "אין לי עוד סיפור על {entity}",
    "זה כל מה שיש לי על {entity}",
    "לא נשאר לי עוד מה לספר על {entity}",
]


def no_story_about(entity: str, *, variant: int = 0) -> str:
    """"That's all I have about אמנון" — said only when it is true.

    `variant` picks from the bank and is expected to be a STABLE function of
    the question, so the same question always produces the same wording.
    Random selection would make the retrieval eval flaky for a reason that has
    nothing to do with retrieval.
    """
    return NO_MORE_STORY_ABOUT_TEMPLATES[
        variant % len(NO_MORE_STORY_ABOUT_TEMPLATES)
    ].format(entity=entity)


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


async def _entity_names_for(segment_ids: List[str], group_id: str) -> Dict[str, Set[str]]:
    """segment id -> its entity names, for a whole set at once.

    Fail-soft: these names only decide the WORDING of a bridge sentence
    between two recordings, never which recordings are used, so losing them
    costs a generic bridge rather than an answer.
    """
    if not segment_ids:
        return {}
    try:
        async with AsyncSessionLocal() as db:
            by_segment = await entity_store.get_entity_names_for_segments(
                db, segment_ids, group_id
            )
    except Exception as e:
        logger.warning(f"Could not fetch entities for {len(segment_ids)} segments: {e}")
        return {}
    return {sid: set(names) for sid, names in by_segment.items()}


def _shared_entity_name(
    primary_entities: Set[str], candidate_entities: Set[str]
) -> Optional[str]:
    return next(iter(primary_entities & candidate_entities), None)


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
        # One query covering the primaries AND every candidate, replacing a
        # graph round trip per primary plus another per candidate.
        entities_by_segment = await _entity_names_for(primary_ids + approved_ids, group_id)
        primary_entities: Set[str] = set()
        for pid in primary_ids:
            primary_entities.update(entities_by_segment.get(pid, set()))

        bridge_index = 0
        for cid in approved_ids:
            if cid not in transcripts:
                continue
            shared_name = _shared_entity_name(
                primary_entities, entities_by_segment.get(cid, set())
            )
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
