"""The producer's core-vs-offer annotations (docs/CORE_OFFER_WORKSHEET.md,
filled 2026-08-21, archive v18) — the conservation contract the core-vs-offer
prompt edit is accepted against.

Semantics per annotated case:
  * selected units must equal CORE exactly, every run/variant.
  * where OFFER is non-empty, a follow_up must be offered on every run and
    every offered id must fall inside OFFER (or SOFT — units the producer
    never saw on the worksheet, tolerated with a warning, never required).
  * where OFFER is empty (childhood: "no split is warranted"), the
    selection contract still binds; offers are unconstrained.

⚠️ Bound to archive v18 like every unit-id artifact — void on any new
recording.
"""

from typing import Dict, List, Set


def _u(*ranges) -> Set[str]:
    out: Set[str] = set()
    for r in ranges:
        if isinstance(r, tuple):
            out |= {f"u{i}" for i in range(r[0], r[1] + 1)}
        else:
            out.add(f"u{r}")
    return out


ANNOTATIONS: Dict[str, dict] = {
    "family": {
        # father, mother, how-parents-met, the sibling names. The producer's
        # core/offer boundary at u86/u87 splits the contiguous nickname
        # stretch — accepted deliberately: the CURRENT engine already stops
        # at exactly u86, and the cut sits on a real speech pause.
        "core": _u((4, 10), (36, 38), (45, 49), (84, 86)),
        # roots (a whole recording) + the extended-family enumeration.
        "offer": _u((50, 59), (87, 97)),
        # nickname take 2 — absent from the worksheet (generator bug:
        # recordings with no selected units were skipped), so unannotated.
        "soft": _u((98, 103)),
    },
    "career-broad": {
        "core": _u((23, 29)),
        "offer": _u((30, 35)),  # the cooking digression, currently invisible
        "soft": set(),
    },
    "childhood-broad": {
        # "All units genuinely answer... no split is warranted here."
        "core": _u(1, (2, 3), (60, 67), (68, 72), (104, 108)),
        "offer": set(),
        "soft": set(),
    },
}


def check(results: Dict[str, dict]) -> List[str]:
    """Evaluate a measure() result against the contract. Returns failure
    strings (empty = PASS). Prints a per-case verdict."""
    failures: List[str] = []
    for label, spec in ANNOTATIONS.items():
        row = results.get(label)
        if row is None:
            failures.append(f"{label}: not measured")
            continue
        core, offer, soft = spec["core"], spec["offer"], spec["soft"]
        ok = True
        for variant in row["variants"]:
            got = set(variant)
            if got != core:
                ok = False
                failures.append(
                    f"{label}: selection != core "
                    f"(missing {sorted(core - got)}, extra {sorted(got - core)})"
                )
        offered_variants = [set(v) for v in row.get("follow_up_unit_variants", [])]
        if offer:
            if row.get("follow_up_offered", 0) != row["runs"]:
                ok = False
                failures.append(
                    f"{label}: offer expected on every run, got "
                    f"{row.get('follow_up_offered')}/{row['runs']}"
                )
            for ov in offered_variants:
                if not ov:
                    continue
                stray = ov - offer - soft
                if stray:
                    ok = False
                    failures.append(f"{label}: offered ids outside annotation: {sorted(stray)}")
                if ov & soft:
                    print(f"  {label}: NOTE offered ids include unannotated (soft) units: {sorted(ov & soft)}")
        print(f"  {label:16} conservation {'PASS' if ok else 'FAIL'}")
    return failures
