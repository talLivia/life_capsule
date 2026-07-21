"""
LangGraph analysis pipeline (Prompt 5) — turns a raw recorded segment into a
Graphiti episode, with a human-in-the-loop pause when an entity name in the
new segment might (or might not) be the same real-world person/place/event
as one already in the story archive.

Nodes, in order: transcribe -> embed_transcript -> extract_topics ->
check_entities -> human_confirm (loops until every ambiguous name this
segment introduces is resolved) -> score_importance -> finalize_ingest. Any
node that hits an unrecoverable error routes to `fail` instead of
continuing.

embed_transcript (Prompt 7) computes and persists the segment's transcript
embedding once, at ingestion time, so relevance_scorer.py's cosine-
similarity term never re-embeds segment text on a live retrieval turn —
only the incoming question gets embedded then. Fail-soft like
extract_topics: an embedding failure leaves embedding=None (relevance_score
degrades to 0 for that segment, per relevance_scorer.py) rather than
failing the whole segment.

Ambiguity heuristic (check_entities): `graph_memory.get_entity_candidates`
returns candidates ranked by relevance but WITHOUT a similarity score or a
minimum-relevance floor (that's its public contract, from Prompt 3 —
filtering is deliberately left to the caller). Confirmed live against a
real graph: querying a single-node graph for a totally unrelated name still
returns that node as a "candidate", so a lexical-similarity gate
(`graph_memory.names_are_similar` — shared with retrieval_service.py's
Prompt 6/10 entity-based primary matching) runs first — only a name that's
actually similar to a candidate's name (substring or a high SequenceMatcher
ratio) counts as a real match at all. A name with zero real matches is
brand new and never interrupts — Graphiti will just create it during
finalize_ingest.

Auto-resolve without asking ONLY when there is exactly one real match and
it's an exact case-insensitive name match — anything else is ambiguous and
interrupts, INCLUDING a first-name-only mention (e.g. "Moshe") that real-
matches more than one existing entity (e.g. "Moshe Cohen" AND "Moshe
Levi"). That case used to silently pick whichever candidate Graphiti's
hybrid search ranked first and ask a plain yes/no against just that one —
a real bug (the other equally-plausible candidate was never even
mentioned). human_confirm now surfaces every real match at once so the
storyteller can pick the right one (or say "someone new") instead of being
asked about an arbitrary single guess, and the confirmed resolution stores
the FULLER identifying name (e.g. "Moshe Cohen", not just "Moshe") so
future retrieval never confuses the two people again.

Entity name extraction here is our OWN lightweight Claude call (see
_ENTITY_NAME_SYSTEM_PROMPT below), not graphiti_core's internal
`extract_nodes` (utils.maintenance.node_operations) — that function exists
but requires constructing EpisodicNode/GraphitiClients objects and isn't
part of Graphiti's stable public surface, so depending on it here would be
one version bump away from breaking silently. This module only ever calls
Graphiti through graph_memory.py's public wrapper functions.

Checkpointing: LangGraph's `human_confirm` interrupt needs to survive well
past the request that triggered it (the storyteller may finish the whole
interview before answering a disambiguation question), so state is persisted
via `AsyncPostgresSaver` against the same Postgres database as the app
(psycopg3 — see requirements.txt). `_open_checkpointer` is swappable
(monkeypatched to an in-memory saver in tests) the same way
graph_memory.py's `_build_graphiti` is.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import InterviewSession, RawSegment, User
from app.services import embeddings, graph_memory
from app.services.graph_memory import names_are_similar as _names_are_similar
from app.services.llm import llm_service
from app.services.storage import storage_service
from app.services.stt import stt_service

logger = logging.getLogger(__name__)


class AnalysisState(TypedDict, total=False):
    segment_id: str
    group_id: str
    transcript: str
    embedding: List[float]
    topic_tags: List[str]
    names_to_check: List[Dict[str, str]]
    entity_resolutions: Dict[str, Dict[str, Optional[str]]]
    importance_score: float
    status: str
    error: str


_EXTRACT_TOPICS_SYSTEM_PROMPT = """\
You are a strict content classifier for a personal life-story archive. \
Given a transcript of someone recounting a memory, output ONLY a JSON \
array of short topic tags (1-3 words each, lowercase, in the SAME \
language as the transcript) describing what the story is actually \
about - its real content, not the interview question that prompted it. \
Do not include any commentary, explanation, or text outside the JSON \
array. Example output: ["military service", "friendship", "loss"]"""

_ENTITY_NAME_SYSTEM_PROMPT = """\
You are a strict named-entity extractor for a personal life-story \
archive. Given a transcript, output ONLY a JSON array of distinct \
proper names of PEOPLE, PLACES, or notable EVENTS mentioned in the \
text, written exactly as they appear there (same language/script). Do \
not include pronouns, generic nouns, or anything that is not a proper \
name. Do not include any commentary or text outside the JSON array. \
Example output: ["Gila", "Tel Aviv"]"""

_IMPORTANCE_SYSTEM_PROMPT = """\
You are scoring the significance of a single memory for a personal \
life-story archive, following the memory-importance approach from Park \
et al. 2023 ("Generative Agents"). On a scale from 0 to 10, where 0 is \
a mundane, routine occurrence (e.g. brushing teeth, a routine commute) \
and 10 is a major, life-altering event (e.g. a marriage, a birth, a \
death, a life-changing decision), rate how significant and memorable \
the event described in this transcript is. Output ONLY a single \
integer from 0 to 10, with no other text."""


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


def _parse_importance_score(text: str) -> int:
    match = re.search(r"-?\d+", text)
    if not match:
        return 5
    return max(0, min(10, int(match.group(0))))


def _build_custom_extraction_instructions(
    resolutions: Dict[str, Dict[str, Optional[str]]],
) -> Optional[str]:
    if not resolutions:
        return None
    lines = []
    for name, resolution in resolutions.items():
        same_as_uuid = resolution.get("same_as_uuid")
        resolved_name = resolution.get("resolved_name") or name
        if same_as_uuid:
            # Use the FULLER identifying name (e.g. "Moshe Cohen", confirmed
            # by the storyteller) rather than the bare mention ("Moshe") —
            # so the entity's name in the graph stays disambiguated for
            # future retrieval, not just linked by id.
            lines.append(
                f'Treat the name "{name}" as referring to the existing entity with id '
                f'{same_as_uuid}, also known as "{resolved_name}" - this is the same '
                f"real-world person, place, or event mentioned before. Do not create a "
                f'duplicate node for it; use "{resolved_name}" as this entity\'s name.'
            )
        else:
            lines.append(
                f'The name "{name}" in this text refers to a person, place, or event '
                f"distinct from any other same-named entity already in the graph. Do "
                f"not merge it with an existing entity of the same name."
            )
    return "\n".join(lines)


async def _load_segment_and_user(db, segment_id: str):
    result = await db.execute(
        select(RawSegment, User)
        .join(InterviewSession, RawSegment.interview_session_id == InterviewSession.id)
        .join(User, InterviewSession.user_id == User.id)
        .where(RawSegment.id == segment_id)
    )
    row = result.first()
    if row is None:
        return None, None
    return row


# ── Nodes ─────────────────────────────────────────────────────────────────


async def transcribe_node(state: AnalysisState) -> dict:
    segment_id = state["segment_id"]
    async with AsyncSessionLocal() as db:
        segment, user = await _load_segment_and_user(db, segment_id)
        if segment is None:
            return {"error": "segment not found"}
        if segment.transcript:
            # Already transcribed (e.g. Prompt 4's ingest-time transcription
            # already ran) — don't burn a second Whisper pass on the same take.
            return {"transcript": segment.transcript}
        if not segment.video_key:
            return {"error": "segment has no video_key"}

        try:
            video_bytes = await storage_service.download_file(segment.video_key)
            transcript = await stt_service.transcribe(
                video_bytes, language=user.recording_language
            )
        except Exception as e:
            logger.error(f"transcribe_node failed for segment {segment_id}: {e}")
            return {"error": f"transcription failed: {e}"}

        segment.transcript = transcript
        await db.commit()
        return {"transcript": transcript}


async def embed_transcript_node(state: AnalysisState) -> dict:
    segment_id = state["segment_id"]
    transcript = state.get("transcript") or ""
    vector: Optional[List[float]] = None
    if transcript:
        try:
            vector = await embeddings.embed_text(transcript)
        except Exception as e:
            # Non-fatal — Prompt 7's relevance scoring just treats a missing
            # embedding as "no relevance signal" for this segment, not a
            # reason to fail the whole segment.
            logger.warning(f"embed_transcript_node failed for segment {segment_id}: {e}")

    async with AsyncSessionLocal() as db:
        segment, _user = await _load_segment_and_user(db, segment_id)
        if segment is not None:
            segment.embedding = vector
            await db.commit()
    return {"embedding": vector} if vector is not None else {}


async def extract_topics_node(state: AnalysisState) -> dict:
    segment_id = state["segment_id"]
    transcript = state.get("transcript") or ""
    tags: List[str] = []
    if transcript:
        try:
            raw = await llm_service.generate_response(
                messages=[{"role": "user", "content": transcript}],
                system_prompt=_EXTRACT_TOPICS_SYSTEM_PROMPT,
            )
            tags = _parse_json_array(raw)
        except Exception as e:
            # Non-fatal — a missing topic tag is far less severe than a
            # missing transcript, so log and continue with an empty list
            # rather than failing the whole segment.
            logger.warning(f"extract_topics_node failed for segment {segment_id}: {e}")

    async with AsyncSessionLocal() as db:
        segment, _user = await _load_segment_and_user(db, segment_id)
        if segment is not None:
            segment.topic_tags = tags
            await db.commit()
    return {"topic_tags": tags}


async def check_entities_node(state: AnalysisState) -> dict:
    segment_id = state["segment_id"]
    group_id = state["group_id"]
    transcript = state.get("transcript") or ""
    if not transcript:
        return {"names_to_check": [], "entity_resolutions": {}}

    try:
        raw = await llm_service.generate_response(
            messages=[{"role": "user", "content": transcript}],
            system_prompt=_ENTITY_NAME_SYSTEM_PROMPT,
        )
        names = _parse_json_array(raw)
    except Exception as e:
        logger.warning(f"check_entities_node name extraction failed for segment {segment_id}: {e}")
        names = []

    to_check: List[Dict[str, str]] = []
    auto_resolutions: Dict[str, Dict[str, Optional[str]]] = {}
    seen: set = set()

    for name in names:
        key = name.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)

        candidates = await graph_memory.get_entity_candidates(name, group_id=group_id)
        # get_entity_candidates has no minimum-relevance floor by design
        # (Prompt 3: "deliberately NOT a single 'best' match" — filtering is
        # the caller's job) — confirmed live that it returns a small graph's
        # only node as a "candidate" even for a completely unrelated query
        # (e.g. "דן כהן" against a graph containing only "גילה"). Gate on
        # actual lexical similarity before treating anything as ambiguous,
        # or every new name in a growing archive would spuriously pause for
        # confirmation against whatever's already there.
        relevant = [c for c in candidates if _names_are_similar(name, c["name"])]
        if not relevant:
            continue  # brand-new entity — Graphiti will just create it

        if len(relevant) == 1 and relevant[0]["name"].strip().lower() == key:
            # Exactly one real match AND it's the same name verbatim —
            # confident enough to auto-resolve without interrupting the
            # storyteller every time a previously-confirmed name recurs.
            only = relevant[0]
            auto_resolutions[name] = {"same_as_uuid": only["uuid"], "resolved_name": only["name"]}
            continue

        # Ambiguous: either 2+ real matches (even if one of them is an exact
        # name match — a bare "Moshe" against both an existing "Moshe" and a
        # "Moshe Cohen" is still genuinely ambiguous), or a single non-exact
        # fuzzy match. Keep every real match so human_confirm can ask about
        # all of them at once instead of an arbitrary single guess.
        to_check.append(
            {
                "name": name,
                "candidates": [
                    {
                        "uuid": c["uuid"],
                        "name": c["name"],
                        "summary": c.get("summary") or "",
                    }
                    for c in relevant
                ],
            }
        )

    return {"names_to_check": to_check, "entity_resolutions": auto_resolutions}


def _confirmation_question(entity_name: str, candidates: List[Dict[str, str]]) -> str:
    if len(candidates) == 1:
        return (
            f'Is "{entity_name}" mentioned in this new segment the same as '
            f'"{candidates[0]["name"]}" already in your story archive?'
        )
    options = "; ".join(
        f'{c["name"]} ({c["summary"]})' if c["summary"] else c["name"] for c in candidates
    )
    return (
        f'You mentioned "{entity_name}" - is this the same person/place as one of '
        f"these already in your story archive: {options}? Or is this someone new?"
    )


async def human_confirm_node(state: AnalysisState) -> dict:
    queue = list(state.get("names_to_check") or [])
    if not queue:
        return {}
    current = queue[0]
    remaining = queue[1:]
    candidates = current["candidates"]

    answer = interrupt(
        {
            "entity_name": current["name"],
            "candidates": candidates,
            "question": _confirmation_question(current["name"], candidates),
        }
    )

    resolutions = dict(state.get("entity_resolutions") or {})
    if answer.get("same_as_existing"):
        chosen_uuid = answer.get("candidate_uuid")
        chosen = next((c for c in candidates if c["uuid"] == chosen_uuid), None)
        resolutions[current["name"]] = {
            "same_as_uuid": chosen_uuid,
            # Fall back to the raw extracted name only if the resume
            # payload names a uuid that isn't actually one of the pending
            # candidates — shouldn't happen through the API (which
            # validates this), but this keeps the node itself safe either way.
            "resolved_name": chosen["name"] if chosen else current["name"],
        }
    else:
        resolutions[current["name"]] = {"same_as_uuid": None, "resolved_name": current["name"]}

    return {"names_to_check": remaining, "entity_resolutions": resolutions}


async def score_importance_node(state: AnalysisState) -> dict:
    segment_id = state["segment_id"]
    transcript = state.get("transcript") or ""
    score = 5.0
    if transcript:
        try:
            raw = await llm_service.generate_response(
                messages=[{"role": "user", "content": transcript}],
                system_prompt=_IMPORTANCE_SYSTEM_PROMPT,
            )
            score = float(_parse_importance_score(raw))
        except Exception as e:
            logger.warning(
                f"score_importance_node failed for segment {segment_id}: {e}; "
                "defaulting to neutral score"
            )

    async with AsyncSessionLocal() as db:
        segment, _user = await _load_segment_and_user(db, segment_id)
        if segment is not None:
            segment.importance_score = score
            await db.commit()
    return {"importance_score": score}


async def finalize_ingest_node(state: AnalysisState) -> dict:
    segment_id = state["segment_id"]
    async with AsyncSessionLocal() as db:
        segment, _user = await _load_segment_and_user(db, segment_id)
        if segment is None:
            return {"error": "segment not found", "status": "failed"}

        instructions = _build_custom_extraction_instructions(
            state.get("entity_resolutions") or {}
        )
        try:
            await graph_memory.add_episode(
                segment_id=segment_id,
                transcript=state.get("transcript") or segment.transcript or "",
                topic_tags=state.get("topic_tags") or segment.topic_tags or [],
                timestamp=segment.created_at,
                group_id=state["group_id"],
                custom_extraction_instructions=instructions,
            )
        except Exception as e:
            logger.error(f"finalize_ingest_node failed for segment {segment_id}: {e}")
            return {"error": str(e), "status": "failed"}

        segment.status = "ready"
        segment.pending_confirmation = None
        await db.commit()

    return {"status": "ready"}


async def fail_node(state: AnalysisState) -> dict:
    segment_id = state["segment_id"]
    async with AsyncSessionLocal() as db:
        segment, _user = await _load_segment_and_user(db, segment_id)
        if segment is not None:
            segment.status = "failed"
            segment.pending_confirmation = None
            await db.commit()
    logger.error(f"analysis_graph failed for segment {segment_id}: {state.get('error')}")
    return {"status": "failed"}


def _route_on_error(state: AnalysisState) -> str:
    return "fail" if state.get("error") else "next"


def _has_pending_names(state: AnalysisState) -> str:
    return "confirm" if state.get("names_to_check") else "skip"


def build_graph(checkpointer):
    graph = StateGraph(AnalysisState)
    graph.add_node("transcribe", transcribe_node)
    graph.add_node("embed_transcript", embed_transcript_node)
    graph.add_node("extract_topics", extract_topics_node)
    graph.add_node("check_entities", check_entities_node)
    graph.add_node("human_confirm", human_confirm_node)
    graph.add_node("score_importance", score_importance_node)
    graph.add_node("finalize_ingest", finalize_ingest_node)
    graph.add_node("fail", fail_node)

    graph.add_edge(START, "transcribe")
    graph.add_conditional_edges(
        "transcribe", _route_on_error, {"fail": "fail", "next": "embed_transcript"}
    )
    graph.add_edge("embed_transcript", "extract_topics")
    graph.add_edge("extract_topics", "check_entities")
    graph.add_conditional_edges(
        "check_entities", _has_pending_names, {"confirm": "human_confirm", "skip": "score_importance"}
    )
    graph.add_conditional_edges(
        "human_confirm", _has_pending_names, {"confirm": "human_confirm", "skip": "score_importance"}
    )
    graph.add_edge("score_importance", "finalize_ingest")
    graph.add_conditional_edges(
        "finalize_ingest", _route_on_error, {"fail": "fail", "next": END}
    )
    graph.add_edge("fail", END)

    return graph.compile(checkpointer=checkpointer)


# ── Entry points (Celery task + confirm-entity API call this) ──────────────


@asynccontextmanager
async def _default_checkpointer():
    async with AsyncPostgresSaver.from_conn_string(settings.DATABASE_URL) as saver:
        await saver.setup()
        yield saver


# Swappable in tests (an InMemorySaver needs no real Postgres) — mirrors
# graph_memory.py's _build_graphiti() swap-in pattern.
_open_checkpointer = _default_checkpointer


def _thread_config(segment_id: str) -> dict:
    return {"configurable": {"thread_id": segment_id}}


async def _sync_segment_from_result(segment_id: str, result: Dict[str, Any]) -> None:
    async with AsyncSessionLocal() as db:
        db_result = await db.execute(select(RawSegment).where(RawSegment.id == segment_id))
        segment = db_result.scalar_one_or_none()
        if segment is None:
            return

        interrupts = result.get("__interrupt__")
        if interrupts:
            segment.pending_confirmation = interrupts[0].value
            segment.status = "pending_confirmation"
        else:
            segment.pending_confirmation = None
            segment.status = result.get("status", "failed")
        await db.commit()


async def run_segment_analysis(segment_id: str) -> Dict[str, Any]:
    """Kick off the pipeline for a freshly-ingested segment (called by the
    Celery task enqueued from `/segments/ingest`, Prompt 4)."""
    async with AsyncSessionLocal() as db:
        segment, user = await _load_segment_and_user(db, segment_id)
    if segment is None or user is None:
        logger.error(f"run_segment_analysis: segment {segment_id} not found")
        return {"status": "failed"}

    async with _open_checkpointer() as checkpointer:
        graph = build_graph(checkpointer)
        result = await graph.ainvoke(
            {"segment_id": segment_id, "group_id": user.id},
            config=_thread_config(segment_id),
        )
        await _sync_segment_from_result(segment_id, result)
        return result


async def resume_segment_analysis(segment_id: str, resume_value: Dict[str, Any]) -> Dict[str, Any]:
    """Resume a paused pipeline after `/segments/{id}/confirm-entity` answers
    the currently-pending human_confirm question."""
    async with _open_checkpointer() as checkpointer:
        graph = build_graph(checkpointer)
        result = await graph.ainvoke(
            Command(resume=resume_value), config=_thread_config(segment_id)
        )
        await _sync_segment_from_result(segment_id, result)
        return result
