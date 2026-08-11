"""
Before/after for the same-name clarification feature (docs/ENTITY_DISAMBIGUATION.md Â§6).

Two arms over the SAME live archive, differing in one thing only:

  OFF â€” `_build_name_tags_for` forced to {}, which is exactly the pre-feature
        state: no tags in the transcript, no disambiguation instruction in the
        prompt, `clarify` ignored even if the model invents it. Byte-identical
        to the prompt this archive got before the feature existed (asserted
        separately in tests).
  ON  â€” the real thing.

THE GATE COMES FIRST, and it is about NOT clarifying. A model taught to ask
"which one?" that starts asking when the answer was obvious is worse than the
conflation being fixed: the conflation affects questions about one name,
over-asking affects every question. So:

    Clarification rate on the existing unambiguous questions must be 0.

A note on "19". ENTITY_DISAMBIGUATION.md Â§6.3 says 19 existing questions (7
scored + 12 comparison). Those are not disjoint â€” the 7 scored are a SUBSET of
the 12 in eval_common.QUESTION_SET, so the real gate is 12
DISTINCT questions. Reported honestly as 12 rather than inflated to 19.

The gate is necessary and not sufficient. Tags change the transcript of 3 of
14 recordings, so this also diffs the SELECTED UNITS per question between the
arms â€” an answer about the army silently shifting because a tag was added
three lines away is the quieter version of the same failure.

Every arm runs REPEAT times (CLAUDE.md: the archive-read call is
non-deterministic on marginal units, so n=1 measures nothing). Known-unstable
questions are marked in the output rather than silently averaged: `family` and
`army-broad` vary Â±1-2 peripheral units run to run, independently of anything
here.

Usage: python scripts/eval_name_disambiguation.py [group_id]
       EVAL_REPEAT=3  (default)
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Before app.* is imported: DEBUG drives SQLAlchemy's echo, and 100 questions
# of statement logging buries the measurement it is meant to show.
os.environ.setdefault("DEBUG", "false")

sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import eval_common as crm  # noqa: E402
from app.services import full_archive_retrieval as ar  # noqa: E402
from app.services import retrieval_service  # noqa: E402

REPEAT = int(os.environ.get("EVAL_REPEAT", "3"))

#: The ambiguous-name cases. Every one of them is about the SAME two people:
#: ××ž× ×•×Ÿ the army friend and ××ž× ×•×Ÿ × ×—×•× the uncle. Three of the five are about
#: NOT clarifying, which is the ratio the risk deserves.
#:
#: (label, question, history, expectation)
#:   "clarify"  -> must return a clarification and no units
#:   "friend"   -> must answer from the friend's recordings ONLY
#:   "uncle"    -> must answer from the uncle's recording ONLY
NEW_CASES: List[Tuple[str, str, List[dict], str]] = [
    ("ambiguous", "×¡×¤×¨ ×œ×™ ×¢×œ ××ž× ×•×Ÿ", [], "clarify"),
    ("specific-by-name", "×¡×¤×¨ ×œ×™ ×¢×œ ××ž× ×•×Ÿ × ×—×•×", [], "uncle"),
    ("specific-by-role", "×¡×¤×¨ ×œ×™ ×¢×œ ×”×“×•×“ ×©×œ×š ××ž× ×•×Ÿ", [], "uncle"),
    # NOT "what did ××ž× ×•×Ÿ DO in the army" â€” measured, and the archive does not
    # answer it: the recording says the speaker was in the air force, served
    # three years, and has a friend ××ž× ×•×Ÿ from there. Nothing says what ××ž× ×•×Ÿ
    # did. A case whose expected answer is not in the archive measures the
    # model's willingness to over-reach, not its disambiguation.
    ("resolved-by-context", "×¡×¤×¨ ×œ×™ ×¢×œ ××ž× ×•×Ÿ ×ž×”×¦×‘×", [], "friend"),
    (
        "resolved-by-history",
        "×•×ž×” ×¢×•×“?",
        [
            {"role": "user", "content": "×¡×¤×¨ ×œ×™ ×¢×œ ××ž× ×•×Ÿ ×”×—×‘×¨ ×©×œ×š ×ž×”×¦×‘×"},
            {"role": "assistant", "content": "http://localhost:8000/uploads/x.mp4"},
        ],
        "friend",
    ),
]

#: Run-to-run variance; see CLAUDE.md, which names exactly these three as the
#: questions flash varies on ("flash varied on army-narrow and family",
#: plus army-broad from the comparison harness). Marked so a difference
#: between arms is not read as an effect of the change without more runs.
KNOWN_UNSTABLE = {"family", "army-broad", "army-narrow"}


async def _which_recordings(group_id: str) -> Dict[str, str]:
    """segment id -> 'friend' | 'uncle', from the entity mentions themselves.

    Derived from the archive rather than hardcoded, so "used only the friend's
    recordings" is ASSERTED against the same data the tags are built from
    instead of inferred from unit ids that move whenever anything is
    re-recorded.
    """
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models import Entity
    from app.services import entity_store

    async with AsyncSessionLocal() as db:
        groups = await entity_store.confusable_entities(db, group_id)
    out: Dict[str, str] = {}
    for group in groups:
        for member in group:
            # The uncle is the one carrying the distinguishing name.
            role = "uncle" if len(member.name.split()) > 1 else "friend"
            for segment_id in member.segment_ids:
                out[segment_id] = role
    return out


async def _run_once(
    question: str, group_id: str, history: List[dict]
) -> Tuple[List[str], Optional[dict], List[str]]:
    """(unit ids, clarify, segment ids touched) for one question, one run."""
    session_id = str(uuid.uuid4())

    async def fake_turns(_session_id, _n):
        return history

    original = retrieval_service._recent_turns
    retrieval_service._recent_turns = fake_turns
    try:
        selection = await ar.select_units(question, group_id, "he", session_id)
    finally:
        retrieval_service._recent_turns = original

    return (
        [u.unit_id for u in selection.selected_units],
        selection.clarify,
        sorted({u.segment_id for u in selection.selected_units}),
    )


async def _arm(
    label: str, group_id: str, cases: List[Tuple[str, str, List[dict]]], tags_on: bool
) -> Dict[str, List[Tuple[List[str], Optional[dict], List[str]]]]:
    real = ar._build_name_tags_for

    async def no_tags(_group_id):
        return {}

    if not tags_on:
        ar._build_name_tags_for = no_tags
    ar.invalidate_archive_cache(group_id)
    try:
        results: Dict[str, List[Tuple[List[str], Optional[dict], List[str]]]] = {}
        for case_label, question, history in cases:
            runs = []
            for _ in range(REPEAT):
                runs.append(await _run_once(question, group_id, history))
            results[case_label] = runs
            clarified = sum(1 for _, c, _ in runs if c)
            print(
                f"  [{label}] {case_label:22} "
                f"clarify {clarified}/{REPEAT}  "
                f"units {[len(u) for u, _, _ in runs]}"
            )
        return results
    finally:
        ar._build_name_tags_for = real
        ar.invalidate_archive_cache(group_id)


def _stable(runs) -> bool:
    return len({tuple(u) for u, _, _ in runs}) == 1


async def main() -> None:
    group_id = sys.argv[1] if len(sys.argv) > 1 else crm.DEFAULT_GROUP_ID
    roles = await _which_recordings(group_id)
    print(f"Archive {group_id}")
    print(f"Confusable recordings: {[(s[:8], r) for s, r in roles.items()]}")
    if not roles:
        print("\nNo two people share a name in this archive â€” nothing to measure.")
        return

    unambiguous = [(label, q, h) for label, q, h in crm.QUESTION_SET]
    print(f"\n{len(unambiguous)} existing unambiguous questions x {REPEAT} runs x 2 arms")
    print("=" * 78)
    off_gate = await _arm("OFF", group_id, unambiguous, tags_on=False)
    on_gate = await _arm("ON ", group_id, unambiguous, tags_on=True)

    print(f"\n{len(NEW_CASES)} same-name cases x {REPEAT} runs x 2 arms")
    print("=" * 78)
    new_cases = [(label, q, h) for label, q, h, _ in NEW_CASES]
    off_new = await _arm("OFF", group_id, new_cases, tags_on=False)
    on_new = await _arm("ON ", group_id, new_cases, tags_on=True)

    # â”€â”€ THE GATE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print("\n" + "=" * 78)
    print("GATE â€” clarification rate on unambiguous questions (must be 0)")
    print("=" * 78)
    total = failures = 0
    for label, runs in on_gate.items():
        clarified = sum(1 for _, c, _ in runs if c)
        total += clarified
        if clarified:
            failures += 1
            print(f"  FAIL {label}: clarified on {clarified}/{REPEAT} runs")
            for _, c, _ in runs:
                if c:
                    print(f"        {c['question']}")
    print(
        f"  {total} clarifications across {len(on_gate)} questions x {REPEAT} runs "
        f"-> {'PASS' if total == 0 else 'FAIL'}"
    )

    # â”€â”€ Did tagging perturb answers it has nothing to do with? â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print("\n" + "=" * 78)
    print("SELECTION DRIFT on unambiguous questions (OFF vs ON)")
    print("=" * 78)
    for label in off_gate:
        off_sets = {tuple(u) for u, _, _ in off_gate[label]}
        on_sets = {tuple(u) for u, _, _ in on_gate[label]}
        note = "  (known-unstable)" if label in KNOWN_UNSTABLE else ""
        if off_sets == on_sets:
            mark = "same" if len(off_sets) == 1 else "same set of variants"
            print(f"  {label:24} {mark}{note}")
        else:
            only_off = sorted(set().union(*off_sets) - set().union(*on_sets))
            only_on = sorted(set().union(*on_sets) - set().union(*off_sets))
            print(
                f"  {label:24} DIFFERS{note}\n"
                f"      only OFF: {only_off}\n"
                f"      only ON : {only_on}"
            )

    # â”€â”€ The cases the feature exists for â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print("\n" + "=" * 78)
    print("SAME-NAME CASES")
    print("=" * 78)
    passed = 0
    for label, question, _history, expected in NEW_CASES:
        off_runs, on_runs = off_new[label], on_new[label]

        def verdict(runs) -> str:
            clarified = sum(1 for _, c, _ in runs if c)
            segs = [
                {roles.get(s, "other") for s in touched} for _, _, touched in runs
            ]
            if expected == "clarify":
                return f"clarify {clarified}/{REPEAT}"
            wrong = sum(
                1 for s in segs if not s or s - {expected} or expected not in s
            )
            return f"clarify {clarified}/{REPEAT}, correct-person {REPEAT - wrong}/{REPEAT}"

        ok = (
            all(c for _, c, _ in on_runs)
            if expected == "clarify"
            else all(
                not c
                and {roles.get(s, "other") for s in touched} == {expected}
                for _, c, touched in on_runs
            )
        )
        passed += ok
        print(f"  {label:22} expect {expected}")
        print(f"      OFF: {verdict(off_runs)}")
        print(f"      ON : {verdict(on_runs)}   -> {'PASS' if ok else 'FAIL'}")

    print("\n" + "=" * 78)
    print(
        f"GATE {'PASS' if total == 0 else 'FAIL'}  |  "
        f"same-name cases {passed}/{len(NEW_CASES)}"
    )
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
