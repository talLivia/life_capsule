"""Coarse recording pre-filter for over-ceiling archives (PREFILTER_PLAN,
built 2026-08-25).

INERT BY CONSTRUCTION below the budget: `apply` returns None (meaning "no
filtering") whenever the toggle is off OR the whole archive fits the char
budget — the caller then renders exactly what it always rendered, byte for
byte. Above the budget it admits WHOLE recordings, ranked by cosine
similarity between the question embedding and each recording's stored
transcript embedding, until the budget fills; the admitted set is rendered
in unchanged archive order (unit ids and keys are untouched — filtering
happens AFTER _build_units, so a unit keeps its global id regardless of
which recordings are admitted).

Force-included regardless of rank (correctness, not relevance):
  * recordings with no stored embedding (fail-soft inclusion),
  * every recording carrying a same-name tag (disambiguation needs all
    same-named people's recordings present to tell them apart),
  * recordings referenced by the conversation's shown-state.

PINNED PER CONVERSATION: the admitted set is computed on the session's
first filtered question and reused, so the prompt prefix — and the
explicit Gemini cache keyed on (version, set_hash) — stays stable across
a conversation. A question whose top-ranked recording falls outside the
pinned set EXPANDS the set (new prefix, one full-price call): a price
event, never silent answering from the wrong material.

A filtered read may never assert archive-wide absence: callers must
consult `covers_entity` / `low_confidence` before emitting the specific
"no more stories about X" line (see _no_story_line's guard).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from app.config import settings
from app.services import embeddings

logger = logging.getLogger(__name__)

#: Similarity floor: below this, a recording is "not obviously relevant";
#: if NOTHING clears it the whole read is flagged low-confidence. Set from
#: a quick sweep on the live archive (top-1 sims for on-topic questions ran
#: ~0.55-0.75; off-topic ~0.15-0.25) — re-measure when the embedding model
#: changes (PREFILTER_PLAN §6).
SIMILARITY_FLOOR = 0.30

#: session_id -> (version_key, frozenset of admitted segment ids)
_PINNED: Dict[str, Tuple[str, frozenset]] = {}


@dataclass
class PrefilterResult:
    admitted: frozenset  # segment ids to render
    excluded: int  # how many recordings were left out
    low_confidence: bool  # nothing cleared the floor / no ranking signal
    expanded: bool  # this question grew the pinned set
    set_hash: str  # folds into the explicit-cache identity


def _version_key(version: Optional[tuple]) -> str:
    return repr(tuple(version)) if version is not None else "?"


def _set_hash(admitted: frozenset) -> str:
    return hashlib.sha256(repr(sorted(admitted)).encode()).hexdigest()[:12]


def _chars(item) -> int:
    return sum(len(c.text or "") for c in item.chunks)


def reset_pins() -> None:
    _PINNED.clear()


async def apply(
    question: str,
    session_id: Optional[str],
    archive: List,
    name_tags: Dict[str, list],
    shown_keys: set,
    version: Optional[tuple],
) -> Optional[PrefilterResult]:
    """None = no filtering (inert). Anything else = render only `admitted`."""
    if settings.PREFILTER != "on":
        return None
    total = sum(_chars(a) for a in archive)
    if total <= settings.PREFILTER_CHAR_BUDGET:
        return None

    shown_segs = {k.rsplit(":", 1)[0] for k in (shown_keys or set())}
    forced = {
        a.segment.id
        for a in archive
        if a.segment.embedding is None
        or a.segment.id in name_tags
        or a.segment.id in shown_segs
    }

    # Rank everything (cheap arithmetic; the one API call is the question).
    sims: Dict[str, float] = {}
    low_confidence = False
    try:
        q_emb = await embeddings.embed_text(question)
        for a in archive:
            if a.segment.embedding is not None:
                sims[a.segment.id] = embeddings.cosine_similarity(
                    q_emb, a.segment.embedding
                )
        if not sims or max(sims.values()) < SIMILARITY_FLOOR:
            low_confidence = True
    except Exception as e:
        logger.warning(f"prefilter ranking unavailable ({e}); admitting in archive order")
        low_confidence = True

    vkey = _version_key(version)
    pinned = _PINNED.get(session_id) if session_id else None
    expanded = False
    if pinned is not None and pinned[0] == vkey:
        admitted = set(pinned[1])
        # Topical-drift check: this question's best recordings must be in
        # the pinned set; otherwise expand (grow-only — the set may exceed
        # the budget after expansion; bounded by one conversation's topics).
        wanted = {
            s for s, v in sims.items() if v >= SIMILARITY_FLOOR
        } or set(sims)  # low-confidence: the ranked order still guides
        top = sorted(wanted, key=lambda s: -sims.get(s, 0.0))[:3]
        missing = [s for s in top if s not in admitted]
        if missing:
            admitted.update(missing)
            expanded = True
    else:
        admitted = set(forced)
        budget = settings.PREFILTER_CHAR_BUDGET
        used = sum(_chars(a) for a in archive if a.segment.id in admitted)
        order = (
            sorted(sims, key=lambda s: -sims[s])
            if sims and not low_confidence
            else [a.segment.id for a in archive]  # archive order fallback
        )
        for seg_id in order:
            if seg_id in admitted:
                continue
            size = next(_chars(a) for a in archive if a.segment.id == seg_id)
            if used + size > budget:
                continue
            admitted.add(seg_id)
            used += size

    admitted_f = frozenset(admitted)
    if session_id:
        _PINNED[session_id] = (vkey, admitted_f)
    result = PrefilterResult(
        admitted=admitted_f,
        excluded=len(archive) - len(admitted_f),
        low_confidence=low_confidence,
        expanded=expanded,
        set_hash=_set_hash(admitted_f),
    )
    logger.info(
        f"prefilter: {len(admitted_f)}/{len(archive)} recordings admitted "
        f"({result.excluded} excluded, low_confidence={low_confidence}, "
        f"expanded={expanded}, set={result.set_hash})"
    )
    return result


def covers_entity(result: Optional[PrefilterResult], segment_ids: List[str]) -> bool:
    """May an exhaustion-style claim about this entity be made? True only
    when no filtering happened, or every one of the entity's recordings was
    admitted AND the ranking was trusted."""
    if result is None:
        return True
    if result.low_confidence:
        return False
    return all(s in result.admitted for s in segment_ids)
