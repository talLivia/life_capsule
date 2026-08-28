"""Conversation-sizing an oversized core selection (2026-08-29).

A CODE-gated, isolated post-processing step — the main archive-read prompt
is untouched (its recalibration attempt failed its gate: wording cannot be
fenced by scale, code can). Under the threshold nothing here runs and the
flow is byte-identical to today. Over it, ONE bounded extra LLM call picks
the conversation-sized subset of the ALREADY-SELECTED core and routes the
remainder into the follow-up offer — the same {unit_ids, follow_up} shape
the main call produces, under the same never-invent contract: ids in,
subset of those ids out, enforced in code; the model can narrow the served
answer, never add to it.

Fail-open everywhere: any error, empty or unparseable reply serves the
ORIGINAL selection (today's behaviour). The one thing this must never do
is turn an answer into silence.

Precedent for the shape: pending_prompt's classifier — small, isolated,
bounded, fail-open, layered after the engine rather than baked into it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import List, Optional, Tuple

from app.config import settings
from app.services.llm import llm_service

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You shorten an OVERSIZED answer from a person's life-story video archive.
You are given the question and the currently selected units (id, duration,
text). Far too much was selected for one natural conversational reply.

Pick the subset of unit ids that answers the question the way a person
would in ONE spoken turn — roughly {target_sec} seconds of speech, keeping
whole coherent stretches (never orphan a sentence mid-thought). Then pick
ONE natural topic-cluster from the REMAINING units as a follow-up offer,
with a short question in the storyteller's own first-person voice inviting
the listener to hear it.

Use ONLY unit ids that appear in the list. Output ONLY JSON, exactly:
{{"unit_ids": ["..."], "follow_up": {{"question": "...", "unit_ids": ["..."]}}}}
"""


def core_duration_sec(units: List) -> float:
    """Exact playable length of a selection — the trigger metric. Duration,
    not unit count: 188 short units and 30 long ones are different answers."""
    return sum(max(0.0, u.end_sec - u.start_sec) for u in units)


def _parse(raw: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except Exception:
        return None
    return out if isinstance(out, dict) else None


async def maybe_compress(
    question: str, units: List, language: str
) -> Tuple[List, Optional[dict], bool]:
    """(served_units, raw_follow_up_or_None, compressed?).

    Under CORE_COMPRESSION_THRESHOLD_SEC (or with the feature off) this
    returns the input list UNTOUCHED with no LLM call — the deterministic
    gate that the failed prompt recalibration could not provide."""
    threshold = settings.CORE_COMPRESSION_THRESHOLD_SEC
    if not threshold or not units:
        return units, None, False
    total = core_duration_sec(units)
    if total <= threshold:
        return units, None, False

    by_id = {u.unit_id: u for u in units}
    lines = [
        f'{u.unit_id} [{u.end_sec - u.start_sec:.0f}s] "{u.text}"' for u in units
    ]
    user_msg = (
        f"Question:\n{question}\n\nSelected units "
        f"({total:.0f}s total, far too long):\n" + "\n".join(lines)
    )
    try:
        raw = await llm_service.generate_response(
            messages=[{"role": "user", "content": user_msg}],
            system_prompt=_SYSTEM_PROMPT.format(
                target_sec=settings.CORE_COMPRESSION_TARGET_SEC
            ),
            temperature=0,
        )
        parsed = _parse(raw)
    except Exception as e:
        logger.warning(f"core compression call failed (serving full core): {e}")
        return units, None, False
    if not parsed:
        logger.warning("core compression reply unparseable (serving full core)")
        return units, None, False

    # NEVER-INVENT ENFORCEMENT: only ids from the input survive, order is
    # the ARCHIVE's (input order), and an empty result fails open — this
    # step may narrow the answer, never invent or erase it.
    kept_ids = {i for i in (parsed.get("unit_ids") or []) if i in by_id}
    kept = [u for u in units if u.unit_id in kept_ids]
    if not kept:
        logger.warning("core compression kept nothing (serving full core)")
        return units, None, False

    raw_fu = parsed.get("follow_up") or None
    if isinstance(raw_fu, dict):
        fu_ids = [
            i for i in (raw_fu.get("unit_ids") or [])
            if i in by_id and i not in kept_ids
        ]
        raw_fu = (
            {"question": raw_fu.get("question"), "unit_ids": fu_ids}
            if fu_ids and raw_fu.get("question")
            else None
        )
    else:
        raw_fu = None

    logger.info(
        f"core compressed: {len(units)} units/{total:.0f}s -> "
        f"{len(kept)} units/{core_duration_sec(kept):.0f}s, "
        f"offer {len((raw_fu or {}).get('unit_ids', []))} units"
    )
    return kept, raw_fu, True
