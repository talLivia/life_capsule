"""
LangGraph analysis pipeline (Prompt 5) — turns a raw recorded segment into a
transcript, chunks, and a set of ENTITIES AND MENTIONS IN POSTGRES, with a
human-in-the-loop pause when an entity name in the new segment might (or
might not) be the same real-world person/place/event as one already in the
story archive.

Entities used to be written to Graphiti/Neo4j here (`add_episode`) and read
back from it for disambiguation. Both now go through `services/entity_store.py`
against Postgres — see `docs/PROJECT_STATUS.md` for why.

Nodes, in order: transcribe -> embed_transcript -> extract_topics ->
check_entities -> human_confirm (ONE pause carrying every question this
recording raises, identity and type together) -> score_importance ->
finalize_ingest. Any node that hits an unrecoverable error routes to `fail`
instead of continuing.

embed_transcript (Prompt 7) computes and persists the segment's transcript
embedding once, at ingestion time, so relevance_scorer.py's cosine-
similarity term never re-embeds segment text on a live retrieval turn —
only the incoming question gets embedded then. Fail-soft like
extract_topics: an embedding failure leaves embedding=None (relevance_score
degrades to 0 for that segment, per relevance_scorer.py) rather than
failing the whole segment.

Ambiguity heuristic (check_entities): `entity_store.get_entity_candidates`
returns candidates ranked by similarity but WITHOUT a minimum floor (that's
its public contract, carried over deliberately — filtering is left to the
caller). Confirmed live both before and after the move: querying a
nearly-empty archive for a totally unrelated name still returns rows as
"candidates", so a lexical-similarity gate
(`entity_names.names_are_similar` — shared with retrieval_service.py's
Prompt 6/10 entity-based primary matching) runs first — only a name that's
actually similar to a candidate's name (substring or a high SequenceMatcher
ratio) counts as a real match at all. A name with zero real matches is
brand new and never interrupts — entity_store creates it during
finalize_ingest, or merges it onto an existing row by normalized_name.

Auto-resolve without asking ONLY when there is exactly one real match and
it's an exact case-insensitive name match — anything else is ambiguous and
interrupts, INCLUDING a first-name-only mention (e.g. "Moshe") that real-
matches more than one existing entity (e.g. "Moshe Cohen" AND "Moshe
Levi"). That case used to silently pick whichever candidate the graph's
hybrid search ranked first and ask a plain yes/no against just that one —
a real bug (the other equally-plausible candidate was never even
mentioned). human_confirm now surfaces every real match at once so the
storyteller can pick the right one (or say "someone new") instead of being
asked about an arbitrary single guess, and the confirmed resolution stores
the FULLER identifying name (e.g. "Moshe Cohen", not just "Moshe") so
future retrieval never confuses the two people again.

Entity extraction is our OWN call (`services/entity_extraction.py`). It was
always ours rather than graphiti_core's internal `extract_nodes`, which was
lucky as well as deliberate: owning the call is what made the richer
`{name, type, alternative_type, summary}` shape possible, and it meant
removing Graphiti cost the extraction nothing.

Checkpointing: LangGraph's `human_confirm` interrupt needs to survive well
past the request that triggered it (the storyteller may finish the whole
interview before answering a disambiguation question), so state is persisted
via `AsyncPostgresSaver` against the same Postgres database as the app
(psycopg3 — see requirements.txt). `_open_checkpointer` is swappable
(monkeypatched to an in-memory saver in tests).
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import delete, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import InterviewSession, RawSegment, TranscriptChunk, User
from app.services import embeddings, entity_extraction, entity_store
from app.services.entity_extraction import ExtractedEntity
from app.services.entity_names import names_are_similar as _names_are_similar
from app.services.llm import llm_service
from app.services.storage import storage_service
from app.services.stt import stt_service

logger = logging.getLogger(__name__)


class AnalysisState(TypedDict, total=False):
    segment_id: str
    group_id: str
    transcript: str
    # Phrase-level STT output (Prompt 11) — one dict per Whisper-detected
    # phrase: {"start_sec", "end_sec", "text", "words": [{"word", "start_sec",
    # "end_sec"}, ...]}. Only populated when transcribe_node actually ran
    # Whisper this pass (empty when it reused an already-set
    # segment.transcript — see transcribe_node's docstring) — the original-
    # video-clip mode (Prompts 12-14) doesn't exist yet without this, but
    # nothing here is used by the avatar path.
    phrases: List[Dict[str, Any]]
    chunk_ids: List[str]
    embedding: List[float]
    topic_tags: List[str]
    names_to_check: List[Dict[str, str]]
    entity_resolutions: Dict[str, Dict[str, Optional[str]]]
    # The structured extraction from check_entities_node, as plain dicts —
    # {name, type, alternative_type, summary}. Carried through state rather
    # than re-extracted in finalize_ingest so that the names a human is asked
    # to confirm are exactly the names that get written; two calls over one
    # transcript can disagree. Dicts, not dataclasses, because this state is
    # checkpointed and may sit through a human confirmation lasting days.
    extracted_entities: List[Dict[str, Any]]
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

# Entity extraction moved to services/entity_extraction.py, which returns
# {name, type, alternative_type, summary} instead of bare names. It had to:
# the summaries used to come from Graphiti's own extraction inside
# add_episode, and with the write path in Postgres nothing else produces them.

_IMPORTANCE_SYSTEM_PROMPT = """\
You are scoring the significance of a single memory for a personal \
life-story archive, following the memory-importance approach from Park \
et al. 2023 ("Generative Agents"). On a scale from 0 to 10, where 0 is \
a mundane, routine occurrence (e.g. brushing teeth, a routine commute) \
and 10 is a major, life-altering event (e.g. a marriage, a birth, a \
death, a life-changing decision), rate how significant and memorable \
the event described in this transcript is. Output ONLY a single \
integer from 0 to 10, with no other text."""

# Prompt 11's TranscriptChunk: how many neighboring phrases (each side) get
# folded into the CONTEXT used only for computing a chunk's embedding — a
# short phrase in isolation ("I was a carpenter") can be too ambiguous on
# its own for a good semantic match. Never affects what's stored as the
# chunk's own start_sec/end_sec/text, only the text handed to the embedder.
_CHUNK_CONTEXT_WINDOW = 1


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


def _apply_entity_resolutions(
    entities: List[ExtractedEntity],
    resolutions: Dict[str, Dict[str, Optional[str]]],
) -> List[ExtractedEntity]:
    """Carry a human's disambiguation answer into the entity that gets written.

    This used to build a natural-language instruction steering Graphiti's own
    dedup. In Postgres the merge is not a suggestion — it is
    UNIQUE (producer_id, normalized_name) — so a confirmation is applied by
    RENAMING: when the producer confirms that "Moshe" is the "Moshe Cohen"
    already in their archive, the entity is written under "Moshe Cohen" and
    lands on that row by the merge key. The fuller name is also the better
    one to store, exactly as before: it stays disambiguated against the other
    Moshe for every future recording.

    The opposite answer — "same name, DIFFERENT person" — is deliberately not
    handled here, and cannot be: the merge key is the name, so two entities
    with one name need a distinguishing name before Postgres can hold them
    apart. That is chunk 4's confirmation flow, which asks for one. Until
    then this is the same behaviour the archive has today (a single row), not
    a regression introduced by the move.
    """
    if not resolutions:
        return entities

    resolved: List[ExtractedEntity] = []
    for entity in entities:
        resolution = resolutions.get(entity.name)
        if resolution and resolution.get("same_as_uuid"):
            name = resolution.get("resolved_name") or entity.name
            resolved.append(replace(entity, name=name))
        else:
            resolved.append(entity)
    return resolved


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

    # REMOVED: this node used to clear the segment's previous Graphiti episode
    # before anything else ran, so that re-ingesting could not duplicate it.
    # It must not do that any more, and the reason is worth stating because
    # deleting-before-rewriting looked correct for as long as there was a
    # rewrite.
    #
    # finalize_ingest no longer writes episodes — it writes entities to
    # Postgres. So a removal here would delete and never replace, and until
    # chunk 2 imports the existing entities, the graph holds the ONLY copy of
    # their summaries. Re-analysing a recording would silently destroy them.
    # Neo4j is deliberately frozen (read-only) between this chunk and its
    # import: nothing writes to it, so nothing needs to clean up after itself.
    #
    # Re-ingest is still idempotent, one layer down: entity_store replaces
    # this segment's mentions rather than appending to them.
    #
    # segment_deletion keeps its own remove_episodes_for_segment call — there
    # the deletion is the point, not a prelude to a rewrite.

    async with AsyncSessionLocal() as db:
        segment, user = await _load_segment_and_user(db, segment_id)
        if segment is None:
            return {"error": "segment not found"}
        if segment.transcript:
            # Already transcribed (e.g. Prompt 4's ingest-time transcription
            # already ran) — don't burn a second Whisper pass on the same
            # take. NOTE (Prompt 11): this shortcut means `phrases` stays
            # empty for a segment transcribed this way, so
            # create_transcript_chunks_node creates no chunks for it this
            # pass — only segments actually transcribed via THIS node (the
            # branch below) get phrase-level data and chunks. Retroactively
            # backfilling chunks for already-transcribed segments is out of
            # this prompt's stated scope (data model + ingestion only).
            return {"transcript": segment.transcript}
        if not segment.video_key:
            return {"error": "segment has no video_key"}

        try:
            video_bytes = await storage_service.download_file(segment.video_key)
            result = await stt_service.transcribe_with_timestamps(
                video_bytes, language=user.recording_language
            )
        except Exception as e:
            logger.error(f"transcribe_node failed for segment {segment_id}: {e}")
            return {"error": f"transcription failed: {e}"}

        transcript = result["text"]
        segment.transcript = transcript
        await db.commit()
        return {"transcript": transcript, "phrases": result["phrases"]}


async def create_transcript_chunks_node(state: AnalysisState) -> dict:
    """
    Prompt 11: one TranscriptChunk per Whisper-detected phrase from
    transcribe_node's `phrases` (empty — and this node a no-op — when that
    node hit its "already transcribed" shortcut; see its docstring). Purely
    additive to the existing avatar path: doesn't read or write
    `segment.transcript`/`embedding`/`topic_tags` at all, only creates rows
    in the new table.

    Each chunk's OWN embedding is computed from its text PLUS a small window
    of neighboring phrases (_CHUNK_CONTEXT_WINDOW) for better semantic
    recall on a short, otherwise-ambiguous phrase — but the window is never
    what's stored as the chunk's own text/boundaries, only what's handed to
    the embedder. Topic tagging runs per-chunk (one LLM call per phrase) as
    specified — worth flagging: a long, many-phrase recording means many
    more LLM calls here than the single whole-segment call extract_topics_
    node already makes, a real latency/cost cost of chunk-level precision.
    """
    segment_id = state["segment_id"]
    phrases = state.get("phrases") or []
    if not phrases:
        return {}

    texts = [p["text"] for p in phrases]

    async with AsyncSessionLocal() as db:
        segment, _user = await _load_segment_and_user(db, segment_id)
        if segment is None:
            return {}

        # Idempotent re-run safety (e.g. a retried analysis pass) — mirrors
        # every other node here overwriting rather than appending.
        await db.execute(
            delete(TranscriptChunk).where(TranscriptChunk.raw_segment_id == segment_id)
        )

        chunks: List[TranscriptChunk] = []
        for i, phrase in enumerate(phrases):
            window_start = max(0, i - _CHUNK_CONTEXT_WINDOW)
            window_end = min(len(texts), i + _CHUNK_CONTEXT_WINDOW + 1)
            context_text = " ".join(t for t in texts[window_start:window_end] if t).strip()

            embedding: Optional[List[float]] = None
            if context_text:
                try:
                    embedding = await embeddings.embed_text(context_text)
                except Exception as e:
                    # Fail-soft, same pattern as embed_transcript_node: a
                    # missing chunk embedding just means Prompt 12's
                    # semantic-similarity signal has nothing to compare for
                    # this one chunk, not a reason to fail the whole segment.
                    logger.warning(
                        f"create_transcript_chunks_node embedding failed for "
                        f"segment {segment_id} chunk {i}: {e}"
                    )

            topic_tags: List[str] = []
            if phrase["text"]:
                try:
                    raw = await llm_service.generate_response(
                        messages=[{"role": "user", "content": phrase["text"]}],
                        system_prompt=_EXTRACT_TOPICS_SYSTEM_PROMPT,
                        temperature=0,  # structured extraction — deterministic
                    )
                    topic_tags = _parse_json_array(raw)
                except Exception as e:
                    logger.warning(
                        f"create_transcript_chunks_node topic tagging failed for "
                        f"segment {segment_id} chunk {i}: {e}"
                    )

            chunk = TranscriptChunk(
                raw_segment_id=segment_id,
                start_sec=phrase["start_sec"],
                end_sec=phrase["end_sec"],
                text=phrase["text"],
                word_timestamps=phrase["words"],
                embedding=embedding,
                topic_tags=topic_tags,
                sequence_index=i,
            )
            db.add(chunk)
            chunks.append(chunk)

        await db.commit()
        for chunk in chunks:
            await db.refresh(chunk)

        return {"chunk_ids": [chunk.id for chunk in chunks]}


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
                temperature=0,  # structured extraction — deterministic
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


# Deterministic, known Hebrew ASR-confusion normalization for the substring
# check in _tag_chunks_with_entities below ONLY — never applied to the
# stored mentioned_entities value itself (that stays exactly as
# check_entities_node extracted it), and never used anywhere near Prompt
# 13's answer text (which pinpoints verbatim from the real chunk, so this
# has zero effect on what a family member actually hears). Deliberately
# NOT fuzzy/edit-distance matching (rejected on purpose — trades away
# precision for convenience); each mapping below is a specific, confirmed
# confusion, auditable on its own:
#   - ט -> ת: faster-whisper transcribed "בטבריה" (correct) as "בתבריה" —
#     confirmed live on segment 502fb283, a real ASR letter confusion
#     between two Hebrew letters that can sound similar without nikud.
#   - final letter forms -> base form (ם/מ, ן/נ, ץ/צ, ף/פ, ך/כ): a name's
#     letter takes its final form only when it's the last letter of a
#     word: whether that's true for a given occurrence depends on the
#     surrounding text, not the name itself, so the SAME name can
#     legitimately appear in either form depending on context.
_HEBREW_MATCH_NORMALIZE_TABLE = str.maketrans(
    {
        "ט": "ת",
        "ם": "מ",
        "ן": "נ",
        "ץ": "צ",
        "ף": "פ",
        "ך": "כ",
    }
)


def _normalize_for_entity_match(text: str) -> str:
    return text.translate(_HEBREW_MATCH_NORMALIZE_TABLE)


async def _tag_chunks_with_entities(segment_id: str, names: List[str]) -> None:
    """Side-effect only helper for check_entities_node (Prompt 11): stamps
    TranscriptChunk.mentioned_entities with whichever of `names` textually
    appear in each chunk's own text, via case-insensitive substring
    matching (normalized through _normalize_for_entity_match first — see
    its docstring for the specific, deterministic rules and why fuzzy
    matching was rejected). Deliberately not an LLM call and not part of
    this node's return value — this is traceability metadata on the new
    table, not a change to entity disambiguation itself. The STORED
    mentioned_entities value is always the original name from `names`,
    never the normalized form — normalization is comparison-only."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TranscriptChunk).where(TranscriptChunk.raw_segment_id == segment_id)
        )
        chunks = result.scalars().all()
        if not chunks:
            return
        for chunk in chunks:
            text_norm = _normalize_for_entity_match(chunk.text.lower())
            mentioned = [
                name
                for name in names
                if _normalize_for_entity_match(name.strip().lower()) in text_norm
            ]
            if mentioned:
                chunk.mentioned_entities = mentioned
        await db.commit()


