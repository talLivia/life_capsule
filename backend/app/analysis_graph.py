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
from sqlalchemy import delete, select, update

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import InterviewSession, RawSegment, TranscriptChunk, User
from app.services import embeddings, entity_extraction, entity_store
from app.services.entity_extraction import ExtractedEntity, ExtractedRelation
from app.services.entity_names import names_are_similar as _names_are_similar
from app.services.entity_names import normalize_entity_name
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
    # Family relations this recording PROPOSED, as plain dicts (same
    # serialisation reason as extracted_entities). Proposals only — nothing is
    # written until the producer confirms, because a silent wrong relation in a
    # family tree is worse than an unanswered question.
    proposed_relations: List[Dict[str, Any]]
    # Siblings still to be asked whose child they are, plus the producer's
    # recorded parents to offer as answers. Resolved from the DATABASE, not
    # from anything the model read — see parentage_questions.
    parentage: Dict[str, Any]
    # Aunts and uncles still needing a side, plus the parents to offer.
    sides: Dict[str, Any]
    # Which of those the producer accepted, keyed by the same index the
    # confirmation screen showed. Absent entirely when they skipped — which is
    # a real answer, not a missing one (relations are skippable by decision).
    relation_answers: Dict[str, bool]
    importance_score: float
    status: str
    # Type changes the producer's answers actually caused, as
    # [{name, was, now}] — surfaced by the confirm endpoint so an answer is
    # visibly applied rather than silently absorbed.
    # Normalised names that already have a year, or were already asked for one
    # and skipped. Resolved in check_entities_node where the DB is open.
    year_settled: List[str]
    applied_type_changes: List[Dict[str, Any]]
    error: str


_EXTRACT_TOPICS_SYSTEM_PROMPT = """\
You are a strict content classifier for a personal life-story archive. \
Given a transcript of someone recounting a memory, output ONLY a JSON \
array of short topic tags (1-3 words each, lowercase, in the SAME \
language as the transcript) describing what the story is actually \
about - its real content, not the interview question that prompted it. \
Do not include any commentary, explanation, or text outside the JSON \
array.

The example below is ENGLISH because this instruction is written in \
English. Do NOT copy its language - it shows the SHAPE of the reply, not \
the language of the tags. Tag a Hebrew transcript in Hebrew, a Spanish one \
in Spanish, and so on.

Example shape only: ["military service", "friendship", "loss"]"""

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


# ── progress reporting ────────────────────────────────────────────────────
#
# The producer watches the extraction screen while this runs (Phase 6 of
# docs/FAMILY_TREE_TIMELINE.md). Without a stage they get a spinner for the
# length of a real extraction, which reads as stuck.
#
# Applied by WRAPPING the nodes at graph-construction time rather than by a
# line at the top of each one. Eight call sites would be eight chances for a
# new node to be added without a stage, and the wrapper makes that impossible:
# a node registered through `_staged` has a stage or it does not compile.
#
# Labels are what the producer reads, not node names. Several nodes share one
# label deliberately — "Reading it back" covers chunking and embedding, which
# are one idea to everybody who is not maintaining this file.
# Roughly how far through the run each stage is, as a percentage.
#
# Weighted by MEASURED duration, not by node count: transcription dominates
# (Deepgram ~2s, Whisper far longer), so an evenly-spaced bar would sprint to
# 60% and then appear to hang. These are honest about where the time goes —
# a bar that stalls at a number nobody expects is worse than no bar.
STAGE_PERCENT = {
    "transcribe": 15,
    "create_transcript_chunks": 45,
    "embed_transcript": 55,
    "extract_topics": 65,
    "check_entities": 85,
    "human_confirm": 100,
    "score_importance": 92,
    "finalize_ingest": 97,
}

STAGE_LABELS = {
    "transcribe": "Listening to your recording",
    "create_transcript_chunks": "Reading it back",
    "embed_transcript": "Reading it back",
    "extract_topics": "Finding the themes",
    "check_entities": "Finding the people and places",
    "human_confirm": "Waiting for you",
    "score_importance": "Filing it away",
    "finalize_ingest": "Filing it away",
}


async def _set_progress_stage(segment_id: str, stage: Optional[str]) -> None:
    """Best-effort. A progress label is never worth failing an ingest over."""
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(RawSegment)
                .where(RawSegment.id == segment_id)
                .values(progress_stage=stage)
            )
            await db.commit()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"could not set progress stage {stage!r}: {e}")


