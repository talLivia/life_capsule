"""Re-baseline v2's accuracy as a MEAN OVER RUNS, not a single number.

The archive-read call is knowingly non-deterministic on marginal unit choices
(a thinking model's reasoning path isn't seed-controlled â€” see llm.py's
_DETERMINISTIC_SEED note), so quoting one run's figure overstates precision.
This runs the scored question set N times and reports mean / stdev / min / max
per question and overall, plus how many DISTINCT answers each question
produced â€” which is the honest picture of the variance we accepted.

Usage: python scripts/rebaseline_accuracy.py            (N=5)
       REBASELINE_RUNS=10 python scripts/rebaseline_accuracy.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Dict, List

sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import eval_common as crm  # noqa: E402
import seed_sweep as ss  # noqa: E402
from app.services import retrieval_service  # noqa: E402

RUNS = int(os.environ.get("REBASELINE_RUNS", "5"))


async def main() -> None:
    group_id, lang = crm.DEFAULT_GROUP_ID, crm.DEFAULT_LANGUAGE
    retrieval_service._recent_turns = crm._fake_recent_turns  # type: ignore[assignment]

    scored = [(label, q, h) for label, q, h in crm.QUESTION_SET if label in ss.REFERENCES]
    print(f"Re-baselining v2 over {RUNS} runs on {len(scored)} scored questions")
    print("(mean, not a single figure â€” this call is non-deterministic on marginal units)")
    print("=" * 96)

    per_q: Dict[str, List[float]] = {label: [] for label, _, _ in scored}
    answers: Dict[str, Counter] = {label: Counter() for label, _, _ in scored}
    run_means: List[float] = []

    for run in range(1, RUNS + 1):
        run_scores: List[float] = []
        for label, question, history in scored:
            session_id = f"rebase-{uuid.uuid4()}"
            if history:
                crm._INJECTED_HISTORY[session_id] = history
            clips, _, _, _ = await crm._run_one(
                crm._v2_ranges, question, group_id, lang, session_id
            )
            score = ss._score(clips, ss.REFERENCES[label])
            per_q[label].append(score)
            run_scores.append(score)
            answers[label][ss._fmt(clips)] += 1
        run_means.append(statistics.mean(run_scores))
        print(f"  run {run}/{RUNS}: mean={run_means[-1]:.3f}")

    print()
    print(f"{'question':24s} {'mean':>6s} {'stdev':>6s} {'min':>5s} {'max':>5s}  distinct answers")
    print("-" * 96)
    for label, _, _ in scored:
        vals = per_q[label]
        sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
        print(
            f"{label:24s} {statistics.mean(vals):6.3f} {sd:6.3f} "
            f"{min(vals):5.2f} {max(vals):5.2f}  {len(answers[label])}"
        )

    overall_sd = statistics.stdev(run_means) if len(run_means) > 1 else 0.0
    print("-" * 96)
    print(f"v2 accuracy over {RUNS} runs: mean={statistics.mean(run_means):.3f} "
          f"stdev={overall_sd:.3f} min={min(run_means):.3f} max={max(run_means):.3f}")
    unstable = [l for l in answers if len(answers[l]) > 1]
    print(f"questions returning >1 distinct answer: {unstable or 'none'}")


if __name__ == "__main__":
    asyncio.run(main())
