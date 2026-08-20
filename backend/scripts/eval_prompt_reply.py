"""Live-label panel for the pending-prompt reply classifier.

`_classify_prompt_reply` (services/pending_prompt.py) decides whether a
reply to a follow-up offer means accept / decline / unrelated. Its unit
tests mock the LLM, so until this panel existed the ACTUAL labels the live
model assigns had no regression protection at all — a prompt tweak, a
model pin change, or provider drift could silently flip "לא בא לי כרגע"
from decline to unrelated and no test would notice.

Fixed panel: real archive offers × utterances with EXPECTED labels,
temperature 0, N runs. Baseline records the observed labels; the compare
run flags any flip. Expected labels are also asserted on --save, so the
baseline can never silently pin wrong behavior — with one deliberate
exception (`kan-mishearing`, see the case comment).

    python scripts/eval_prompt_reply.py --save   # record baseline
    python scripts/eval_prompt_reply.py          # compare against it
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault("DEBUG", "false")
sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.llm import llm_service  # noqa: E402
from app.services.pending_prompt import _classify_prompt_reply  # noqa: E402

BASELINE = Path(__file__).resolve().parent / "eval_prompt_reply_baseline.json"


class ExhaustedAPI(RuntimeError):
    pass


def _install_hard_failing_llm(retries: int = 6) -> None:
    """PRODUCTION fails OPEN (an outage becomes 'unrelated' — the safe
    action). This EVAL must do the opposite: under a 503 burst, fail-open
    labels are indistinguishable from real judgment, and the first compare
    run proved it — two cases 'drifted' during a storm and recovered with
    it. Same lesson prompt_regression.py already enforces: an outage is
    never a result."""
    real = llm_service.generate_response

    async def wrapper(*a, **kw):
        last = None
        for attempt in range(retries):
            try:
                return await real(*a, **kw)
            except Exception as e:  # noqa: BLE001
                last = e
                await asyncio.sleep(4 * (attempt + 1))
        raise ExhaustedAPI(f"{retries} attempts failed; last: {last}")

    llm_service.generate_response = wrapper

# Real offers the engine has actually made (08-17/08-19 live sessions).
OFFER_ROOTS = "רוצה לשמוע על השורשים של המשפחה שלי מצד אמא ומצד אבא?"
OFFER_PARENTS = "רוצה לשמוע איך ההורים שלי הכירו?"

#: (label, offer, utterance, expected). Expected is asserted at --save time
#: so a wrong behavior can't be silently pinned — except where noted.
CASES = [
    # accepts, phrased the way the word-list provably missed
    ("accept-plain", OFFER_ROOTS, "כן, תספר לי", "accept"),
    ("accept-casual", OFFER_ROOTS, "אה בטח, למה לא", "accept"),
    ("accept-imperative", OFFER_PARENTS, "ספר לי על זה", "accept"),
    ("accept-eager", OFFER_PARENTS, "ברור שאני רוצה לשמוע", "accept"),
    # declines that carry no request of their own
    ("decline-soft", OFFER_ROOTS, "לא בא לי כרגע", "decline"),
    ("decline-later", OFFER_ROOTS, "אולי אחר כך, תודה", "decline"),
    ("decline-polite", OFFER_PARENTS, "עזוב, לא עכשיו", "decline"),
    # unrelated: the reply contains its own request/topic, even yes/no-led
    ("no-plus-request", OFFER_ROOTS, "לא, תספר לי על הצבא", "unrelated"),
    ("no-negation", OFFER_ROOTS, "לא סיפרת לי על הבית", "unrelated"),
    ("fresh-question", OFFER_PARENTS, "מה השעה?", "unrelated"),
    ("yes-but-other", OFFER_PARENTS, "כן אבל קודם ספר לי על אמא שלך", "unrelated"),
    ("new-topic", OFFER_ROOTS, "ספר לי על הקריירה שלך", "unrelated"),
    # The live STT mishearing (08-19, session 94b70403): the user said כן,
    # Whisper heard כאן. `unrelated` is the fail-open cost documented
    # honestly — if the model ever starts reading כאן as accept, that's a
    # BEHAVIOR CHANGE worth knowing about, in either direction, so this
    # case has no expected-label assert; the baseline pins whatever is
    # observed and the compare run reports movement.
    ("kan-mishearing", OFFER_ROOTS, "כאן", None),
]


async def measure(runs: int) -> dict:
    results = {}
    for label, offer, utterance, expected in CASES:
        labels = [await _classify_prompt_reply(offer, utterance) for _ in range(runs)]
        counts = dict(Counter(labels))
        results[label] = {"labels": counts, "expected": expected, "runs": runs}
        stable = "stable" if len(counts) == 1 else f"UNSTABLE {counts}"
        got = labels[0] if len(counts) == 1 else None
        verdict = ""
        if expected is not None and got is not None:
            verdict = "ok" if got == expected else f"WRONG (expected {expected})"
        print(f"  {label:20} -> {counts}  {stable}  {verdict}")
    return {"cases": results, "runs": runs}


def check_expectations(current: dict) -> int:
    bad = []
    for label, row in current["cases"].items():
        exp = row["expected"]
        if exp is None:
            continue
        observed = set(row["labels"])
        if observed != {exp}:
            bad.append((label, exp, row["labels"]))
    if bad:
        print("\nEXPECTATION FAILURES (refusing to pin these as a baseline):")
        for label, exp, got in bad:
            print(f"  {label}: expected {exp}, observed {got}")
        return 1
    return 0


def compare(before: dict, after: dict) -> int:
    drifted = []
    for label, b in before["cases"].items():
        a = after["cases"].get(label)
        if a is None or set(b["labels"]) != set(a["labels"]):
            drifted.append(label)
            print(f"  {label:20} DRIFT {b['labels']} -> {(a or {}).get('labels')}")
        else:
            print(f"  {label:20} same {a['labels']}")
    if drifted:
        print(f"\n{len(drifted)} case(s) drifted: {drifted}")
        return 1
    print("\nNO DRIFT.")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()

    _install_hard_failing_llm()
    print(f"{len(CASES)} cases x {args.runs} runs")
    try:
        current = await measure(args.runs)
    except ExhaustedAPI as e:
        print(f"\nABORTED - {e}")
        print("An outage is not a label; re-run when the API recovers.")
        return 3

    if args.save:
        rc = check_expectations(current)
        if rc:
            return rc
        BASELINE.write_text(
            json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\nbaseline saved: {BASELINE.name}")
        return 0

    if not BASELINE.exists():
        print("no baseline — run with --save first")
        return 2
    before = json.loads(BASELINE.read_text(encoding="utf-8"))
    rc = compare(before, current)
    rc2 = check_expectations(current)
    return rc or rc2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
