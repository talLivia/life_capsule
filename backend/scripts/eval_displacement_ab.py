"""Simulated-displacement A/B: does numeric id DISCONTINUITY alone move
judgment? (UNIT_ID_STABILITY_PLAN follow-up, 2026-08-22.)

The third id-stability option keeps today's bare u<int> format and today's
presentation order, appending new recordings at the high-water mark — so
the ONLY prompt-surface change is that one block of ids mid-transcript is
numerically discontinuous (…u84, then u109-u121, then u36…). Last night's
scoped A/B could not isolate this variable (it changed format + headers +
rule text together).

This run isolates it exactly: the career recording's units (u23-u35) are
renumbered to u109-u121 IN PLACE — same content, same position, same
order, same headers, same id format, same rule text; nothing changes but
the numbers on those thirteen lines. Confound-checked: no code parses the
number out of a unit id; all ordering is by the .index field, untouched.

Panel runs against this state; selections are translated back
(u109->u23 …) for the key-equivalent baseline diff.

    python scripts/eval_displacement_ab.py --runs 5 [--labels ...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import List

os.environ.setdefault("DEBUG", "false")
sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import prompt_regression as prm  # noqa: E402
from app.services import full_archive_retrieval as ar  # noqa: E402

DISPLACED_QUESTION_ID = "career_q02"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--labels", default=None)
    args = ap.parse_args()
    group_id = __import__("eval_common").DEFAULT_GROUP_ID

    baseline = json.loads(prm.BASELINE.read_text(encoding="utf-8"))
    version = await ar._archive_version(group_id)
    if list(version) != baseline["archive_version"]:
        print("ABORT — archive changed since the baseline; re-save first.")
        return 2

    # Identify the career segment + build the displacement map.
    ar.invalidate_archive_cache(group_id)
    archive = await ar._load_archive(group_id)
    units = ar._build_units(archive)
    career_seg = next(
        a.segment.id for a in archive if a.segment.question_id == DISPLACED_QUESTION_ID
    )
    high_water = max(u.index for u in units)
    career_units = [u for u in units if u.segment_id == career_seg]
    fwd = {}  # original id -> displaced id
    for offset, u in enumerate(career_units, start=1):
        fwd[u.unit_id] = f"u{high_water + offset}"
    back = {v: k for k, v in fwd.items()}
    print(f"displacing {DISPLACED_QUESTION_ID}: "
          f"{career_units[0].unit_id}-{career_units[-1].unit_id} -> "
          f"{fwd[career_units[0].unit_id]}-{fwd[career_units[-1].unit_id]} "
          f"(position/order unchanged)")

    # Wrap _build_units: substitute ids for the career segment only.
    real_build = ar._build_units

    def displacing_build(archive_arg):
        built = real_build(archive_arg)
        return [
            replace(u, unit_id=fwd.get(u.unit_id, u.unit_id))
            if u.segment_id == career_seg
            else u
            for u in built
        ]

    ar._build_units = displacing_build
    ar.invalidate_archive_cache(group_id)
    prm._install_hard_failing_llm()

    def tr_back(ids: List[str]) -> List[str]:
        return [back.get(i, i) for i in ids]

    wanted = {x.strip() for x in args.labels.split(",")} if args.labels else None
    cases = [c for c in prm.panel() if (wanted is None or c[0] in wanted)]
    print(f"{len(cases)} cases x {args.runs} runs, displaced-career state")
    print("=" * 74)

    failures: List[str] = []
    for label, question, history, shown_builder in cases:
        # Fixtures reference no career units (verified: uncle/family lists
        # span u2-u103 minus u23-35), so shown states pass through as-is —
        # by_id resolves them because only career ids changed.
        shown = await shown_builder(group_id) if shown_builder else None
        rows = [
            await prm._run_once(question, group_id, history, shown)
            for _ in range(args.runs)
        ]
        variants = sorted({tuple(tr_back(r["units"])) for r in rows})
        fu_offered = sum(1 for r in rows if r["follow_up"])
        b = baseline["questions"][label]
        b_variants = {tuple(v) for v in b["variants"]}
        a_variants = set(variants)
        same_units = a_variants == b_variants
        same_fu = fu_offered == b.get("follow_up_offered", 0)
        marginal = label in prm.MARGINAL or label == "uncle-then-more-exhausted"
        verdict = "same" if (same_units and same_fu) else (
            "DRIFT (marginal)" if marginal else "DRIFT"
        )
        if not (same_units and same_fu) and not marginal:
            failures.append(label)
        counts = [len(r["units"]) for r in rows]
        print(f"  {label:24} units {counts}  fu {fu_offered}/{args.runs}  {verdict}")
        if not same_units:
            only_b = sorted(set().union(*b_variants) - set().union(*a_variants)) if b_variants else []
            only_a = sorted(set().union(*a_variants) - set().union(*b_variants)) if a_variants else []
            if only_b:
                print(f"      only BASELINE : {only_b}")
            if only_a:
                print(f"      only DISPLACED: {only_a}")

    print("=" * 74)
    if failures:
        print(f"DISPLACEMENT A/B FAIL: {failures}")
        return 1
    print("DISPLACEMENT A/B PASS — numeric discontinuity alone moved nothing "
          "outside the known-marginal set.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
