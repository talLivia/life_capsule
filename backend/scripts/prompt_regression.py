"""
Did a prompt edit change an answer it had nothing to do with?

THE CHECK THAT CATCHES WHAT THE FEATURE'S OWN GATE CANNOT. A feature-specific
gate asks "does this behave correctly on its own terms". Every one of the four
regressions found while building the same-name disambiguation passed that kind
of gate and broke something unrelated anyway:

  clarify JSON moved to the end of Rules   -> `school` 8 units -> 0
  example "my friend from the army"        -> `army-narrow` 2 units -> 4
  example swapped to "my neighbour"        -> `school` 8 units -> 0
  tag on RECORDING line instead of inline  -> `school` 8 units -> 0

None of those questions involves a name, a tag, or a clarification. The
mechanism is that every instruction is in context for every question: the
instruction text is English, the transcript is Hebrew, and concrete nouns in
the instructions act as soft retrieval cues against it. Examples leak harder
than rules, because examples are where the domain nouns live. Position matters
too â€” the same clarify rule primed empty answers purely by sitting last.

So: run BEFORE the edit, run AFTER, diff the selected units. That is the whole
idea, and it is worth more than any amount of reading the prompt carefully.

## Usage

    python scripts/prompt_regression.py --save     # BEFORE you edit the prompt
    ...edit the prompt...
    python scripts/prompt_regression.py            # AFTER â€” diffs against it

Exit code is non-zero when anything drifted, so it can gate a commit.

## Three things this harness does that a naive one would not

**It hard-fails on a failed archive read.** This checks
`UnitSelection.read_failed`, NOT just an exception â€” and the difference is the
whole lesson. The earlier version only raised from a retry wrapper, which
`_read_archive_for_ranges` swallowed, so the safety net was decorative: an
outage still arrived here as "this question now returns nothing".

`_read_archive_for_ranges` stays fail-soft â€” a live turn should get a sentence,
not a stack trace â€” but it now REPORTS the failure instead of returning an
empty selection that looks identical to a real one. Both of the broken
measurements taken while building the same-name feature were outages that read
as clean results, and so, most likely, was one live bug report. PROJECT_STATUS
has carried this warning about the accuracy eval since 2026-07-29; here it is
enforced rather than warned about.

**It refuses to compare across a changed archive.** The baseline records the
archive fingerprint. Unit ids are positional across the whole archive, so
re-recording one segment renumbers everything after it and every stored id
becomes a lie. `seed_sweep.py`'s references died exactly this way â€” they name
segment uuids that no longer exist, and the questions they score are silently
unscoreable.

**It marks the questions already known to vary** rather than averaging them
away. `family`, `army-broad` and `army-narrow` move by a unit or two run to
run for reasons that predate any edit (CLAUDE.md: the archive-read call is
non-deterministic on marginal units). Drift on those needs more runs before it
means anything; drift anywhere else is a finding.

## The panel

Every question from the comparison harness, plus MARGINAL ones added by hand.
Marginal questions are where leakage lands â€” not at random. `school` asks what
he STUDIED while the units say WHERE he studied; that stretch is a close call
the model can go either way on, so any perturbation flips it. The set of
leakage-vulnerable questions is approximately the set of already-unstable
ones, which is what makes this panel worth curating rather than growing at
random. ADD A QUESTION HERE whenever one is found to flip on an unrelated
edit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Before app.* is imported: DEBUG drives SQLAlchemy's echo, and a hundred
# questions of statement logging buries the report.
os.environ.setdefault("DEBUG", "false")

sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import eval_common as crm  # noqa: E402
from app.services import full_archive_retrieval as ar  # noqa: E402
from app.services import retrieval_service  # noqa: E402
from app.services.llm import llm_service  # noqa: E402

BASELINE = Path(__file__).resolve().parent / "prompt_regression_baseline.json"

#: Questions found to flip on an edit that had nothing to do with them. Not a
#: guess â€” each one earned its place by actually moving. Keep the reason.
MARGINAL: Dict[str, str] = {
    # Asks what he STUDIED; the units say WHERE he studied. Flipped 8 -> 0 on
    # three separate unrelated edits.
    "school": "answered by a stretch â€” 'what did you study' vs units naming schools",
    # Narrow role question sitting next to units about army friends. Broadened
    # 2 -> 4 when an unrelated instruction mentioned "my friend from the army".
    "army-narrow": "narrow question adjacent to broader material in the same recording",
    # Both documented in CLAUDE.md as varying run to run on peripheral units.
    "family": "broad â€” CLAUDE.md documents +/-1-2 peripheral units",
    "army-broad": "broad â€” CLAUDE.md documents +/-1-2 peripheral units",
    # Broad question about a person; how much context travels with the
    # name-bearing units is exactly the judgement under test here.
    "about-a-person": "broad question about a person named in only one unit per recording",
    # Added 2026-08-20 with the gap recordings: new broad domains start life
    # marginal until the 5-run baseline proves otherwise.
    "career-broad": "new broad domain (career recording, 2026-08-20) â€” stability unproven",
    "childhood-broad": "broad across 4+ childhood recordings â€” stability unproven",
    "two-hop-roots": "new state-bearing case (accepted follow-up offer) â€” stability unproven",
}

#: The archive fingerprint the `uncle-then-more` fixture was derived from.
#: Unit ids are positional across the whole archive, so these lists are void
#: the moment anything is re-recorded â€” the builder REFUSES to run rather
#: than silently testing different words (how seed_sweep.py's references
#: died, twice).
#: Re-derived 2026-08-20 after the gap-filling recordings (career_q02 +
#: relationships_q03) landed. ⚠️ Archive order is (question_index,
#: created_at) — NOT append-only by date: both new recordings inserted
#: MID-ARCHIVE (career at u23-35, spouse at u39-44), shifting every unit
#: from the old u23 onward by +13, then +19 from the old u26 onward. The
#: fixture lists below were mapped mechanically by that rule and are
#: re-verified semantically by _verified_uncle_state's guards on every run.
_UNCLE_STATE_ARCHIVE_VERSION = (
    18,
    "2026-08-20 16:08:34.227713+00:00",
    "2026-08-20 16:07:07.180180+00:00",
)

#: The live session's per-assistant-turn unit lists (2026-08-09, session
#: 90992fb3), oldest first, up to and including the answer to
#: "×ž×™ ×”×“×•×“×™× ×©×œ×š?". VERBATIM, not abstracted: the reproduction is
#: state-sensitive enough that plausible simplifications of this list â€” the
#: uncle's whole segment as one turn, or "everything except the friend's
#: recordings" â€” measured 0/2 where this exact state measured 5/5 and 2/2.
_UNCLE_STATE_TURNS: List[List[str]] = [
    [f"u{i}" for i in range(4, 11)] + ["u36", "u37", "u38", "u84", "u85", "u86"],
    [f"u{i}" for i in range(45, 60)]
    + [f"u{i}" for i in range(68, 79)]
    + [f"u{i}" for i in range(87, 98)],
    [f"u{i}" for i in range(73, 79)] + ["u82", "u83"],
    ["u2", "u3"],
    [],  # a persisted no-story reply: an assistant row with no units
    ["u93", "u94", "u95", "u96", "u97"],
]

#: Session 70305082 (2026-08-09 23:26-23:28 UTC), oldest first: friend fully
#: played (turns 1-2), three unrelated family turns, a clarify (persisted
#: assistant row, no units), then the uncle resolved and fully played. The
#: distinguishing feature vs _UNCLE_STATE_TURNS: BOTH same-named people are
#: exhausted when the "×¢×•×“" question lands.
_UNCLE_EXHAUSTED_TURNS: List[List[str]] = [
    [f"u{i}" for i in range(11, 15)],
    [f"u{i}" for i in range(15, 23)],
    [f"u{i}" for i in range(4, 11)],
    ["u36", "u37", "u38"],
    [f"u{i}" for i in range(45, 50)],
    # ⚠️ Fixed 2026-08-20: this list originally ended with only the uncle's
    # 5-unit enumeration (old u74-78), which NEVER satisfied the guard's
    # "everything about the uncle is shown" — the case was added while
    # Gemini credits were exhausted and had never actually run, so the
    # defect sat undetected until the first real --save. The session note
    # says "the uncle resolved and FULLY played"; the full recording is
    # what that means.
    [],  # the clarify reply "×œ××™×–×” ××ž× ×•×Ÿ ×”×ª×›×•×•× ×ª?"
    [f"u{i}" for i in range(84, 98)],
]


async def _verified_uncle_state(
    case: str, turns: List[List[str]], group_id: str, friend_exhausted: bool
) -> List[List[str]]:
    """Shared guards for the state-bearing fixtures, so they go stale LOUDLY.

    Checks the archive fingerprint, that the last turn's units all belong to
    the uncle's recording, and that everything about him is shown â€” the facts
    the reproductions actually rest on. `friend_exhausted` additionally
    asserts every unit of the friend's recordings is shown, which is what
    separates the two cases."""
    from app.database import AsyncSessionLocal
    from app.services import entity_store

    version = await ar._archive_version(group_id)
    if version != _UNCLE_STATE_ARCHIVE_VERSION:
        raise RuntimeError(
            f"the {case} fixture was derived from archive "
            f"{_UNCLE_STATE_ARCHIVE_VERSION} but the archive is now {version}. "
            "Unit ids are positional, so its lists now point at different "
            "words. Re-derive them from a real session (see the fixture "
            "comment) rather than deleting the case."
        )

    async with AsyncSessionLocal() as db:
        groups = await entity_store.confusable_entities(db, group_id)
    uncle_segments = {
        seg
        for group in groups
        for member in group
        if len(member.name.split()) > 1
        for seg in member.segment_ids
    }
    friend_segments = {
        seg
        for group in groups
        for member in group
        if len(member.name.split()) == 1
        for seg in member.segment_ids
    }
    _archive, _em, units, _tags = await ar._archive_bundle(group_id)
    by_id = {u.unit_id: u for u in units}
    last_turn = turns[-1]
    uncle_unit_ids = {u.unit_id for u in units if u.segment_id in uncle_segments}
    shown = {uid for turn in turns for uid in turn}
    if not all(by_id[uid].segment_id in uncle_segments for uid in last_turn):
        raise RuntimeError(f"{case}: the last turn no longer plays the uncle's recording")
    if not uncle_unit_ids <= shown:
        raise RuntimeError(f"{case}: not every unit about the uncle is marked shown")
    if friend_exhausted:
        friend_unit_ids = {u.unit_id for u in units if u.segment_id in friend_segments}
        if not friend_unit_ids or not friend_unit_ids <= shown:
            raise RuntimeError(
                f"{case}: the friend's units are not all marked shown, but the "
                "case exists to probe exactly the both-people-exhausted state"
            )
    return turns


async def _uncle_conversation_state(group_id: str) -> List[List[str]]:
    return await _verified_uncle_state(
        "uncle-then-more", _UNCLE_STATE_TURNS, group_id, friend_exhausted=False
    )


#: The family answer's exact selection at archive v18 (father + mother +
#: both nickname takes), verified live 2x on 08-17/08-19 in the OLD
#: numbering and remapped 2026-08-20. The two-hop case replays: family
#: answered -> the listener accepts the roots OFFER (the literal offered
#: question observed live on 08-17) -> the roots recording should answer.
_FAMILY_ANSWER_TURN: List[str] = (
    [f"u{i}" for i in range(4, 11)]
    + ["u36", "u37", "u38"]
    + [f"u{i}" for i in range(84, 104)]
)


async def _family_then_roots_state(group_id: str) -> List[List[str]]:
    version = await ar._archive_version(group_id)
    if version != _UNCLE_STATE_ARCHIVE_VERSION:
        raise RuntimeError(
            f"two-hop-roots fixture derived from {_UNCLE_STATE_ARCHIVE_VERSION} "
            f"but the archive is now {version} - re-derive before trusting it"
        )
    _archive, _em, units, _tags = await ar._archive_bundle(group_id)
    by_id = {u.unit_id: u for u in units}
    missing = [u for u in _FAMILY_ANSWER_TURN if u not in by_id]
    if missing:
        raise RuntimeError(f"two-hop-roots: fixture units missing from archive: {missing}")
    return [_FAMILY_ANSWER_TURN]


async def _uncle_exhausted_state(group_id: str) -> List[List[str]]:
    return await _verified_uncle_state(
        "uncle-then-more-exhausted", _UNCLE_EXHAUSTED_TURNS, group_id, friend_exhausted=True
    )


#: Extra questions not in the comparison set, added because they probe a
#: marginal judgement. (label, question, history, shown_turns)
#:
#: `shown_turns` is None, or an async builder (group_id) -> per-turn unit-id
#: lists, oldest first â€” the last list is what the previous assistant turn
#: played. It exists because the comparison set runs every question against an
#: EMPTY session, and one live regression was invisible in exactly that state:
#: the answer only went wrong once the subject's units were ALREADY SHOWN.
EXTRA: List[Tuple[str, str, List[dict], Optional[object]]] = [
    # "Tell me about a PERSON", where that person is named in only one unit of
    # each recording that covers them. Selecting just those two units and
    # dropping the story around them is the failure this watches for; the
    # question is here because it was reported live and reproduced 4/4.
    (
        "about-a-person",
        "×¡×¤×¨ ×œ×™  ×¢×œ ××ž× ×•×Ÿ â€” ××ž× ×•×Ÿ, ×—×‘×¨ ×©×œ×™ ×ž×”×¦×‘× ×•×ž×”×œ×™×ž×•×“×™×",
        [{"role": "assistant", "content": "×œ××™×–×” ××ž× ×•×Ÿ ××ª×” ×ž×ª×›×•×•×Ÿ?"},
         {"role": "user", "content": "×¡×¤×¨ ×œ×™  ×¢×œ ××ž× ×•×Ÿ â€” ××ž× ×•×Ÿ, ×—×‘×¨ ×©×œ×™ ×ž×”×¦×‘× ×•×ž×”×œ×™×ž×•×“×™×"}],
        None,
    ),
    # THE STATE-BEARING CASE. Live 2026-08-09: with the uncle just discussed
    # and all of his units already shown, "×™×© ×¢×•×“ ×¡×™×¤×•×¨ ×¢×œ ××ž× ×•×Ÿ?" answered
    # with the OTHER ××ž× ×•×Ÿ â€” the army friend's passages â€” 5/5 in faithful
    # replay. Correct behaviour is an empty selection (everything about the
    # uncle has played; the friend is a different person), which the no-story
    # path then names. The edit that caused it (the backward-passage bullet,
    # 323f88d) passed this panel clean, because every other case runs with an
    # empty session: the bug needs ALREADY SHOWN marks on the subject's units
    # to exist at all. NOT in MARGINAL â€” it reproduced 5/5 and its correct
    # form held 3/3, so any drift here is a finding, not noise.
    (
        "uncle-then-more",
        "×™×© ×¢×•×“ ×¡×™×¤×•×¨ ×¢×œ ××ž× ×•×Ÿ?",
        [{"role": "assistant",
          "content": "×™×© ×œ×™ ×’× ×“×•×“ ×ž×¦×“ ××‘× ×©×§×•×¨××™× ×œ×• ××ž× ×•×Ÿ ×•×™×© ×œ×• ×©×ª×™ ×™×œ×“×™× ×‘×¨ ×•×“×•×¨"},
         {"role": "user", "content": "×™×© ×¢×•×“ ×¡×™×¤×•×¨ ×¢×œ ××ž× ×•×Ÿ?"}],
        _uncle_conversation_state,
    ),
    # THE NEIGHBOURING STATE, failed live 2026-08-09 23:28 UTC (session
    # 70305082, turn 8) and DELIBERATELY LEFT UNFIXED â€” a documented known
    # gap, see PROJECT_STATUS. Same question, same uncle-just-discussed
    # history, but BOTH same-named people are exhausted. The model switched
    # to the friend and replayed him in full; the correct output is an empty
    # selection with about naming the uncle ("×–×” ×›×œ ×ž×” ×©×™×© ×œ×™ ×¢×œ ××ž× ×•×Ÿ × ×—×•×").
    #
    # âš ï¸ NO BASELINE ENTRY EXISTS YET (added while Gemini credits were
    # exhausted), and the LIVE behaviour on this state is the bug. The first
    # --save will therefore pin CURRENT behaviour, whatever it is: read the
    # recorded variant as "the gap, pinned for drift-detection", never as
    # "correct". When a fix lands, drift on this case flipping to [] is the
    # intended outcome. NOT in MARGINAL for the same reason as its sibling.
    (
        "uncle-then-more-exhausted",
        "×™×© ×¢×•×“ ×¡×™×¤×•×¨ ×¢×œ ××ž× ×•×Ÿ?",
        [{"role": "assistant",
          "content": "×™×© ×œ×™ ×’× ×“×•×“ ×ž×¦×“ ××‘× ×©×§×•×¨××™× ×œ×• ××ž× ×•×Ÿ ×•×™×© ×œ×• ×©×ª×™ ×™×œ×“×™× ×‘×¨ ×•×“×•×¨"},
         {"role": "user", "content": "×™×© ×¢×•×“ ×¡×™×¤×•×¨ ×¢×œ ××ž× ×•×Ÿ?"}],
        _uncle_exhausted_state,
    ),
    # Two-hop follow-up acceptance (added 2026-08-20): after the broad family
    # answer played, the listener says yes to the roots offer - the offered
    # question goes through the normal path (byte-identical string, as the
    # button/voice layer sends it). The roots recording should answer;
    # answering with already-shown family units instead would be the
    # offer-leads-nowhere failure.
    (
        "two-hop-roots",
        "רוצה לשמוע על השורשים של המשפחה שלי מצד אמא ומצד אבא?",
        [
            {"role": "user", "content": "ספר לי על המשפחה שלך"},
            {"role": "assistant", "content": "http://localhost:8000/uploads/video-clips/f.mp4"},
        ],
        _family_then_roots_state,
    ),
]


class ExhaustedAPI(RuntimeError):
    """Raised rather than letting fail-soft turn an outage into a result."""


def _install_hard_failing_llm(retries: int = 6) -> None:
    real = llm_service.generate_response

    async def wrapper(messages, system_prompt=None, thinking=False, temperature=None, **kw):
        last: Optional[Exception] = None
        for attempt in range(retries):
            try:
                return await real(
                    messages=messages, system_prompt=system_prompt,
                    thinking=thinking, temperature=temperature, **kw
                )
            except Exception as e:  # noqa: BLE001 â€” re-raised below
                last = e
                await asyncio.sleep(4 * (attempt + 1))
        raise ExhaustedAPI(f"{retries} attempts failed; last: {last}")

    llm_service.generate_response = wrapper


def panel() -> List[Tuple[str, str, List[dict], Optional[object]]]:
    return [(label, q, h, None) for label, q, h in crm.QUESTION_SET] + EXTRA


async def _run_once(
    question: str,
    group_id: str,
    history: List[dict],
    shown_turns: Optional[List[List[str]]] = None,
) -> dict:
    session_id = str(uuid.uuid4())

    async def fake_turns(_session_id, _n):
        return history

    original = retrieval_service._recent_turns
    retrieval_service._recent_turns = fake_turns
    original_shown = ar._load_shown_units
    if shown_turns is not None:
        # A faithful shown-state, the way production stores it: per-turn unit
        # records carrying REAL texts. An approximate window reproduces
        # nothing â€” that lesson is paid for three times over in
        # PROJECT_STATUS; empty texts here made the live bug vanish.
        _archive, _em, units, _tags = await ar._archive_bundle(group_id)
        by_id = {u.unit_id: u for u in units}
        per_turn = [
            [{"key": ar._unit_key(by_id[uid].segment_id, by_id[uid].start_sec),
              "unit_id": uid, "text": by_id[uid].text}
             for uid in turn if uid in by_id]
            for turn in shown_turns
        ]
        keys = {u["key"] for t in per_turn for u in t}

        async def fake_shown(_session_id):
            return keys, per_turn

        ar._load_shown_units = fake_shown
    try:
        selection = await ar.select_units(question, group_id, "he", session_id)
    finally:
        retrieval_service._recent_turns = original
        ar._load_shown_units = original_shown

    if selection.read_failed:
        raise ExhaustedAPI(
            "the archive read failed; refusing to record it as a selection"
        )
    return {
        "units": [u.unit_id for u in selection.selected_units],
        # Recorded too: a prompt edit can switch clarification on or off for a
        # question, and that is a behaviour change even when the units match.
        "clarify": bool(selection.clarify),
        # And the follow-up OFFER (added 2026-08-20): PRESENCE only â€”
        # _validate_follow_up validates the offered unit ids against the
        # archive and shown-set, then deliberately strips them from its
        # return, so ids are not observable here. "Offers stopped
        # appearing" is the drift this catches; id-level conservation
        # needs the engine to expose them (a decision that belongs to the
        # gated core-vs-offer step, not this harness).
        "follow_up": bool(selection.follow_up),
        # unit_ids became observable on 2026-08-21 (exposed server-side for
        # the conservation metric; stripped at the WS boundary). Compared
        # only when the baseline carries them â€” a presence-only baseline
        # stays valid until its next --save.
        "follow_up_units": sorted((selection.follow_up or {}).get("unit_ids", [])),
    }


async def measure(group_id: str, runs: int) -> dict:
    ar.invalidate_archive_cache(group_id)
    results: Dict[str, dict] = {}
    for label, question, history, shown_builder in panel():
        shown_turns = await shown_builder(group_id) if shown_builder else None
        rows = [
            await _run_once(question, group_id, history, shown_turns)
            for _ in range(runs)
        ]
        variants = sorted({tuple(r["units"]) for r in rows})
        fu_variants = sorted({tuple(r["follow_up_units"]) for r in rows})
        results[label] = {
            "variants": [list(v) for v in variants],
            "clarified": sum(1 for r in rows if r["clarify"]),
            "follow_up_offered": sum(1 for r in rows if r["follow_up"]),
            "follow_up_unit_variants": [list(v) for v in fu_variants],
            "runs": runs,
        }
        stable = "stable" if len(variants) == 1 else f"{len(variants)} variants"
        print(
            f"  {label:24} units {[len(r['units']) for r in rows]}  "
            f"clarify {results[label]['clarified']}/{runs}  {stable}"
        )
    return {
        # Unit ids are positional across the WHOLE archive, so this is what
        # makes a stored baseline meaningful at all.
        "archive_version": list(await ar._archive_version(group_id)),
        "group_id": group_id,
        "runs": runs,
        "questions": results,
    }


def compare(before: dict, after: dict) -> int:
    if before["archive_version"] != after["archive_version"]:
        print("\nBASELINE IS VOID â€” the archive changed since it was saved.")
        print(f"  saved: {before['archive_version']}")
        print(f"  now  : {after['archive_version']}")
        print(
            "\nUnit ids are positional across the whole archive, so every id in\n"
            "the baseline refers to something else now. Re-save it (--save) from\n"
            "a checkout WITHOUT your prompt edit, then re-run."
        )
        return 2

    print("\n" + "=" * 74)
    print("DRIFT")
    print("=" * 74)
    drifted: List[str] = []
    for label in before["questions"]:
        b, a = before["questions"][label], after["questions"].get(label)
        if a is None:
            print(f"  {label:24} MISSING from this run")
            drifted.append(label)
            continue
        b_units = {tuple(v) for v in b["variants"]}
        a_units = {tuple(v) for v in a["variants"]}
        note = f"   ({MARGINAL[label]})" if label in MARGINAL else ""
        b_fu = b.get("follow_up_offered")
        a_fu = a.get("follow_up_offered")
        b_fuv = b.get("follow_up_unit_variants")
        a_fuv = a.get("follow_up_unit_variants")
        fu_same = ("follow_up_offered" not in b) or (
            b_fu == a_fu and (b_fuv is None or b_fuv == a_fuv)
        )
        if b_units == a_units and b["clarified"] == a["clarified"] and fu_same:
            print(f"  {label:24} same")
            continue
        drifted.append(label)
        flag = "DRIFT (known-marginal â€” re-run with more N before acting)" if label in MARGINAL else "DRIFT"
        print(f"  {label:24} {flag}{note}")
        only_before = sorted(set().union(*b_units) - set().union(*a_units)) if b_units else []
        only_after = sorted(set().union(*a_units) - set().union(*b_units)) if a_units else []
        if only_before:
            print(f"      only BEFORE: {only_before}")
        if only_after:
            print(f"      only AFTER : {only_after}")
        if b["clarified"] != a["clarified"]:
            print(f"      clarify {b['clarified']}/{b['runs']} -> {a['clarified']}/{a['runs']}")
        if "follow_up_offered" in b and not fu_same:
            print(f"      follow-up offered {b_fu}/{b['runs']} -> {a_fu}/{a['runs']}")
            if b_fuv is not None:
                print(f"      follow-up units  {b_fuv} -> {a_fuv}")

    print("\n" + "=" * 74)
    if not drifted:
        print("NO DRIFT â€” the edit changed nothing on this panel.")
        return 0
    unexpected = [d for d in drifted if d not in MARGINAL]
    print(f"{len(drifted)} question(s) drifted: {drifted}")
    if unexpected:
        print(
            f"\n{len(unexpected)} of them are NOT known-marginal: {unexpected}\n"
            "That is a finding, not noise. An edit reaching a question it has\n"
            "nothing to do with is the failure this harness exists for."
        )
    print(
        "\nIf a question drifted that is not in MARGINAL, add it there with the\n"
        "reason once you understand it â€” the panel is only useful if it grows\n"
        "from real findings."
    )
    return 1


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", action="store_true",
                        help="record the CURRENT behaviour as the baseline")
    parser.add_argument("--runs", type=int, default=int(os.environ.get("PROMPT_REGRESSION_RUNS", "3")))
    parser.add_argument("--group-id", default=crm.DEFAULT_GROUP_ID)
    parser.add_argument("--check-annotations", action="store_true",
                        help="evaluate the core-vs-offer conservation contract "
                             "(scripts/core_offer_annotations.py) on this run")
    args = parser.parse_args()

    _install_hard_failing_llm()

    print(f"{len(panel())} questions x {args.runs} runs   (archive {args.group_id})")
    print("=" * 74)
    try:
        current = await measure(args.group_id, args.runs)
    except ExhaustedAPI as e:
        print(f"\nABORTED â€” {e}")
        print(
            "Deliberately not reported as a result. The archive read is\n"
            "fail-soft, so an outage would otherwise look like 'this question\n"
            "now returns nothing' â€” which is how two measurements were misread\n"
            "while building the same-name feature."
        )
        return 3

    annotation_failures = []
    if args.check_annotations:
        import core_offer_annotations as coa
        print()
        print("=" * 74)
        print("CORE-VS-OFFER CONSERVATION (producer annotations, 2026-08-21)")
        print("=" * 74)
        annotation_failures = coa.check(current["questions"])
        for f in annotation_failures:
            print(f"  FAIL: {f}")

    if args.save:
        BASELINE.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nBaseline saved to {BASELINE.name} "
              f"(archive version {current['archive_version']}).")
        print("Now make the prompt edit and re-run without --save.")
        return 0

    if not BASELINE.exists():
        print(f"\nNo baseline at {BASELINE.name}. Run with --save BEFORE editing "
              "the prompt, from a checkout without the edit.")
        return 2

    rc = compare(json.loads(BASELINE.read_text(encoding="utf-8")), current)
    if annotation_failures:
        return 3
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
