"""
One-time seed sweep to PICK the fixed sampling seed for llm.py (follow-up to
the determinism fix). A fixed seed makes Gemini reproducible, but different
seed VALUES settle on different (each still stable) answers — so we choose
the value that is both stable AND most accurate against known-correct
answers, not just the first one tried.

Reuses the comparison harness's mode helpers (compare_retrieval_modes),
sweeps seeds by patching app.services.llm._DETERMINISTIC_SEED (read at call
time, so reassigning the module global takes effect), runs the full
question set through both modes per seed, and scores accuracy by IoU
(intersection-over-union of the returned time range vs the known-correct
range) on the questions where we have a confirmed answer. "brothers" (the
tight 3.3-12.3 siblings range) is the primary benchmark.

Usage: python scripts/seed_sweep.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for compare_retrieval_modes
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for app.*

import app.services.llm as llm_mod  # noqa: E402
import compare_retrieval_modes as crm  # noqa: E402
from app.services import retrieval_service  # noqa: E402
from app.services.video_clip_assembler import ExpandedClip  # noqa: E402

# Defaults to evaluating ONLY the currently-pinned seed — the seed optimum
# was found to chase prompt-specific noise, so we no longer re-sweep it as a
# matter of course (set SWEEP_SEEDS="0,7,42,100" to force a real sweep).
SEEDS = [int(x) for x in os.environ.get("SWEEP_SEEDS", "").split(",") if x.strip()] or [
    llm_mod._DETERMINISTIC_SEED
]

# Known-correct references (full segment id, (start_sec, end_sec)) for the
# questions whose right answer we've already confirmed live. None => the
# correct answer is no-story (nothing in the archive answers it).
_BROTHERS_SEG = "502fb283-8f10-4ba2-adb3-d8dc6dc16f24"  # siblings + parents recording
_FATHER_SEG = "1d32a9b5-603e-4b51-9d17-e962149888a5"  # most-influential-figure recording

REFERENCES: dict[str, Optional[Tuple[str, Tuple[float, float]]]] = {
    "brothers": (_BROTHERS_SEG, (3.3, 12.3)),  # PRIMARY benchmark — the tight siblings range
    "ilana": (_BROTHERS_SEG, (14.2, 16.6)),  # the "צבי ואילנה" parents-naming clause
    "tzvi": (_BROTHERS_SEG, (14.2, 16.6)),  # same parents-naming clause (Zvi = the father)
    "influence-1": (_FATHER_SEG, (0.8, 5.2)),  # the father segment
    "influence-2 (followup)": None,  # father's aliveness isn't recorded -> no-story
    "no-answer": None,  # nothing about pets -> no-story
}


def _cover(clips: List[ExpandedClip], seg: str) -> Optional[Tuple[float, float]]:
    on = [(c.start_sec, c.end_sec) for c in clips if c.raw_segment_id == seg]
    if not on:
        return None
    return (min(s for s, _ in on), max(e for _, e in on))


def _score(clips: List[ExpandedClip], ref) -> float:
    """1.0 = perfect. For a no-story reference, correct iff empty. Otherwise
    IoU of the returned coverage (on the reference segment) vs the reference
    range — penalizes both missing the answer and over-including irrelevant
    surrounding speech."""
    if ref is None:
        return 1.0 if not clips else 0.0
    seg, (rs, re) = ref
    cover = _cover(clips, seg)
    if cover is None:
        return 0.0
    ov = max(0.0, min(cover[1], re) - max(cover[0], rs))
    un = max(cover[1], re) - min(cover[0], rs)
    return ov / un if un > 0 else 0.0


def _fmt(clips: List[ExpandedClip]) -> str:
    if not clips:
        return "(no-story)"
    return ", ".join(f"{c.raw_segment_id[:8]}:{c.start_sec:.1f}-{c.end_sec:.1f}" for c in clips)


async def _ranges(mode_fn, question, group_id, lang, history) -> List[ExpandedClip]:
    session_id = f"sweep-{uuid.uuid4()}"
    if history:
        crm._INJECTED_HISTORY[session_id] = history
    clips, _, _, _ = await crm._run_one(mode_fn, question, group_id, lang, session_id)
    return clips


async def main() -> None:
    group_id, lang = crm.DEFAULT_GROUP_ID, crm.DEFAULT_LANGUAGE
    retrieval_service._recent_turns = crm._fake_recent_turns  # type: ignore[assignment]

    print(f"Seed sweep over {SEEDS} — accuracy = IoU vs known-correct (brothers is primary)")
    print("=" * 100)

    # seed -> {"v1": mean_acc, "v2": mean_acc, "brothers_v1": str, "brothers_v2": str}
    summary: dict[int, dict] = {}

    for seed in SEEDS:
        llm_mod._DETERMINISTIC_SEED = seed
        print(f"\n### seed = {seed}")
        v1_scores: List[float] = []
        v2_scores: List[float] = []
        brothers = {"v1": "", "v2": ""}
        for label, question, history in crm.QUESTION_SET:
            v1c = await _ranges(crm._v1_ranges, question, group_id, lang, history)
            v2c = await _ranges(crm._v2_ranges, question, group_id, lang, history)
            ref = REFERENCES.get(label, "unscored")
            if ref != "unscored":
                s1, s2 = _score(v1c, ref), _score(v2c, ref)
                v1_scores.append(s1)
                v2_scores.append(s2)
                mark = f"  [v1 IoU={s1:.2f} | v2 IoU={s2:.2f}]"
            else:
                mark = "  [unscored]"
            if label == "brothers":
                brothers = {"v1": _fmt(v1c), "v2": _fmt(v2c)}
            print(f"  {label:24s} v1: {_fmt(v1c):40s} v2: {_fmt(v2c):28s}{mark}")

        v1_acc = sum(v1_scores) / len(v1_scores)
        v2_acc = sum(v2_scores) / len(v2_scores)
        summary[seed] = {"v1": v1_acc, "v2": v2_acc, **{f"brothers_{k}": v for k, v in brothers.items()}}
        print(f"  --> mean accuracy: v1={v1_acc:.3f}  v2={v2_acc:.3f}")

    print("\n" + "=" * 100)
    print(f"{'seed':>6} | {'v1 acc':>7} | {'v2 acc':>7} | {'combined':>8} | brothers v2 (target 502fb283:3.3-12.3)")
    print("-" * 100)
    best_seed, best_combined = None, -1.0
    for seed in SEEDS:
        s = summary[seed]
        combined = (s["v1"] + s["v2"]) / 2
        if combined > best_combined:
            best_combined, best_seed = combined, seed
        print(f"{seed:>6} | {s['v1']:>7.3f} | {s['v2']:>7.3f} | {combined:>8.3f} | {s['brothers_v2']}")

    print("-" * 100)
    print(f"Best combined accuracy: seed={best_seed} (combined={best_combined:.3f})")
    print("Pick the seed that is both accurate AND stable; stability is confirmed separately")
    print("(the fixed-seed mechanism itself was already shown 8/8 stable at seed=7).")


if __name__ == "__main__":
    asyncio.run(main())
