"""
Side-by-side comparison harness for the two video-clip retrieval paths
(Prompt 15). Runs the SAME question set through both WITHOUT the WebSocket
— direct function calls, stopping at the "which ranges answer this
question" decision (the part that actually differs; the downstream ffmpeg
trim/concat + upload is shared, identical code and is skipped here). For
each question it prints, per mode: the returned ranges, wall-clock latency
of the range decision, number of LLM generate calls made, and estimated
tokens.

  v1 = video_clip_assembler (chunk retrieval: coreference -> perspective
       normalize -> 3-signal chunk match -> leniency -> per-candidate
       verify/pinpoint -> boundary expansion)
  v2 = full_archive_retrieval (single full-archive LLM read -> deterministic
       range validation)

Conversation history is INJECTED (patched onto retrieval_service._recent_
turns) rather than seeded into the DB, so both modes see the exact same
recent turns for the two-turn coreference case and nothing is written to
the live database. Everything else (archive contents, retrieval, the LLM)
is real.

Usage: python scripts/compare_retrieval_modes.py [group_id] [recording_language]
Defaults to the live POC producer + Hebrew.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Tuple

# How many times to run each question per mode, to measure consistency of the
# range decision (set COMPARE_REPEAT to override). With temperature=0 AND the
# fixed sampling seed now pinned in llm.py, repeated runs of the same question
# should produce identical ranges — this is exactly what we're checking.
REPEAT = int(os.environ.get("COMPARE_REPEAT", "3"))

sys.stdout.reconfigure(encoding="utf-8")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import full_archive_retrieval, retrieval_service, video_clip_assembler  # noqa: E402
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
        async def wrapper(messages, system_prompt=None, thinking=False, temperature=None):
            self.calls += 1
            in_chars = len(system_prompt or "") + sum(len(m.get("content", "")) for m in messages)
            out = await self._orig(
                messages, system_prompt=system_prompt, thinking=thinking, temperature=temperature
            )
            self.est_tokens += (in_chars + len(out or "")) // 4
            return out

        llm_service.generate_response = wrapper  # type: ignore[assignment]

    def restore(self) -> None:
        llm_service.generate_response = self._orig  # type: ignore[assignment]


# Injected per-session conversation history (patched onto _recent_turns), so
# both modes see identical recent turns and nothing touches the DB.
_INJECTED_HISTORY: Dict[str, List[Dict[str, str]]] = {}


async def _fake_recent_turns(session_id: str, limit: int) -> List[Dict[str, str]]:
    return _INJECTED_HISTORY.get(session_id, [])[-limit:]


async def _v1_ranges(question: str, group_id: str, lang: str, session_id: str) -> List[ExpandedClip]:
    """v1 range decision only (no ffmpeg) — mirrors assemble_video_clip_
    response's orchestration up to the expanded clips, using existing
    internals without modifying them."""
    candidates = await retrieval_service.retrieve_chunks(question, group_id, lang, session_id)
    if not candidates:
        return []
    clauses = await video_clip_assembler._split_question_into_clauses(question, lang)
    verified = []
    for c in candidates:
        r = await video_clip_assembler._verify_and_pinpoint_chunk(c, question, clauses)
        if r is not None:
            verified.append(r)
    expanded: List[ExpandedClip] = []
    for v in verified:
        expanded.extend(await video_clip_assembler._expand_chunk_boundaries(v))
    return expanded


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


# (label, question, history-for-this-turn) — history is the prior turns this
# question should "see"; the two-turn coreference case gives turn 2 turn 1.
QUESTION_SET: List[Tuple[str, str, List[Dict[str, str]]]] = [
    ("family", "ספר לי על המשפחה שלך", []),
    ("brothers", "מי האחים שלך?", []),
    ("ilana", "מי זאת אילנה?", []),
    ("tzvi", "מי זה צבי?", []),
    ("army", "מה עשית בצבא?", []),
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
]


async def _run_mode_repeated(label, mode_fn, question, group_id, lang, history):
    """Run one mode REPEAT times; return (per-run range strings, consistent?,
    avg latency, calls, est tokens). A fresh session per run so nothing
    (visited-set, cache) bleeds between runs; the injected history is keyed
    to each run's session so the coreference case still sees it."""
    runs: List[str] = []
    latencies: List[float] = []
    calls = tok = 0
    for _ in range(REPEAT):
        session_id = f"compare-{uuid.uuid4()}"
        if history:
            _INJECTED_HISTORY[session_id] = history
        clips, dt, calls, tok = await _run_one(mode_fn, question, group_id, lang, session_id)
        runs.append(_norm_ranges(clips))
        latencies.append(dt)
    consistent = all(r == runs[0] for r in runs)
    return runs, consistent, sum(latencies) / len(latencies), calls, tok


async def main() -> None:
    group_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_GROUP_ID
    lang = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_LANGUAGE

    # Patch history loading for BOTH modes (v1's coreference step and v2's
    # read both call retrieval_service._recent_turns).
    retrieval_service._recent_turns = _fake_recent_turns  # type: ignore[assignment]

    print(f"Comparing retrieval modes for producer {group_id} (lang={lang}), {REPEAT} runs each")
    print("=" * 100)

    v1_stable = v2_stable = 0
    for label, question, history in QUESTION_SET:
        print(f"\n[{label}]  {question}")
        if history:
            print(f"    (history: {history[0]['content']!r})")
        for mode_label, mode_fn in (("v1 chunk-retrieval", _v1_ranges), ("v2 archive-read   ", _v2_ranges)):
            runs, consistent, avg_dt, calls, tok = await _run_mode_repeated(
                label, mode_fn, question, group_id, lang, history
            )
            if consistent and mode_fn is _v1_ranges:
                v1_stable += 1
            if consistent and mode_fn is _v2_ranges:
                v2_stable += 1
            flag = "STABLE " if consistent else "VARIES!"
            print(f"    {mode_label} [{flag}] avg {avg_dt:5.2f}s | {calls:2d} calls | ~{tok:6d} tok")
            if consistent:
                print(f"        all {REPEAT} runs: {runs[0]}")
            else:
                for i, r in enumerate(runs):
                    print(f"        run {i + 1}: {r}")

    total = len(QUESTION_SET)
    print("\n" + "=" * 100)
    print(f"Consistency across {REPEAT} runs:  v1 {v1_stable}/{total} stable   v2 {v2_stable}/{total} stable")
    print("(measures temperature=0 + the newly-pinned fixed sampling seed together.)")
    print("Note: latency/calls/tokens cover the RANGE-DECISION phase only; the downstream")
    print("ffmpeg trim/concat + upload is shared, identical code between the two modes.")


if __name__ == "__main__":
    asyncio.run(main())
