"""
Pending-prompt voice answering — the mode-agnostic core, shared by BOTH
renderers' WS handlers (extracted from websocket.py on 2026-08-19; the
avatar handler's behavior is pinned unchanged by its existing tests).

When a turn ends with a follow-up offer or a clarify ask, the next
utterance may be the ANSWER to that prompt rather than a fresh question.
Two layers decide, for a FOLLOW-UP offer:

  1. Bare-word fast path: an utterance that IS a plain yes/no (whole
     normalized utterance against the tiny sets below) resolves with zero
     latency and no model involvement.
  2. Everything else goes to `_classify_prompt_reply` — one temperature=0
     LLM call (the `_classify_topic` pattern) choosing among exactly three
     LABELS, never generating output text: accept / decline / unrelated.
     Any error or unparseable reply fails OPEN to "unrelated", which is
     the pre-classifier behavior: route the whole utterance as a fresh
     question, prompt dismissed — the failure mode is the status quo,
     never a wrong action.

Either way the ACTION stays deterministic: accept sends the byte-identical
offered question a button click sends; what a dismiss OUTPUTS is the one
mode-specific piece (avatar speaks a fixed ack; v2 sends a text ack + a
card-dismissal event) and stays in each caller.

CLARIFY prompts deliberately stay literal-option matching only (whole-word
containment, longest option first, so "אמנון נחום" beats its prefix
"אמנון") — the options are proper names: a name either appears in the
reply or the engine's own disambiguation handles the paraphrase.
"""

import logging
import re
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_YES_UTTERANCES = {
    "כן", "כן בבקשה", "בטח", "ברור", "כמובן",
    "yes", "sure", "ok", "okay",
}
_NO_UTTERANCES = {
    "לא", "לא תודה", "לא עכשיו",
    "no", "no thanks", "nope",
}

_PROMPT_REPLY_LABELS = {"accept", "decline", "unrelated"}

_PROMPT_REPLY_SYSTEM_PROMPT = """\
The assistant just offered to tell the listener more, phrased as a question \
(the OFFER). The listener replied (the REPLY), usually in Hebrew. Classify \
the REPLY as exactly one of three labels:

accept — the listener wants the offered material, however phrased \
(e.g. "כן, תספר לי", "אה בטח, למה לא", "ספר לי על זה").
decline — the listener is closing the offer and asking for NOTHING else \
(e.g. "לא בא לי כרגע", "אולי אחר כך, תודה").
unrelated — the reply contains its own request, question, or topic, even if \
it starts with a yes or a no (e.g. "לא, תספר לי על הצבא", "מה לגבי אמא שלך?", \
"לא סיפרת לי על הבית") — the conversation should just answer it.

Answer with one word only: accept, decline, or unrelated."""


def _normalize_utterance(text: str) -> str:
    """Punctuation → spaces, collapsed whitespace, lowercased (no-op for
    Hebrew, correct for English). What both sides of every match see."""
    cleaned = re.sub(r"[^\w\s]", " ", text or "", flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


async def _classify_prompt_reply(offered_question: str, utterance: str) -> str:
    """Bounded 3-label classification, never generation — the model picks
    which server-known action runs; every output string stays byte-identical
    to what the buttons send. Fail-OPEN to \"unrelated\" (= the fresh-question
    fall-through that was the only behavior before this call existed)."""
    from app.services.llm import llm_service

    try:
        raw = await llm_service.generate_response(
            messages=[
                {
                    "role": "user",
                    "content": f"OFFER: {offered_question}\nREPLY: {utterance}",
                }
            ],
            system_prompt=_PROMPT_REPLY_SYSTEM_PROMPT,
            temperature=0,
        )
    except Exception as e:
        logger.warning(f"prompt-reply classification failed (fail-open): {e}")
        return "unrelated"
    label = (raw or "").strip().strip('"').strip("'").strip(".").lower()
    return label if label in _PROMPT_REPLY_LABELS else "unrelated"


def _match_pending_prompt(text: str, pending: dict) -> Optional[dict]:
    """The deterministic layer: the bare-word fast path for follow-ups and
    the ONLY matching clarify gets. Returns {"action": "ask", "text":
    <outgoing question>} — byte-identical to the corresponding button's
    send — or {"action": "dismiss"}, or None (no deterministic match; for a
    follow-up the caller then consults the classifier via `resolve`)."""
    spoken = _normalize_utterance(text)
    if not spoken:
        return None

    kind = pending.get("kind")
    if kind == "follow_up":
        if spoken in _YES_UTTERANCES:
            return {"action": "ask", "text": pending["question"]}
        if spoken in _NO_UTTERANCES:
            return {"action": "dismiss"}
        return None

    if kind == "clarify":
        # Whole-word containment so "דן" can't fire inside "ירדן"; longest
        # option first so the fuller name wins when one contains another.
        padded = f" {spoken} "
        for option in sorted(pending.get("options", []), key=len, reverse=True):
            normalized = _normalize_utterance(option)
            if normalized and f" {normalized} " in padded:
                return {
                    "action": "ask",
                    "text": f"{pending['original']} — {option}",
                }
        return None

    return None


async def resolve(
    pending: dict,
    text: str,
    classify: Optional[Callable[[str, str], Awaitable[str]]] = None,
) -> Optional[dict]:
    """The full two-layer resolution: fast path, then (follow-ups only) the
    injected `classify` callable. `classify` is a parameter, not a direct
    call, so each caller's module-level `_classify_prompt_reply` binding —
    the seam tests monkeypatch — stays authoritative. Returns the same
    action dicts as `_match_pending_prompt`, or None (fresh question)."""
    match = _match_pending_prompt(text, pending)
    if match is None and pending.get("kind") == "follow_up" and classify is not None:
        label = await classify(pending.get("question", ""), text)
        if label == "accept":
            return {"action": "ask", "text": pending["question"]}
        if label == "decline":
            return {"action": "dismiss"}
    return match


def pending_from_result(
    follow_up: Optional[dict],
    clarify: Optional[dict],
    original_question: str,
) -> Optional[dict]:
    """The shared arming rule: what (if anything) a completed turn's result
    leaves pending for the next utterance. Clarify wins when both exist
    (the engine guarantees they don't, but a rule needs an order)."""
    if clarify and clarify.get("options"):
        return {
            "kind": "clarify",
            "options": clarify.get("options", []),
            "original": original_question,
        }
    if follow_up and follow_up.get("question"):
        return {"kind": "follow_up", "question": follow_up["question"]}
    return None