async def check_entities_node(state: AnalysisState) -> dict:
    segment_id = state["segment_id"]
    group_id = state["group_id"]
    transcript = state.get("transcript") or ""
    if not transcript:
        return {"names_to_check": [], "entity_resolutions": {}, "extracted_entities": []}

    # ONE extraction, used twice: the names drive disambiguation here, and the
    # full objects are written by finalize_ingest. Extracting again there
    # could return a different list, and then the names the producer confirmed
    # would not be the names that got stored.
    extracted = await entity_extraction.extract_entities(transcript)
    names = [e.name for e in extracted]

    if names:
        # Prompt 11: trace each extracted entity name back to the specific
        # TranscriptChunk(s) it textually appears in — simple substring
        # matching against already-created chunks, NOT a second LLM call,
        # and no change to this node's own disambiguation behavior/return
        # value below. A no-op when no chunks exist yet (already-transcribed
        # segment that skipped chunk creation — see transcribe_node).
        await _tag_chunks_with_entities(segment_id, names)

    to_check: List[Dict[str, str]] = []
    auto_resolutions: Dict[str, Dict[str, Optional[str]]] = {}
    seen: set = set()

    for name in names:
        key = name.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)

        async with AsyncSessionLocal() as db:
            candidates = await entity_store.get_entity_candidates(db, name, group_id)
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
            continue  # brand-new entity — entity_store will just create it

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

    return {
        "names_to_check": to_check,
        "entity_resolutions": auto_resolutions,
        "extracted_entities": [e.as_dict() for e in extracted],
    }


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


