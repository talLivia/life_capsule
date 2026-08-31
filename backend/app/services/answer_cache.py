"""Semantic answer cache (2026-08-31) — skip the archive read entirely for
questions the archive has already answered.

WHY THIS EXISTS (the measured chain that led here, PROJECT_STATUS 2026-08-31):
the archive-read call has a ~12-15s floor at quiet hours and swings to 30-80s
under Google-side load; prompt-shrinking was proven a non-lever (interleaved
A/B: 34.8K-token filtered reads were NOT faster than 111.8K full reads), and
thinking_level knobs were tested live (minimal: catastrophically slower).
The only route to a fast turn is not calling the model on the user's path.

WHAT IT CACHES: the FINAL outcome of `select_units` — the served unit keys
(order preserved) and the follow-up offer's question + unit keys — for a
producer + archive-version fingerprint. NOT model text: everything served
from here is the same verbatim footage a fresh read would have selected.

WHEN IT MAY ANSWER (all enforced in code, each one fail-open to a miss):
  - the toggle is on (`ANSWER_CACHE=on`; default off, and off is INERT —
    no DB read, no embedding call, byte-identical flow);
  - the conversation is FRESH: no shown units and no history turns. Shown
    state and history change what a correct answer is (exhaustion, offers,
    coreference); a cached fresh-conversation answer must never leak into a
    turn that has context. (Session-scoped speculative entries, which DO
    carry context, are milestone 2 and are keyed to their exact session.)
  - the question does not mention a SAME-NAME-AMBIGUOUS person. This is the
    highest-risk corner for a similar-but-wrong hit ("ספר לי על אמנון" vs a
    cached "ספר לי על אמנון החבר"): any question naming a confusable person
    bypasses the cache entirely, both read and write, and takes the full
    engine path with its clarify machinery.
  - cosine similarity to a stored question meets ANSWER_CACHE_THRESHOLD
    (conservative by default; err toward a miss over a wrong-but-similar
    answer).
  - the stored entry's version fingerprint matches the CURRENT archive
    version (the same `_archive_version` tuple the gemini context cache
    keys on — any ingest/delete moves it and orphans every entry), and
    every stored unit key still resolves to a live unit.

Clarifications, failed reads and empty selections are never stored — a
cached answer is always a real, playable, validated selection.

Every hit is logged with similarity, the matched stored question, source
and age, for auditability.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import delete, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import AnswerCacheEntry
from app.services import embeddings
from app.services.core_compression import COMPRESSION_VERSION
from app.services.gemini_cache import version_hash

#: A cached answer embodies the whole production pipeline's behaviour, not
#: just the archive bytes - so the hash that scopes entries folds in the
#: compression version. Bumping COMPRESSION_VERSION auto-orphans every
#: stored answer (they were valid, but no longer match what a fresh read
#: would produce). Module attribute so tests can monkeypatch it.
_PIPELINE_SALT = COMPRESSION_VERSION


def _vh(version: tuple) -> str:
    return version_hash(tuple(version) + (_PIPELINE_SALT,))

logger = logging.getLogger(__name__)

#: Canonical first-turn questions for pre-warming (milestone 3). Server-side
#: because the chat UI has no starter-question list of its own; these are the
#: broad openers family members actually start with. Hebrew — the archive's
#: recording language drives the question language at warm time.
CANONICAL_QUESTIONS_HE = [
    "ספר לי על המשפחה שלך",
    "ספר לי על הילדות שלך",
    "ספר לי מה עשית בצבא",
    "ספר לי על העבודה שלך",
    "איפה נולדת וגדלת?",
]

# Same shape as full_archive_retrieval._annotate_names: one optional Hebrew
# prefix letter, no Hebrew letter on either side.
_HEBREW_PREFIXES = "ובלמשהכ"


def unit_key(u) -> str:
    """MUST byte-match full_archive_retrieval._unit_key (":.2f" on start_sec).
    The live bug this pins: raw float repr ("10.0") vs the canonical "10.00"
    silently failed follow-up resolution on cache hits (2026-08-31)."""
    return f"{u.segment_id}:{u.start_sec:.2f}"


@dataclass
class LookupHit:
    units: List  # resolved UtteranceUnits, stored order
    raw_follow_up: Optional[dict]  # {"question", "unit_ids"} in CURRENT ids
    matched_question: str
    similarity: float
    source: str


def _mentions_name(question: str, name: str) -> bool:
    if not name or not name.strip():
        return False
    pattern = (
        rf"(?<![א-ת])[{_HEBREW_PREFIXES}]?{re.escape(name)}(?![א-ת])"
    )
    return re.search(pattern, question) is not None


def question_names_ambiguous_person(question: str, name_tags: Dict) -> bool:
    """True when the question mentions any same-name-confusable surface.
    Such questions NEVER touch the cache (read or write) — disambiguation
    belongs to the full engine, clarify machinery included."""
    try:
        surfaces = {
            s
            for tags in (name_tags or {}).values()
            for t in tags
            for s in getattr(t, "surfaces", ())
        }
        return any(_mentions_name(question, s) for s in surfaces)
    except Exception:  # malformed tags → treat as ambiguous (safe direction)
        return True


def _fresh_conversation(shown_keys, turns, question: str) -> bool:
    """Fresh = no shown units and no PRIOR conversation. The WS handler
    persists the user's message BEFORE the engine runs, so on the real
    serving path the just-asked question is always the trailing turn —
    that is this turn, not history (found live 2026-08-31: the gate never
    opened over WS while passing in-process). A trailing user turn whose
    content equals the current question is stripped; anything else — an
    assistant answer, a different user message — is real context."""
    if shown_keys:
        return False
    t = list(turns or [])
    try:
        if (
            t
            and t[-1].get("role") == "user"
            and (t[-1].get("content") or "").strip() == question.strip()
        ):
            t = t[:-1]
    except AttributeError:
        return False  # unknown turn shape → safe direction
    return not t


async def try_lookup(
    question: str,
    group_id: str,
    version: Optional[tuple],
    units: List,
    name_tags: Dict,
    shown_keys,
    turns,
) -> Tuple[Optional[List[float]], Optional[LookupHit]]:
    """(embedding_used, hit). (None, None) whenever the cache may not answer.

    The embedding is returned so a MISS's fresh read can be stored without a
    second embedding call; embedding is None exactly when store() must not
    run either (toggle off / not fresh / ambiguous-name guard / no version).
    """
    if settings.ANSWER_CACHE != "on":
        return None, None
    if version is None or not _fresh_conversation(shown_keys, turns, question):
        return None, None
    if question_names_ambiguous_person(question, name_tags):
        logger.info("answer cache BYPASS (same-name-ambiguous question)")
        return None, None
    try:
        q_emb = await embeddings.embed_text(question)
    except Exception as e:
        logger.warning(f"answer cache embedding failed (full read): {e}")
        return None, None
    try:
        vh = _vh(version)
        async with AsyncSessionLocal() as db:
            rows = (
                (
                    await db.execute(
                        select(AnswerCacheEntry).where(
                            AnswerCacheEntry.producer_id == group_id,
                            AnswerCacheEntry.version_hash == vh,
                            AnswerCacheEntry.session_id.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        best, best_sim = None, 0.0
        for r in rows:
            sim = embeddings.cosine_similarity(q_emb, r.question_embedding)
            if sim > best_sim:
                best, best_sim = r, sim
        if best is None or best_sim < settings.ANSWER_CACHE_THRESHOLD:
            return q_emb, None

        # Resolve stored keys against the CURRENT units — every key must
        # resolve or the entry is stale in a way the fingerprint missed.
        by_key = {unit_key(u): u for u in units}
        resolved = [by_key.get(k) for k in (best.unit_keys or [])]
        if not resolved or any(u is None for u in resolved):
            logger.warning(
                "answer cache entry had unresolvable unit keys; full read"
            )
            return q_emb, None
        raw_fu = None
        fu = best.follow_up or None
        if isinstance(fu, dict) and fu.get("question") and fu.get("unit_keys"):
            fu_units = [by_key.get(k) for k in fu["unit_keys"]]
            if all(u is not None for u in fu_units):
                raw_fu = {
                    "question": fu["question"],
                    "unit_ids": [u.unit_id for u in fu_units],
                }
        logger.info(
            f"answer cache HIT sim={best_sim:.3f} source={best.source} "
            f"hits={best.hit_count + 1} matched={best.question_text!r} "
            f"for={question!r}"
        )
        try:
            async with AsyncSessionLocal() as db:
                row = await db.get(AnswerCacheEntry, best.id)
                if row is not None:
                    row.hit_count = (row.hit_count or 0) + 1
                    row.last_hit_at = datetime.now(timezone.utc)
                    await db.commit()
        except Exception:
            pass  # metrics only
        return q_emb, LookupHit(
            units=resolved,
            raw_follow_up=raw_fu,
            matched_question=best.question_text,
            similarity=best_sim,
            source=best.source,
        )
    except Exception as e:
        logger.warning(f"answer cache lookup failed (full read): {e}")
        return q_emb, None


def _resolve_entry(entry, units) -> Optional[LookupHit]:
    """Resolve a stored entry against the CURRENT units; None = stale."""
    by_key = {unit_key(u): u for u in units}
    resolved = [by_key.get(k) for k in (entry.unit_keys or [])]
    if not resolved or any(u is None for u in resolved):
        return None
    raw_fu = None
    fu = entry.follow_up or None
    if isinstance(fu, dict) and fu.get("question") and fu.get("unit_keys"):
        fu_units = [by_key.get(k) for k in fu["unit_keys"]]
        if all(u is not None for u in fu_units):
            raw_fu = {
                "question": fu["question"],
                "unit_ids": [u.unit_id for u in fu_units],
            }
    return LookupHit(
        units=resolved,
        raw_follow_up=raw_fu,
        matched_question=entry.question_text,
        similarity=1.0,
        source=entry.source,
    )


async def prewarm(group_id: str) -> None:
    """Milestone 3: after an ingest settles, run the canonical first-turn
    questions through the FULL engine so the semantic cache is populated
    before anyone asks. Each run goes through select_units with a fresh
    synthetic session (no shown-state, no history), so the normal
    lookup/store hooks do all the work: an already-warm question hits and
    stores nothing; a cold one reads and stores. Fire-and-forget, gated on
    the toggle, never raises — a failed warm just means the first asker
    pays the normal read."""
    if settings.ANSWER_CACHE != "on":
        return
    try:
        import uuid

        from app.models import User
        from app.services import full_archive_retrieval as far

        async with AsyncSessionLocal() as db:
            user = await db.get(User, group_id)
        language = getattr(user, "recording_language", None) or "he"
        for q in CANONICAL_QUESTIONS_HE:
            try:
                sel = await far.select_units(q, group_id, language, str(uuid.uuid4()))
                logger.info(
                    f"answer cache prewarm {q!r}: "
                    f"{'failed-read' if sel.read_failed else f'{len(sel.selected_units)} units'}"
                )
            except Exception as e:
                logger.warning(f"prewarm question failed ({q!r}): {e}")
    except Exception as e:
        logger.warning(f"answer cache prewarm failed (ignored): {e}")


async def take_speculative(
    question: str,
    group_id: str,
    session_id: str,
    version: Optional[tuple],
    units: List,
) -> Optional[LookupHit]:
    """Serve-and-CONSUME a speculative follow-up prefetch (milestone 2).

    Session-scoped entries are the one place a cached answer may serve into
    a conversation WITH context: the prefetch ran through the full engine
    with this session's own shown-state/history the moment the offer
    shipped, and acceptance is the immediately following turn, so the state
    it was computed under is the state it serves under. Match is EXACT
    question text (the offer button/voice-accept sends the byte-identical
    offered question) + session + version — no similarity, no threshold,
    no cross-session reuse, and the entry is deleted on first use (or on
    any mismatch staleness)."""
    if settings.ANSWER_CACHE != "on" or version is None:
        return None
    try:
        vh = _vh(version)
        async with AsyncSessionLocal() as db:
            row = (
                (
                    await db.execute(
                        select(AnswerCacheEntry).where(
                            AnswerCacheEntry.producer_id == group_id,
                            AnswerCacheEntry.session_id == session_id,
                            AnswerCacheEntry.question_text == question,
                            AnswerCacheEntry.version_hash == vh,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                return None
            hit = _resolve_entry(row, units)
            await db.delete(row)  # one-shot either way
            await db.commit()
        if hit is not None:
            logger.info(
                f"answer cache SPECULATIVE HIT session={session_id[:8]} "
                f"q={question!r}"
            )
        return hit
    except Exception as e:
        logger.warning(f"speculative take failed (full read): {e}")
        return None


async def store(
    question: str,
    embedding: Optional[List[float]],
    group_id: str,
    version: Optional[tuple],
    served_units: List,
    follow_up_keys: Optional[dict],
    source: str = "live",
    session_id: Optional[str] = None,
) -> None:
    """Persist a served answer. Callers pass the embedding try_lookup already
    computed; embedding=None means the gates said this turn is uncacheable —
    store() then does nothing. Never raises."""
    if settings.ANSWER_CACHE != "on":
        return
    if embedding is None or version is None or not served_units:
        return
    try:
        vh = _vh(version)
        keys = [unit_key(u) for u in served_units]
        async with AsyncSessionLocal() as db:
            # One entry per exact question text per version (re-warm updates
            # rather than accumulating duplicates).
            await db.execute(
                delete(AnswerCacheEntry).where(
                    AnswerCacheEntry.producer_id == group_id,
                    AnswerCacheEntry.version_hash == vh,
                    AnswerCacheEntry.question_text == question,
                    AnswerCacheEntry.session_id.is_(None)
                    if session_id is None
                    else AnswerCacheEntry.session_id == session_id,
                )
            )
            db.add(
                AnswerCacheEntry(
                    producer_id=group_id,
                    session_id=session_id,
                    version_hash=vh,
                    question_text=question,
                    question_embedding=embedding,
                    unit_keys=keys,
                    follow_up=follow_up_keys,
                    source=source,
                )
            )
            await db.commit()
        logger.info(
            f"answer cache STORE source={source} units={len(keys)} "
            f"q={question!r}"
        )
    except Exception as e:
        logger.warning(f"answer cache store failed (answer unaffected): {e}")