def _staged(stage: str, node):
    """Record which node is running, then run it."""

    async def wrapped(state: AnalysisState) -> dict:
        await _set_progress_stage(state["segment_id"], stage)
        return await node(state)

    wrapped.__name__ = getattr(node, "__name__", stage)
    return wrapped


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
    # Family relations come out of the SAME call — a second pass over the same
    # transcript could name people this one did not extract, and the relation
    # would then point at an entity that never gets created. The vocabulary
    # comes from relation_types, so adding a type needs no prompt edit.
    async with AsyncSessionLocal() as db:
        relation_vocabulary = await entity_store.get_relation_vocabulary(db)
        # Who is narrating. Without it the model cannot tell which of twelve
        # names is the one telling the story, and extracts the producer as a
        # person in their own archive — measured 2/2 before, 0/2 after.
        speaker_name = await entity_store.speaker_name_for(db, group_id)
    extracted, proposed = await entity_extraction.extract(
        transcript, relation_vocabulary, speaker_name
    )
    # The producer is not somebody IN their archive — they are who it belongs
    # to. The prompt says so, and this enforces it, because a prompt cannot be
    # the only guard on something that silently forks the tree root.
    async with AsyncSessionLocal() as db:
        extracted, proposed = await entity_store.fold_speaker_into_self(
            db, group_id, extracted, proposed, entity_extraction.SELF
        )
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
            # Plus anyone this producer's OTHER unanswered recordings have
            # already named. Entities are written after the confirmation
            # pause, so a recording analysed while an earlier one waits on a
            # human sees none of its people — which silently fragmented
            # "איציק" and "איציק כהן" into two strangers with no question
            # asked. See pending_entity_candidates for why a candidate with
            # no row still resolves correctly.
            candidates = candidates + [
                pending
                for pending in await entity_store.pending_entity_candidates(
                    db, group_id, segment_id
                )
                # A name the archive already holds is a better candidate than
                # the same name from an unfinished recording — it has a row,
                # a summary and a history.
                if not any(
                    normalize_entity_name(pending["name"])
                    == normalize_entity_name(existing["name"])
                    for existing in candidates
                )
            ]
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

    # Which of these names must never be asked for a year again. Resolved
    # HERE, where the database is already open, rather than in human_confirm
    # which is a pure function over state.
    async with AsyncSessionLocal() as db:
        year_settled = await entity_store.names_with_year_settled(
            db, group_id, [e.name for e in extracted]
        )
        # Nothing to do with THIS recording: these are siblings confirmed by
        # earlier ones that still have no parent recorded. Resolved here for
        # the same reason year_settled is — the database is already open, and
        # human_confirm stays a pure function over state.
        # Passed this recording's PROPOSED relations, not just what the
        # archive already holds. A producer's first recording is the one that
        # names their parents and siblings, so a DB-only question could never
        # fire on it — which is exactly what happened four times running.
        proposed_dicts = [r.as_dict() for r in proposed]
        parentage = await entity_store.parentage_candidates(
            db, group_id, proposed_dicts, entity_extraction.SELF
        )
        sides = await entity_store.aunt_uncle_candidates(
            db, group_id, proposed_dicts, entity_extraction.SELF
        )
        # Grandparents ask the SAME question — "which of your parents is this
        # person on the side of?" — and differ only in the edge the answer
        # writes, so they join the same grouped question rather than adding a
        # second near-identical screen. `kind` is what tells them apart.
        grandparents = await entity_store.grandparent_candidates(
            db, group_id, proposed_dicts, entity_extraction.SELF
        )
        sides = _merge_side_candidates(sides, grandparents)
        # What a WRONG relation may be corrected to. Both read here for the
        # same reason as the two above — the database is open and
        # human_confirm stays a pure function over state.
        #
        # Not derived from `parentage` even though it also carries people:
        # that returns nothing at all when the producer has no recorded
        # parents, and a misclassified relation still needs correcting then.
        correction_people = await entity_store.people_for_correction(
            db, group_id, proposed_dicts, entity_extraction.SELF
        )
        # The SAME vocabulary the extractor may propose from. A correction that
        # could name a type extraction can never produce would be two lists to
        # keep in step; the FK is the backstop either way.
        correction_types = await entity_store.get_relation_vocabulary(db)

    return {
        "names_to_check": to_check,
        "entity_resolutions": auto_resolutions,
        "extracted_entities": [e.as_dict() for e in extracted],
        "proposed_relations": [r.as_dict() for r in proposed],
        "year_settled": sorted(year_settled),
        "parentage": parentage,
        "sides": sides,
        "correction_people": correction_people,
        "correction_types": correction_types,
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


# Which entities can carry a year. Everything the extractor is actually asked
# to classify — a person has a birth year, a place a year you moved there, an
# organisation a year you joined it.
#
# `other` is excluded on purpose. It is the fallback for a name the extractor
# could not classify at all, and asking for the year of something we do not
# understand is noise in a screen whose whole value is that it only asks what
# genuinely needs an answer.
YEAR_QUESTION_TYPES = ("person", "place", "organisation", "event")


def year_questions(
    extracted: List[Dict[str, Any]], settled: Optional[set] = None
) -> List[Dict[str, Any]]:
    """The year questions this recording raises. ASKED AT MOST ONCE PER ENTITY.

    `settled` holds the normalised names that must not be asked again — either
    the entity already has a year, or the producer was already asked and did
    not give one. Skipping is a real answer ("I do not know"), so a name that
    has been offered once is never offered again, however many later
    recordings mention it. Without that, widening beyond `event` would put the
    same questions in front of the producer on every single recording until
    they learned to click past the whole screen.

    Also skipped when this batch already carries a year for the entity, which
    is only reachable on a re-run of an already-answered confirmation.
    """
    settled = settled or set()
    return [
        {
            "name": e["name"],
            "type": e["type"],
            "question": f'Roughly what year was "{e["name"]}"? (optional)',
        }
        for e in extracted
        if e.get("type") in YEAR_QUESTION_TYPES
        and not e.get("year_start")
        and normalize_entity_name(e["name"]) not in settled
    ]


def relation_questions(proposed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The relation questions this recording raises — one per proposal.

    Unlike type questions, EVERY proposal is asked about. There is no
    equivalent of `alternative_type` to gate on: the extractor either found a
    stated relation or it did not, and a relation it is confident about is
    exactly the kind that lands wrong in a family tree if nobody looks.

    Indexed rather than keyed by name: two people can share a relation type to
    the speaker ("ניר ורז הם אחים שלי"), so name alone does not identify a
    proposal.
    """
    return [
        {
            "index": i,
            "from_name": r["from_name"],
            "to_name": r["to_name"],
            "relation_type": r["relation_type"],
            "evidence": r.get("evidence"),
        }
        for i, r in enumerate(proposed)
    ]


def _canonical_person(person: Dict[str, Any], *, sibling: bool = False) -> Dict[str, Any]:
    """One spelling for a person in the parentage payload.

    `entity_id` is None for anyone this recording has only just named. For a
    sibling, `recorded` says whether their sibling relation already exists —
    an older payload has no such field, and an entity that came back from a
    database query is one that did.
    """
    entity_id = person.get("entity_id", person.get("id"))
    out: Dict[str, Any] = {"name": person["name"], "entity_id": entity_id}
    if sibling:
        out["recorded"] = person.get("recorded", entity_id is not None)
    return out


def parentage_questions(parentage: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Whose child is each sibling — ASKED AT MOST ONCE PER SIBLING, EVER.

    A sibling is recorded as a sibling OF THE PRODUCER, which says nothing
    about whose child they are. The family tree can therefore place them in
    the right generation and still draw no line to them, which is correct and
    looks broken. This is the question that fills that in.

    Derived from the DATABASE (entity_store.parentage_candidates), never from
    the model. Nothing about the extraction prompt changes to support it, so
    none of the breadth measurements in CLAUDE.md are put at risk.

    Deliberately about siblings confirmed by EARLIER recordings, not the ones
    being proposed on this same screen. A sibling proposed here is not in the
    database yet, and waiting for the answer would mean a second interrupt —
    the one thing human_confirm_node exists to avoid. The cost is that a newly
    confirmed sibling is asked about on the NEXT recording; the benefit is
    that the rule holds and the existing backlog gets asked at all.

    Checkboxes per parent rather than "same as you / different", because a
    half-sibling shares ONE parent and a yes/no cannot say which.
    """
    parentage = parentage or {}
    # Canonicalised because these dicts arrive from three places with two
    # spellings: freshly from entity_store, from a LangGraph checkpoint written
    # by an older build, and from a persisted pending_confirmation. The old
    # shape said "id"; the current one says "entity_id". Normalising once here
    # means nothing downstream has to know that.
    parents = [_canonical_person(p) for p in parentage.get("parents") or []]
    siblings = [_canonical_person(s, sibling=True) for s in parentage.get("siblings") or []]
    known_people = [_canonical_person(p) for p in parentage.get("known_people") or []]
    if not parents or not siblings:
        return []

    names = [s["name"] for s in siblings]
    listed = (
        names[0]
        if len(names) == 1
        else f"{', '.join(names[:-1])} and {names[-1]}"
    )
    parent_names = (
        parents[0]["name"]
        if len(parents) == 1
        else f"{' and '.join(p['name'] for p in parents[:2])}"
        if len(parents) == 2
        else f"{', '.join(p['name'] for p in parents[:-1])} and {parents[-1]['name']}"
    )

    return [
        {
            # ONE question for the whole set. Asking per sibling produced four
            # near-identical screens whose answer was the same each time, which
            # is how a producer learns to click past a screen without reading —
            # and that is how a question that DOES matter gets missed.
            "question": f"Are {listed} all children of {parent_names}?",
            "siblings": siblings,
            "parents": parents,
            # Nested inside the question rather than a payload key of its own:
            # the client counts every array in the payload to decide whether to
            # render, so a top-level list of people would be counted as
            # questions — the miscounting behind two earlier bugs.
            "known_people": known_people,
        }
    ]


# Every question class, in ONE place.
#
# THE ROUTER AND THE NODE MUST NEVER DISAGREE, and for three phases they did.
# `_has_confirmation_questions` checked identity and type only, while
# human_confirm_node's interrupt carried relations (Phase 2), years (Phase 3)
# and parentage (Phase 6) as well. A recording that raised no identity or type
# question therefore routed straight past confirmation — and because the node
# is what narrows `proposed_relations` to the ACCEPTED subset, finalize wrote
# every proposed relation unasked. That is the exact "nothing is auto-applied"
# rule this pipeline exists to keep, broken silently, and it looked like
# success: a recording that ingests cleanly and shows no questions.
#
# Deriving both from this function is what makes it structural rather than
# remembered. A sixth class added here is asked about AND gates the route; a
# sixth class added anywhere else does not exist. Same fix, and same reason, as
# sharing `_chosen_option` between resolve_steps and category_is_settled.
def _merge_side_candidates(
    aunts: Dict[str, Any], grandparents: Dict[str, Any]
) -> Dict[str, Any]:
    """One question covering both kinds, each relative carrying its own.

    Either query returns `{"parents": [], "relatives": []}` when it has nothing
    to ask, INCLUDING when the producer has no recorded parents — so the parent
    list is taken from whichever side actually found one rather than from a
    fixed precedence, which would drop the options when only the other kind
    qualifies.
    """
    relatives = [
        {**r, "kind": entity_store.AUNT_UNCLE_RELATION}
        for r in aunts.get("relatives") or []
    ]
    known = {r["name"] for r in relatives}
    relatives += [
        {**r, "kind": entity_store.GRANDPARENT_RELATION}
        for r in grandparents.get("relatives") or []
        # A person recorded as both is asked once, as the closer relation.
        if r["name"] not in known
    ]
    parents = (aunts.get("parents") or []) or (grandparents.get("parents") or [])
    if not relatives or not parents:
        return {"parents": [], "relatives": []}
    return {"parents": parents, "relatives": relatives}


def side_questions(sides: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Which of the producer's parents a relative attaches to. ONE question.

    An `aunt_uncle` edge places somebody in the parents' row and says nothing
    about which of the two parents they belong to, so the row ends up as four
    boxes with no way to tell the parents from their siblings. A `grandparent`
    edge has the identical gap one generation up: it places somebody in the
    grandparents' row and draws their only line to the PRODUCER, skipping the
    parent between, which reads on the chart as a grandparent floating
    unattached to either parent. Found on live data — יוכבד and ג'ולי, both
    correctly captured, both correctly placed, neither connected.

    Both kinds are asked together because it is the same question. Only the
    edge the answer writes differs, and `kind` on each relative carries that
    through to `write_sides`.

    Grouped from the start rather than after four repetitions — see the
    parentage question, which learned that the expensive way.
    """
    sides = sides or {}
    parents = [_canonical_person(p) for p in sides.get("parents") or []]
    relatives = [
        # `kind` is not part of _canonical_person's contract, so it is carried
        # across explicitly rather than hoping the helper preserves it.
        {**_canonical_person(r, sibling=True), "kind": r.get("kind") or "aunt_uncle"}
        for r in sides.get("relatives") or []
    ]
    if not parents or not relatives:
        return []

    names = [r["name"] for r in relatives]
    listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return [
        {
            "question": f"Which side of the family are {listed} on?",
            "relatives": relatives,
            "parents": parents,
        }
    ]


def normalise_pending_confirmation(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Bring a stored payload up to the shape the current client expects.

    `pending_confirmation` is persisted JSON, so a segment can pause under one
    build and be answered under another. Parentage went from one question per
    sibling to one grouped question, and a client written for the second
    crashed on the first.

    Upgraded rather than dropped: the questions are still worth asking, and the
    producer paused mid-flow should not lose them because we changed our mind
    about the shape. Anything already current passes straight through.
    """
    if not payload:
        return payload or {}
    questions = payload.get("parentage_questions") or []
    if not questions or "siblings" in questions[0]:
        return payload

    first = questions[0]
    upgraded = parentage_questions(
        {
            "parents": first.get("parents") or [],
            # One entry per sibling in the old shape; the name is on the
            # question itself rather than in a list.
            "siblings": [
                {"name": q["name"], "entity_id": q.get("entity_id"), "recorded": True}
                for q in questions
                if q.get("name")
            ],
            "known_people": first.get("known_people") or [],
        }
    )
    return {**payload, "parentage_questions": upgraded}


def build_confirmation_payload(state: AnalysisState) -> Dict[str, List[Dict[str, Any]]]:
    """Every question this recording raises. Empty lists where it raises none."""
    return {
        "identity_questions": list(state.get("names_to_check") or []),
        "type_questions": type_questions(state.get("extracted_entities") or []),
        # A THIRD class, and deliberately a different one: identity and type
        # must be answered (both silent defaults are dangerous), while
        # relations are skippable. An unanswered relation is simply not
        # stored, which is the status quo and harmless.
        "relation_questions": relation_questions(state.get("proposed_relations") or []),
        # Skippable, like relations and for the same reason: an unanswered
        # year has a real empty outcome — no year stored, timeline unaffected.
        "year_questions": year_questions(
            state.get("extracted_entities") or [], set(state.get("year_settled") or [])
        ),
        # Skippable too, and the ONLY class not raised by this recording:
        # these are siblings from earlier recordings who still have no parent.
        # Skipping is recorded (parentage_asked_at) so it is asked once.
        "parentage_questions": parentage_questions(state.get("parentage")),
        # A SIXTH class, and it cost nothing to add: the router reads this
        # function and the client counts every array in the payload, so both
        # picked it up without being touched. That is what the two structural
        # fixes bought.
        "side_questions": side_questions(state.get("sides")),
    }


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
    payload = build_confirmation_payload(state)
    identity_questions = payload["identity_questions"]
    pending_types = payload["type_questions"]
    pending_relations = payload["relation_questions"]
    pending_years = payload["year_questions"]
    pending_parentage = payload["parentage_questions"]
    pending_sides = payload["side_questions"]
    if not any(payload.values()):
        # Reached only when the router and this node disagree, which they
        # cannot now that both read build_confirmation_payload. Kept so the
        # node is still correct if called directly.
        return {}

    answer = interrupt(
        {
            **payload,
            # NOT a question, and deliberately not part of `payload`: every
            # entity this recording named, so any of them can be corrected.
            # Counting it as a question would pause on every recording that
            # named anybody; leaving it out of the screen means a confidently
            # misheard name has nowhere to be fixed.
            "editable_entities": [
                {"name": e["name"], "type": e.get("type")}
                for e in state.get("extracted_entities") or []
            ],
            # Also NOT questions, and for the same reason: they are the
            # OPTIONS a relation question is answered with, not something
            # being asked. Counted as questions they would pause the pipeline
            # on every recording that named a person, and inflate "3 things to
            # check" with the contents of a dropdown.
            "correction_people": state.get("correction_people") or [],
            "correction_types": [
                # snake_case in the table, people words on the screen. Done
                # here so one place decides how a type reads.
                {"value": relation_type, "label": relation_type.replace("_", " ")}
                for relation_type in state.get("correction_types") or []
            ],
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
            resolutions[name] = {
                "same_as_uuid": None,
                "resolved_name": corrected_name(name),
            }

    # Names the producer corrected outright. Applied BEFORE anything else
    # reads a name, because two other things are keyed by it.
    #
    # The gap this closes: the extractor can be confidently wrong. "אליאן" came
    # back as "ליאן" — a brand-new name with nothing similar to disambiguate
    # against, so it raised no identity question and there was no screen on
    # which it could be fixed. Confidence and correctness are different things.
    raw_edits = answer.get("name_edits") or {}
    name_edits = {
        original: corrected.strip()
        for original, corrected in raw_edits.items()
        if isinstance(corrected, str) and corrected.strip() and corrected.strip() != original
    }

    def corrected_name(name: str) -> str:
        return name_edits.get(name, name)

    # Types: rewrite the extraction the confirmed answer disagrees with, and
    # clear alternative_type either way — the question has been asked, so it
    # must not be raised again by the writer's needs_confirmation report.
    type_answers = answer.get("types") or {}
    # Years arrive already parsed — the API turned the producer's free text
    # into an int, or refused it and told them. Nothing unparsed reaches here,
    # so there is no guessing left to do at this point.
    year_answers = answer.get("years") or {}
    # Every entity the screen ASKED about, answered or not.
    asked_year_names = {q["name"] for q in pending_years}
    entities = []
    for entity in state.get("extracted_entities") or []:
        entity = dict(entity)
        original_name = entity["name"]
        if entity.get("alternative_type"):
            chosen = type_answers.get(entity["name"])
            if chosen in (entity["type"], entity["alternative_type"]):
                entity["type"] = chosen
                # Mark it as the PRODUCER's answer, not the extractor's guess.
                # The writer keeps an existing type against a guess but must
                # yield to a human — without this flag the two are
                # indistinguishable and the answer is silently discarded.
                entity["type_confirmed"] = True
            entity["alternative_type"] = None
        if original_name in asked_year_names:
            # Stamped whether or not they answered — the stamp is what stops
            # the question coming back on every later recording.
            entity["year_asked"] = True
            if original_name in year_answers:
                entity["year_start"] = year_answers[original_name]
        # Renamed LAST, so every answer above still matches the name the
        # screen asked about. Writing it under the corrected name merges onto
        # an existing entity if one already has it, which is the right
        # outcome — a correction that splits the person in two would be worse
        # than the misspelling.
        entity["name"] = corrected_name(original_name)
        entities.append(entity)

    # Relations: keep only what was explicitly ACCEPTED. Anything else —
    # declined, or skipped entirely — is simply not stored.
    #
    # The default here is the opposite of the identity default, on purpose.
    # An unanswered identity question resolves to "someone new" because
    # silence has to mean SOMETHING and a false split is the recoverable
    # direction. An unanswered relation has a genuinely empty outcome
    # available: storing nothing leaves the archive exactly as it was, which
    # is why relations could be made skippable and identity could not.
    raw_relation_answers = answer.get("relations") or {}
    # Corrections, keyed by proposal INDEX like the acceptances — two people
    # can hold the same relation to the speaker, so a name does not identify a
    # proposal.
    raw_relation_edits = answer.get("relation_edits") or {}

    def _edit_for(i: int) -> Optional[Dict[str, Any]]:
        return raw_relation_edits.get(str(i)) or raw_relation_edits.get(i)

    def _accepted(i: int) -> bool:
        """A CORRECTED relation is accepted by virtue of being corrected.

        Requiring the tick as well would mean a producer who fixed a wrong
        relation and did not also tick it lost the correction — silently, the
        same shape as the parentage answer that was thrown away for needing an
        acceptance it was busy contradicting. Correcting a proposal IS saying
        it should exist, in the corrected form.
        """
        if _edit_for(i):
            return True
        return raw_relation_answers.get(str(i)) is True or raw_relation_answers.get(i) is True

    def _resolve(relation: Dict[str, Any], i: int) -> Dict[str, Any]:
        # Endpoints carry NAMES, and write_segment_relations resolves them by
        # looking the entity up. Renaming an entity without rewriting the
        # relations that point at it would leave those endpoints unresolvable
        # — the relation would be dropped with a log line nobody reads, which
        # is precisely the silent-failure shape to avoid.
        edit = _edit_for(i) or {}
        return {
            **relation,
            "relation_type": edit.get("relation_type") or relation["relation_type"],
            "from_name": corrected_name(edit.get("from_name") or relation["from_name"]),
            "to_name": corrected_name(edit.get("to_name") or relation["to_name"]),
        }

    # ── The parentage answer OWNS whether they are a sibling ────────────────
    #
    # A sibling relation and a parentage answer used to be collected
    # independently and both stored, so "he is my brother" and "he is רז's
    # child" could both be written — and the tree, unable to honour both, kept
    # the first and reported the second as a contradiction. From the outside
    # that looked exactly like the chosen parent failing to save.
    #
    # There is no reason the sibling relation needs to survive its own
    # correction, so it does not: the parentage answer decides, and the
    # contradiction becomes unconstructible rather than caught.
    #
    # THE RULE IS "SHARES NO PARENT WITH YOU", not "picked a different parent".
    # A half-sibling shares ONE parent and is still a sibling — the case that
    # made this checkboxes rather than a yes/no in the first place, and the
    # case a naive "any other parent means not a sibling" would silently
    # destroy. The parents this question OFFERS are exactly the producer's own
    # (`parentage_candidates` builds them from parent edges to the self
    # entity), so ticking any of them means a shared parent, and naming only
    # someone else means none.
    parentage_answers = {
        corrected_name(name): given
        for name, given in (answer.get("parentage") or {}).items()
    }
    offered_parent_keys = {
        normalize_entity_name(parent["name"])
        for question in pending_parentage
        for parent in question.get("parents") or []
    }
    offered_parent_keys.discard("")

    def _answered_parentage(name: str) -> bool:
        """Did the producer say something specific about this person?

        The acceptance rule below is right about SILENCE and wrong about an
        explicit answer, which is how a real correction was silently thrown
        away: told "בני is your brother — and is he אילנה and צבי's child?",
        a producer whose בני is actually a NEPHEW declines the sibling and
        names the real parent. That is one statement made with two controls,
        and reading the second only when the first was accepted discards it.
        """
        given = parentage_answers.get(name) or {}
        return bool(given.get("parent_names") or (given.get("new_parent_name") or "").strip())

    def _shares_a_parent(name: str) -> bool:
        given = parentage_answers.get(name) or {}
        keys = {normalize_entity_name(n) for n in given.get("parent_names") or []}
        typed = normalize_entity_name((given.get("new_parent_name") or "").strip())
        if typed:
            keys.add(typed)
        keys.discard("")
        return bool(keys & offered_parent_keys)

    # Answered, and none of the named parents is one of the producer's own.
    # Silence is NOT included: skipping the question changes nothing, exactly
    # as it does everywhere else on this screen.
    not_siblings = {
        name
        for name in parentage_answers
        if _answered_parentage(name) and not _shares_a_parent(name)
    }

    def _replaced_by_parentage(relation: Dict[str, Any]) -> bool:
        if relation.get("relation_type") != entity_store.SIBLING_RELATION:
            return False
        endpoints = (relation.get("from_name"), relation.get("to_name"))
        if entity_extraction.SELF not in endpoints:
            return False
        other = endpoints[0] if endpoints[1] == entity_extraction.SELF else endpoints[1]
        return other in not_siblings

    accepted = [
        relation
        for relation in (
            _resolve(r, i)
            for i, r in enumerate(state.get("proposed_relations") or [])
            if _accepted(i)
        )
        # Dropped rather than written and then deleted: this recording's own
        # proposal has not been stored yet, so the replacement costs nothing.
        if not _replaced_by_parentage(relation)
    ]

    # Parentage: carry both the answers and the full list of siblings the
    # screen asked about. finalize_ingest needs the second even when the first
    # is empty — a skipped question still has to be stamped as asked, or it
    # comes back on every future recording.
    # ── Everything below travels under the CORRECTED names ──────────────────
    #
    # The entities were renamed a few lines above, and every downstream step
    # resolves people by NAME: `write_parentage` and `write_sides` look the
    # entity up, and the acceptance sets here already hold corrected names
    # because they are built from `accepted`. Carrying the screen's original
    # spelling into either would compare "גבי" against a set holding "גבינון"
    # and then search for a "גבי" that no longer exists — the answer dropped
    # with a warning nobody reads.
    #
    # Confirmed on two real recordings: "גבי" corrected to "גבינון" and told
    # he was רז's child, and the same shape for "יונתן"/"יוני". Both left the
    # person floating in the tree. The client keys its answers by the name the
    # SCREEN asked about, which is the only name it knows, so the remapping
    # has to happen here — at the one point that knows both spellings.
    side_answers = {
        corrected_name(name): given
        for name, given in (answer.get("sides") or {}).items()
    }
    # Either relation qualifies its own kind: an aunt/uncle proposal makes the
    # aunt/uncle question answerable, a grandparent proposal the grandparent
    # one. Keyed by kind so accepting one does not unlock the other.
    accepted_by_kind = {
        entity_store.AUNT_UNCLE_RELATION: set(),
        entity_store.GRANDPARENT_RELATION: set(),
    }
    for relation in accepted:
        kind = relation.get("relation_type")
        if kind not in accepted_by_kind:
            continue
        if entity_extraction.SELF not in (relation.get("from_name"), relation.get("to_name")):
            continue
        other = (
            relation["from_name"]
            if relation["to_name"] == entity_extraction.SELF
            else relation["to_name"]
        )
        accepted_by_kind[kind].add(other)

    asked_sides = [
        corrected_name(relative["name"])
        for question in pending_sides
        for relative in question["relatives"]
        if relative.get("recorded")
        or corrected_name(relative["name"])
        in accepted_by_kind.get(relative.get("kind") or entity_store.AUNT_UNCLE_RELATION, set())
    ]
    # Which edge each answer writes. Carried from the question the producer
    # actually saw, not re-derived, so it cannot disagree with what was asked.
    side_kinds = {
        corrected_name(relative["name"]): relative.get("kind")
        or entity_store.AUNT_UNCLE_RELATION
        for question in pending_sides
        for relative in question["relatives"]
    }

    # A sibling this recording only PROPOSED is asked about on the same screen
    # — that is what makes the question work on a first recording. But their
    # parentage may only be written if the sibling relation itself was
    # accepted: otherwise declining "ניר is my brother" while answering the
    # grouped question would record ניר's parents anyway.
    accepted_sibling_names = {
        r["from_name"] if r["to_name"] == entity_extraction.SELF else r["to_name"]
        for r in accepted
        if r.get("relation_type") == entity_store.SIBLING_RELATION
        and entity_extraction.SELF in (r.get("from_name"), r.get("to_name"))
    }

    asked_parentage = [
        corrected_name(sibling["name"])
        for question in pending_parentage
        for sibling in question["siblings"]
        if sibling.get("recorded")
        or corrected_name(sibling["name"]) in accepted_sibling_names
        or _answered_parentage(corrected_name(sibling["name"]))
    ]

    return {
        "names_to_check": [],
        "entity_resolutions": resolutions,
        "extracted_entities": entities,
        # Overwritten with the accepted subset, so finalize_ingest writes
        # exactly what the producer approved and nothing else.
        "proposed_relations": accepted,
        "parentage": {
            **(state.get("parentage") or {}),
            "asked_names": asked_parentage,
            "answers": parentage_answers,
            # Whose sibling relation this answer REPLACES. Dropping the
            # proposal above covers one this recording raised; a sibling
            # recorded by an EARLIER recording already has a row, and only a
            # delete can retract it.
            "not_sibling_names": sorted(not_siblings),
        },
        "sides": {
            **(state.get("sides") or {}),
            "asked_names": asked_sides,
            "answers": side_answers,
            "kinds": side_kinds,
        },
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
            write_result = await entity_store.write_segment_entities(
                db,
                segment_id=segment_id,
                producer_id=state["group_id"],
                entities=entities,
            )
            applied_type_changes = [
                {"name": n, "was": was, "now": now}
                for n, was, now in write_result.type_changes
            ]
            if write_result.type_changes:
                # Logged as well as returned: the producer sees it in the UI,
                # and anyone reading logs can see an answer took effect.
                logger.info(
                    f"Producer type answers applied on segment {segment_id}: "
                    f"{write_result.type_changes}"
                )
            # Relations AFTER entities, in the same transaction: an endpoint is
            # resolved by looking up the entity row, which the call above is
            # what creates. Only ever the confirmed subset — human_confirm
            # replaced proposed_relations with what the producer accepted.
            await entity_store.write_segment_relations(
                db,
                segment_id=segment_id,
                producer_id=state["group_id"],
                relations=[
                    ExtractedRelation.from_dict(d)
                    for d in state.get("proposed_relations") or []
                ],
                self_marker=entity_extraction.SELF,
            )
            # Parentage last: it may create a parent entity of its own, and it
            # must run AFTER write_segment_relations, whose replace-by-segment
            # delete is scoped to origin="recording" precisely so these
            # survive a re-analysis.
            sides = state.get("sides") or {}
            if sides.get("asked_names"):
                await entity_store.write_sides(
                    db,
                    producer_id=state["group_id"],
                    segment_id=segment_id,
                    asked_names=sides.get("asked_names") or [],
                    answers=sides.get("answers") or {},
                    kinds=sides.get("kinds") or {},
                )

            parentage = state.get("parentage") or {}
            if parentage.get("asked_names"):
                await entity_store.write_parentage(
                    db,
                    producer_id=state["group_id"],
                    segment_id=segment_id,
                    asked_sibling_names=parentage.get("asked_names") or [],
                    answers=parentage.get("answers") or {},
                    not_sibling_names=parentage.get("not_sibling_names") or [],
                )

            # ONE commit for the entities AND the status. entity_store
            # deliberately does not commit, so a recording can never be marked
            # ready behind a half-written entity set — which would be
            # indistinguishable from one that genuinely mentioned nobody.
            segment.status = "ready"
            segment.pending_confirmation = None
            # The run is over; a stale stage would leave the extraction screen
            # claiming work is still happening.
            segment.progress_stage = None
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

    # Carried out of the graph so the confirm endpoint can tell the producer
    # what their answer actually did. Without it the answer takes effect
    # invisibly, which is only marginally better than not taking effect.
    return {"status": "ready", "applied_type_changes": applied_type_changes}


async def fail_node(state: AnalysisState) -> dict:
    segment_id = state["segment_id"]
    async with AsyncSessionLocal() as db:
        segment, _user = await _load_segment_and_user(db, segment_id)
        if segment is not None:
            segment.status = "failed"
            segment.pending_confirmation = None
            segment.progress_stage = None
            await db.commit()
    logger.error(f"analysis_graph failed for segment {segment_id}: {state.get('error')}")
    return {"status": "failed"}


def _route_on_error(state: AnalysisState) -> str:
    return "fail" if state.get("error") else "next"


def _has_confirmation_questions(state: AnalysisState) -> str:
    """Whether this recording raises ANY question, of ANY class.

    Reads build_confirmation_payload — the same function the node interrupts
    with — so the gate cannot fall behind the questions again. See the comment
    there for what happened when it did.
    """
    return "confirm" if any(build_confirmation_payload(state).values()) else "skip"


def build_graph(checkpointer):
    graph = StateGraph(AnalysisState)
    graph.add_node("transcribe", _staged("transcribe", transcribe_node))
    graph.add_node("create_transcript_chunks", _staged("create_transcript_chunks", create_transcript_chunks_node))
    graph.add_node("embed_transcript", _staged("embed_transcript", embed_transcript_node))
    graph.add_node("extract_topics", _staged("extract_topics", extract_topics_node))
    graph.add_node("check_entities", _staged("check_entities", check_entities_node))
    graph.add_node("human_confirm", _staged("human_confirm", human_confirm_node))
    graph.add_node("score_importance", _staged("score_importance", score_importance_node))
    graph.add_node("finalize_ingest", _staged("finalize_ingest", finalize_ingest_node))
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
            segment.progress_stage = None
        await db.commit()


async def run_segment_analysis(segment_id: str) -> Dict[str, Any]:
    """Kick off the pipeline for a freshly-ingested segment (called by the
    Celery task enqueued from `/segments/ingest`, Prompt 4)."""
    async with AsyncSessionLocal() as db:
        segment, user = await _load_segment_and_user(db, segment_id)
    if segment is None or user is None:
        logger.error(f"run_segment_analysis: segment {segment_id} not found")
        return {"status": "failed"}

    try:
        async with _open_checkpointer() as checkpointer:
            graph = build_graph(checkpointer)
            result = await graph.ainvoke(
                {"segment_id": segment_id, "group_id": user.id},
                config=_thread_config(segment_id),
            )
            await _sync_segment_from_result(segment_id, result)
            return result
    except Exception:
        # STRANDED OTHERWISE, AND INVISIBLY.
        #
        # `_sync_segment_from_result` is the ONLY thing that moves a segment
        # off its initial status, and it runs after `ainvoke` returns. If the
        # run dies — a dropped checkpointer connection, a killed worker, a
        # crash inside a node — the row keeps `pending_transcription` while
        # `progress_stage` shows how far it got. That state appears NOWHERE:
        # /talk loads `status == "ready"`, the notification bell queries
        # `status == "pending_confirmation"`, and this is neither. The producer
        # records a story and nothing anywhere says it is missing.
        #
        # Seen on a real recording (1ffc53b7, stage=human_confirm), found only
        # because the producer noticed the content was absent.
        #
        # Marking it failed does not recover it, and is not meant to: it makes
        # it VISIBLE. `failed` already renders as "Something went wrong
        # processing this" in the recordings list, and
        # `scripts/recover_lost_segments.py` re-runs it from the stored
        # transcript.
        logger.exception(f"Segment analysis crashed for {segment_id}; marking failed")
        try:
            async with AsyncSessionLocal() as db:
                crashed = (
                    await db.execute(select(RawSegment).where(RawSegment.id == segment_id))
                ).scalar_one_or_none()
                if crashed is not None and crashed.status not in (
                    "ready",
                    "analyzed",
                    "pending_confirmation",
                ):
                    crashed.status = "failed"
                    crashed.progress_stage = None
                    await db.commit()
        except Exception:
            # The database is the thing that just failed, most likely. Nothing
            # further to try, and re-raising here would hide the original.
            logger.exception(f"Could not mark segment {segment_id} as failed")
        raise


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
