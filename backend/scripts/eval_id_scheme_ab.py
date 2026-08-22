"""A/B measurement for the scoped unit-id scheme (UNIT_ID_STABILITY_PLAN §4).

Runs the full prompt_regression panel with UNIT_ID_SCHEME=scoped and
compares the selections against the saved GLOBAL-scheme baseline at the
KEY level (segment_id:start_sec) — the scheme-independent ground truth.
Also counts malformed model-output ids (the copy-reliability instrument):
anything persistently above zero blocks the flip.

Fixture shown-states are stored as global ids; they are translated
global -> key -> scoped before each run, and measured selections are
translated back scoped -> key -> global for the baseline diff.

    python scripts/eval_id_scheme_ab.py --runs 5 [--labels a,b,c]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Dict, List

os.environ.setdefault("DEBUG", "false")
sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import prompt_regression as prm  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import full_archive_retrieval as ar  # noqa: E402


async def _id_maps(group_id: str):
    """key<->id maps under BOTH schemes, from the same archive."""
    maps = {}
    for scheme in ("global", "scoped"):
        settings.UNIT_ID_SCHEME = scheme
        ar.invalidate_archive_cache(group_id)
        archive = await ar._load_archive(group_id)
        units = ar._build_units(archive)
        maps[scheme] = {
            "id_to_key": {u.unit_id: ar._unit_key(u.segment_id, u.start_sec) for u in units},
            "key_to_id": {ar._unit_key(u.segment_id, u.start_sec): u.unit_id for u in units},
        }
    return maps


def _translate(ids: List[str], src: dict, dst: dict) -> List[str]:
    out = []
    for i in ids:
        key = src["id_to_key"].get(i)
        if key is None:
            raise RuntimeError(f"untranslatable id {i!r} — key not found")
        out.append(dst["key_to_id"][key])
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--labels", default=None)
    ap.add_argument("--group-id", default=None)
    args = ap.parse_args()
    group_id = args.group_id or __import__("eval_common").DEFAULT_GROUP_ID

    baseline = json.loads(prm.BASELINE.read_text(encoding="utf-8"))
    print(f"baseline: archive {baseline['archive_version']}")
    version = await ar._archive_version(group_id)
    if list(version) != baseline["archive_version"]:
        print("ABORT — archive changed since the baseline; re-save first.")
        return 2

    maps = await _id_maps(group_id)
    g, s = maps["global"], maps["scoped"]

    # Malformed-id capture: wrap the one LLM read.
    malformed_total = {"n": 0, "reads": 0}
    real_read = ar._read_archive_for_ranges

    async def counting_read(*a, **kw):
        read = await real_read(*a, **kw)
        malformed_total["n"] += read.malformed_ids
        malformed_total["reads"] += 1
        return read

    ar._read_archive_for_ranges = counting_read
    prm._install_hard_failing_llm()

    wanted = {x.strip() for x in args.labels.split(",")} if args.labels else None
    cases = [c for c in prm.panel() if (wanted is None or c[0] in wanted)]
    print(f"{len(cases)} cases x {args.runs} runs under UNIT_ID_SCHEME=scoped")
    print("=" * 74)

    settings.UNIT_ID_SCHEME = "scoped"
    ar.invalidate_archive_cache(group_id)

    failures: List[str] = []
    for label, question, history, shown_builder in cases:
        # Fixture states carry GLOBAL ids and their guards look ids up in
        # the CURRENT scheme's unit map — so builders must run under
        # global, with the scheme flipped back for the measured run.
        if shown_builder:
            settings.UNIT_ID_SCHEME = "global"
            ar.invalidate_archive_cache(group_id)
            shown_global = await shown_builder(group_id)
            settings.UNIT_ID_SCHEME = "scoped"
            ar.invalidate_archive_cache(group_id)
        else:
            shown_global = None
        shown_scoped = (
            [_translate(turn, g, s) for turn in shown_global]
            if shown_global is not None
            else None
        )
        rows = []
        for _ in range(args.runs):
            r = await prm._run_once(question, group_id, history, shown_scoped)
            rows.append(r)
        # translate back for the baseline diff
        variants = sorted(
            {tuple(_translate(r["units"], s, g)) for r in rows}
        )
        fu_offered = sum(1 for r in rows if r["follow_up"])
        b = baseline["questions"].get(label)
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
                print(f"      only BASELINE: {only_b}")
            if only_a:
                print(f"      only SCOPED  : {only_a}")

    print("=" * 74)
    for scheme in ("global", "scoped"):
        settings.UNIT_ID_SCHEME = scheme
        ar.invalidate_archive_cache(group_id)
        archive = await ar._load_archive(group_id)
        units = ar._build_units(archive)
        shown_keys: set = set()
        block = ar._format_annotated_transcript(archive, units, shown_keys, {})
        print(f"transcript block ({scheme}): {len(block)} chars")
    rate = malformed_total["n"] / max(1, malformed_total["reads"])
    print(f"malformed ids: {malformed_total['n']} across {malformed_total['reads']} reads "
          f"({rate:.3f}/read)")
    if malformed_total["n"] > 0:
        failures.append(f"malformed ids present ({malformed_total['n']})")
    if failures:
        print(f"A/B FAIL: {failures}")
        return 1
    print("A/B PASS — key-level selections match the global baseline; zero malformed ids.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