def _article(word: str) -> str:
    """"an organisation", "a place". Both slots need this, not just the second
    — the runner-up is as likely to be the vowel-initial one."""
    return "an" if word[:1].lower() in "aeiou" else "a"


def _type_question(entity: Dict[str, Any]) -> str:
    primary, alternative = entity["type"], entity["alternative_type"]
    return (
        f'Is "{entity["name"]}" {_article(primary)} {primary} '
        f"or {_article(alternative)} {alternative}?"
    )


def type_questions(extracted: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The type questions this recording raises — one per entity the extractor
    was genuinely TORN about, and no others.

    `alternative_type` is the whole trigger. It is deliberately not a
    confidence score: self-reported confidence is uncalibrated, whereas "which
    two are you torn between" is concrete, checkable, and gives the screen
    exactly two options to render. An entity named plainly produces no
    question at all, which is the point — asking about everything trains the
    producer to click through without reading, which is worse than not asking.
    """
    return [
        {
            "name": e["name"],
            "type": e["type"],
            "alternative_type": e["alternative_type"],
            "question": _type_question(e),
        }
        for e in extracted
        if e.get("alternative_type")
    ]


async def human_confirm_node(state: AnalysisState) -> dict:
    """ONE interrupt per recording, carrying every question it raises.

    This used to interrupt once PER ambiguous name and loop back into itself,
    so a recording with three questions became three modals in sequence. Now
    identity and type questions go out together and come back in one answer.

    Why batching is the right shape and not just fewer clicks: a sequence of
    modals gives the producer no idea how many are coming, and each one is
    decided without seeing the others — even though "is this the same Moshe"
    and "is הכפר הירוק a place or an organisation" are both really the same
    question, "did the system understand this recording". One screen shows the
    whole of what was unclear about one recording, which is also the only
    scale at which a producer can tell a small misreading from a big one.
    """
    identity_questions = list(state.get("names_to_check") or [])
    pending_types = type_questions(state.get("extracted_entities") or [])
    if not identity_questions and not pending_types:
        return {}

    answer = interrupt(
        {
            "identity_questions": identity_questions,
            "type_questions": pending_types,
        }
    )

    resolutions = dict(state.get("entity_resolutions") or {})
    identity_answers = answer.get("identity") or {}
    for question in identity_questions:
        name = question["name"]
        given = identity_answers.get(name) or {}
        candidates = question["candidates"]
        if given.get("same_as_existing"):
            chosen_uuid = given.get("candidate_uuid")
            chosen = next((c for c in candidates if c["uuid"] == chosen_uuid), None)
            resolutions[name] = {
                "same_as_uuid": chosen_uuid,
                # Fall back to the raw extracted name only if the resume
                # payload names a uuid that isn't actually one of the pending
                # candidates — shouldn't happen through the API (which
                # validates it), but this keeps the node safe on its own.
                "resolved_name": chosen["name"] if chosen else name,
            }
        else:
            # Covers both "someone new" and an unanswered question. Treating
            # silence as "someone new" is the SAFE default in a way the
            # opposite would not be: it creates a separate entity, which shows
            # up in the extraction panel as two similar names and can be
            # merged later. Defaulting to "same" would silently attribute one
            # person's story to another with nothing in the UI to reveal it.
            resolutions[name] = {"same_as_uuid": None, "resolved_name": name}

    # Types: rewrite the extraction the confirmed answer disagrees with, and
    # clear alternative_type either way — the question has been asked, so it
    # must not be raised again by the writer's needs_confirmation report.
    type_answers = answer.get("types") or {}
    entities = []
    for entity in state.get("extracted_entities") or []:
        entity = dict(entity)
        if entity.get("alternative_type"):
            chosen = type_answers.get(entity["name"])
            if chosen in (entity["type"], entity["alternative_type"]):
                entity["type"] = chosen
            entity["alternative_type"] = None
        entities.append(entity)

    return {
        "names_to_check": [],
        "entity_resolutions": resolutions,
        "extracted_entities": entities,
    }


async def score_importance_node(state: AnalysisState) -> dict:
    segment_id = state["segment_id"]
    transcript = state.get("transcript") or ""
    score = 5.0
    if transcript:
        try:
            raw = await llm_service.generate_response(
                messages=[{"role": "user", "content": transcript}],
                system_prompt=_IMPORTANCE_SYSTEM_PROMPT,
                temperature=0,  # structured scoring — deterministic
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

        entities = _apply_entity_resolutions(
            [
                ExtractedEntity.from_dict(d)
                for d in state.get("extracted_entities") or []
            ],
            state.get("entity_resolutions") or {},
        )

        try:
            await entity_store.write_segment_entities(
                db,
                segment_id=segment_id,
                producer_id=state["group_id"],
                entities=entities,
            )
            # ONE commit for the entities AND the status. entity_store
            # deliberately does not commit, so a recording can never be marked
            # ready behind a half-written entity set — which would be
            # indistinguishable from one that genuinely mentioned nobody.
            segment.status = "ready"
            segment.pending_confirmation = None
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.error(f"finalize_ingest_node failed for segment {segment_id}: {e}")
            return {"error": str(e), "status": "failed"}

    # This recording is now part of the producer's archive. Drop the cached
    # archive/entity-map/units so the very next question sees it, rather than
    # waiting for the version check to notice. A stale cache here would mean a
    # freshly recorded story silently not existing as far as answers go.
    #
    # Then REBUILD it here rather than leaving the next question to do it.
    # Rebuilding used to cost up to ~15s (almost entirely the per-recording
    # Neo4j fan-out) and is now well under a second, but ingestion is offline
    # with nobody waiting — whereas the
    # first person to ask afterwards very much is. Awaited rather than fired
    # and forgotten, so it cannot race that first question into doing the same
    # work twice; warm_archive_cache is internally bounded and fail-soft, so
    # the worst case is simply the old behaviour.
    try:
        from app.services.full_archive_retrieval import (
            invalidate_archive_cache,
            warm_archive_cache,
        )

        invalidate_archive_cache(state["group_id"])
        await warm_archive_cache(state["group_id"])
    except Exception as e:  # never fail ingestion over a cache refresh
        logger.warning(f"Could not refresh archive cache for {state['group_id']}: {e}")

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


def _has_confirmation_questions(state: AnalysisState) -> str:
    """Whether this recording raises ANY question — identity or type.

    Both kinds route to the same single node, which is what makes the
    confirmation batched rather than a sequence.
    """
    if state.get("names_to_check"):
        return "confirm"
    if type_questions(state.get("extracted_entities") or []):
        return "confirm"
    return "skip"


def build_graph(checkpointer):
    graph = StateGraph(AnalysisState)
    graph.add_node("transcribe", transcribe_node)
    graph.add_node("create_transcript_chunks", create_transcript_chunks_node)
    graph.add_node("embed_transcript", embed_transcript_node)
    graph.add_node("extract_topics", extract_topics_node)
    graph.add_node("check_entities", check_entities_node)
    graph.add_node("human_confirm", human_confirm_node)
    graph.add_node("score_importance", score_importance_node)
    graph.add_node("finalize_ingest", finalize_ingest_node)
    graph.add_node("fail", fail_node)

    graph.add_edge(START, "transcribe")
    graph.add_conditional_edges(
        "transcribe", _route_on_error, {"fail": "fail", "next": "create_transcript_chunks"}
    )
    # Prompt 11: purely additive — doesn't read/write anything the avatar
    # path (embed_transcript onward) depends on, and never errors the whole
    # segment (fail-soft internally; see create_transcript_chunks_node).
    graph.add_edge("create_transcript_chunks", "embed_transcript")
    graph.add_edge("embed_transcript", "extract_topics")
    graph.add_edge("extract_topics", "check_entities")
    graph.add_conditional_edges(
        "check_entities",
        _has_confirmation_questions,
        {"confirm": "human_confirm", "skip": "score_importance"},
    )
    # A PLAIN edge onward, not a loop back into human_confirm: one interrupt
    # carries every question this recording raises, so there is never a second
    # one to ask. The self-edge that used to be here is what made a recording
    # with three ambiguities into three modals in sequence.
    graph.add_edge("human_confirm", "score_importance")
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


# Swappable in tests (an InMemorySaver needs no real Postgres).
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
