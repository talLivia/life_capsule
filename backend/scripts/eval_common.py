"""
Shared fixtures and runners for the retrieval eval scripts
(prompt_regression, rebaseline_accuracy, seed_sweep, eval_name_disambiguation,
eval_no_story_subject).

These pieces used to live in compare_retrieval_modes.py, which doubled as
the fleet's common module until the v1 mode — and with it the comparison
harness — was removed (docs/V1_REMOVAL_PLAN.md). What survives here is
exactly what the remaining scripts share: the question set, the live POC
defaults, the injected-history mechanism (patched onto
retrieval_service._recent_turns so nothing touches the database), the LLM
call meter, and the single-run v2 range decision.

Import as `import eval_common as crm` — the historical alias keeps the
call sites readable against old measurement notes.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Tuple

sys.stdout.reconfigure(encoding="utf-8")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import full_archive_retrieval  # noqa: E402
from app.services.llm import llm_service  # noqa: E402
from app.services.video_clip_assembler import ExpandedClip  # noqa: E402

DEFAULT_GROUP_ID = "79820a49-b07d-41fe-941b-f5ceba09f7b5"
DEFAULT_LANGUAGE = "he"


class LLMCallMeter:
    """Wraps llm_service.generate_response to count calls and roughly
    estimate tokens (chars/4) over a measured block. ~ because we don't
    tokenize; this is for order-of-magnitude comparison, not billing."""

    def __init__(self) -> None:
        self.calls = 0
        self.est_tokens = 0
        self._orig = llm_service.generate_response

    def install(self) -> None:
        # **kwargs, not a fixed signature: generate_response gains per-call
        # options over time (temperature, then `model`), and a fixed signature
        # here turns each new one into a TypeError that the callers' fail-soft
        # silently reports as "no story" — which is exactly how it looked when
        # ARCHIVE_READ_MODEL was added.
        async def wrapper(messages, system_prompt=None, thinking=False, temperature=None, **kwargs):
            self.calls += 1
            in_chars = len(system_prompt or "") + sum(len(m.get("content", "")) for m in messages)
            out = await self._orig(
                messages,
                system_prompt=system_prompt,
                thinking=thinking,
                temperature=temperature,
                **kwargs,
            )
            self.est_tokens += (in_chars + len(out or "")) // 4
            return out

        llm_service.generate_response = wrapper  # type: ignore[assignment]

    def restore(self) -> None:
        llm_service.generate_response = self._orig  # type: ignore[assignment]


# Injected per-session conversation history (patched onto _recent_turns), so
# every run sees identical recent turns and nothing touches the DB.
_INJECTED_HISTORY: Dict[str, List[Dict[str, str]]] = {}


async def _fake_recent_turns(session_id: str, limit: int) -> List[Dict[str, str]]:
    return _INJECTED_HISTORY.get(session_id, [])[-limit:]


async def _v2_ranges(question: str, group_id: str, lang: str, session_id: str) -> List[ExpandedClip]:
    return await full_archive_retrieval.read_and_validate_ranges(question, group_id, lang, session_id)


def _norm_ranges(clips: List[ExpandedClip]) -> str:
    """Order-preserving, rounded string form of a run's ranges — the unit of
    comparison for 'did two runs produce the same answer'. Word-boundary
    snapping is deterministic given the same chunk data, so an identical
    model decision yields an identical string here."""
    if not clips:
        return "(no-story)"
    return ", ".join(f"{c.raw_segment_id[:8]}:{c.start_sec:.1f}-{c.end_sec:.1f}" for c in clips)


async def _run_one(mode_fn, question, group_id, lang, session_id) -> Tuple[List[ExpandedClip], float, int, int]:
    meter = LLMCallMeter()
    meter.install()
    t0 = time.perf_counter()
    try:
        clips = await mode_fn(question, group_id, lang, session_id)
    finally:
        meter.restore()
    elapsed = time.perf_counter() - t0
    return clips, elapsed, meter.calls, meter.est_tokens


def fresh_session_id() -> str:
    return f"eval-{uuid.uuid4()}"


# (label, question, history-for-this-turn) — history is the prior turns this
# question should "see"; the two-turn coreference case gives turn 2 turn 1.
QUESTION_SET: List[Tuple[str, str, List[Dict[str, str]]]] = [
    ("family", "ספר לי על המשפחה שלך", []),
    ("brothers", "מי האחים שלך?", []),
    ("ilana", "מי זאת אילנה?", []),
    ("tzvi", "מי זה צבי?", []),
    ("army", "מה עשית בצבא?", []),
    # Montreal: the mid-thought-cut case. The answer spans TWO recordings
    # (the post-army flight + the programming studies), and the second one
    # must run to the end of the thought ("...and when I came back to Israel
    # I kept working in it") rather than stopping after "I studied programming".
    ("montreal", "מה לך ולעיר מונטריאול?", []),
    # Narrow vs broad on the SAME topic — breadth must fall out of the
    # question alone, with no length rule anywhere in the code.
    ("army-narrow", "באיזה תפקיד שירתת בצבא?", []),
    ("army-broad", "ספר לי על התקופה שלך בצבא", []),
    ("influence-1", "מי הדמות הכי משפיעה בילדות שלך?", []),
    (
        "influence-2 (followup)",
        "הוא עדיין בחיים?",
        [
            {"role": "user", "content": "מי הדמות הכי משפיעה בילדות שלך?"},
            {"role": "assistant", "content": "http://localhost:8000/uploads/video-clips/x.mp4"},
        ],
    ),
    ("school", "מה למדת בבית הספר?", []),
    ("no-answer", "איזה חיות מחמד היו לך?", []),
    # ── added 2026-08-20 with the gap-filling recordings (docs/
    # VALIDATION_COVERAGE_PLAN.md §2). rebaseline_accuracy filters by
    # REFERENCES, so these join the drift panel without touching the
    # accuracy metric. ──
    # A second/third broad domain, so core-vs-offer work can't be tuned
    # against the single family case it was designed around.
    ("career-broad", "ספר לי על הקריירה שלך", []),
    ("childhood-broad", "ספר לי על הילדות שלך", []),
    # The spouse recording (relationships_q03) — she is never NAMED in the
    # archive, so the follow-up's "עליה" must resolve purely from history:
    # the harder, more realistic pronoun case.
    ("spouse", "ספר לי על בת הזוג שלך", []),
    (
        "spouse-pronoun (followup)",
        "ספר לי עוד עליה",
        [
            {"role": "user", "content": "ספר לי על בת הזוג שלך"},
            {"role": "assistant", "content": "http://localhost:8000/uploads/video-clips/y.mp4"},
        ],
    ),
]
