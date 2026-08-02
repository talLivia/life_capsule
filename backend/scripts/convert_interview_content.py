"""
Convert docs/interview_content_source.json into the final v2 question file.

Step 1 of docs/INTERVIEW_RESTRUCTURE.md. Produces
`app/interview_questions_v2.json`; the live `interview_questions.json` is NOT
touched until the cutover (step 6), so the app keeps working throughout.

WHY A SCRIPT AND NOT A HAND EDIT: 16 categories, 129 questions and nested
gates is past the size where a hand-built file can be trusted, and the
conversion has to be repeatable while the source is still being revised.

IDS ARE ASSIGNED ONCE AND THEN FROZEN. `raw_segments.question_id` (Phase 1b)
is what keeps a recording attached to its life period across a question-set
edit, so an id must never change once a recording references it. Positional
NUMBERING at conversion time is fine — `childhood_q01` — because the number is
written literally into the output and never recomputed at read time. What
would break Phase 1b is DERIVING an id from position on every load.

Re-running is therefore id-preserving: existing ids are matched by verbatim
question text and reused, and only genuinely new questions get new numbers.
Rewording a question breaks that match, so a reworded question is treated as
new and reported — check the report before accepting it.

    python scripts/convert_interview_content.py            # report only
    python scripts/convert_interview_content.py --write    # write the file
"""

import json
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT.parent / "docs" / "interview_content_source.json"
OUTPUT = ROOT / "app" / "interview_questions_v2.json"
LEGACY = ROOT / "app" / "interview_questions.json"

YES, NO = "כן", "לא"

# Wording that is MINE, not the producer's, and must be confirmed before
# cutover. Every one of these is emitted with needs_wording_confirmation=true
# so the validator can refuse to let them ship unnoticed.
ALIYAH_MADE_TEXT = "האם עלית לארץ?"
ALIYAH_BORN_TEXT = "האם נולדת בישראל?"
STATUS_LABELS = {
    "together": "יחד",
    "widowed": "אלמן/ה",
    "separated_divorced": "פרוד/ה או גרוש/ה",
}


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _existing_ids_by_text(path):
    """Reuse ids from a previous run so re-conversion never renumbers."""
    if not path.exists():
        return {}
    out = {}

    def walk(steps):
        for s in steps:
            if s["kind"] == "question":
                out[s["text"].strip()] = s["id"]
            else:
                for opt in s["options"]:
                    walk(opt["steps"])

    doc = _load(path)
    for cat in doc["languages"]["he"]["categories"]:
        walk(cat["steps"])
    return out


class Assigner:
    """Hands out `{category}_q{NN}`, reusing any id a previous run gave the
    same verbatim text."""

    def __init__(self, existing):
        self.existing = existing
        self.counters = {}
        self.reused = 0
        self.fresh = []

    def question(self, category_id, text):
        text = text.strip()
        if text in self.existing:
            self.reused += 1
            return self.existing[text]
        n = self.counters.get(category_id, 0) + 1
        self.counters[category_id] = n
        qid = f"{category_id}_q{n:02d}"
        self.fresh.append((qid, text))
        return qid


def _q(assigner, category_id, text):
    return {"kind": "question", "id": assigner.question(category_id, text), "text": text.strip()}


def _gate(gate_id, text, options, needs_wording=False):
    gate = {"kind": "gate", "id": gate_id, "text": text, "options": options}
    if needs_wording:
        gate["needs_wording_confirmation"] = True
    return gate


def _yes_no(gate_id, text, yes_steps, no_steps=None, needs_wording=False):
    return _gate(
        gate_id,
        text,
        [
            {"value": "yes", "label": YES, "steps": yes_steps},
            {"value": "no", "label": NO, "steps": no_steps or []},
        ],
        needs_wording,
    )


def _build_relationships(cat, assigner):
    """Intro questions outside the gate, then a nested gate — the worked
    example from INTERVIEW_RESTRUCTURE §3.3."""
    cid = cat["id"]
    steps = [_q(assigner, cid, t) for t in cat.get("questions_intro", [])]

    b = cat["branching"]
    y = b["yes_branch"]

    status_opts = []
    for value, questions in y["status_options"].items():
        status_opts.append({
            "value": value,
            "label": STATUS_LABELS[value],
            "steps": [_q(assigner, cid, t) for t in questions],
        })

    yes_steps = [_q(assigner, cid, t) for t in y["shared_questions"]]
    yes_steps.append(
        _gate(f"gate_{cid}_status", y["status_question"], status_opts, needs_wording=True)
    )

    steps.append(_yes_no(f"gate_{cid}_significant", b["screening_question"], yes_steps))
    return steps


def _build_aliyah(cat, assigner):
    """Two INDEPENDENT yes/no questions replacing the source's either/or
    (INTERVIEW_RESTRUCTURE §8.5).

    Birthplace is a FACT-CAPTURE gate: both branches are empty, the answer is
    stored, nothing branches. It sits FIRST and outside the aliyah gate so it
    is asked exactly once — nesting it would force a copy in both branches.

    Only the aliyah answer gates, so "both no" skips without needing its own
    case, and "both yes" runs.
    """
    cid = cat["id"]
    return [
        _yes_no(f"gate_{cid}_born_in_israel", ALIYAH_BORN_TEXT, [], [], needs_wording=True),
        _yes_no(
            f"gate_{cid}_made_aliyah",
            ALIYAH_MADE_TEXT,
            [_q(assigner, cid, t) for t in cat["questions"]],
            needs_wording=True,
        ),
    ]


def _build_category(cat, assigner):
    cid = cat["id"]
    if "branching" in cat:
        steps = _build_relationships(cat, assigner)
    elif cid == "aliyah":
        steps = _build_aliyah(cat, assigner)
    elif cat.get("gated"):
        steps = [
            _yes_no(
                f"gate_{cid}",
                cat["screening_question"],
                [_q(assigner, cid, t) for t in cat["questions"]],
            )
        ]
    else:
        steps = [_q(assigner, cid, t) for t in cat.get("questions", [])]
    return {"id": cid, "name": cat["name"], "steps": steps}


def _retired():
    """The 12 outgoing questions, kept resolvable so the 16 existing
    recordings do not silently lose their life period (§4). Never offered to
    a producer — lookup only."""
    legacy = _load(LEGACY)
    return [
        {"id": q["id"], "category": q["category"], "text": q["text"]}
        for q in legacy["he"]
    ]


def count_questions(steps):
    n = 0
    for s in steps:
        if s["kind"] == "question":
            n += 1
        else:
            for opt in s["options"]:
                n += count_questions(opt["steps"])
    return n


def count_gates(steps):
    n = 0
    for s in steps:
        if s["kind"] == "gate":
            n += 1
            for opt in s["options"]:
                n += count_gates(opt["steps"])
    return n


def main(write: bool) -> int:
    src = _load(SOURCE)
    assigner = Assigner(_existing_ids_by_text(OUTPUT))

    categories = [_build_category(c, assigner) for c in src["categories"]]
    total_q = sum(count_questions(c["steps"]) for c in categories)
    total_g = sum(count_gates(c["steps"]) for c in categories)

    doc = {
        "schema_version": 2,
        "meta": {
            "total_categories": len(categories),
            "total_questions": total_q,
            "total_gates": total_g,
            "generated_from": "docs/interview_content_source.json",
            "generated_on": date.today().isoformat(),
            "note": (
                "Generated by scripts/convert_interview_content.py. Ids are "
                "FROZEN once a recording references them — see that script. "
                "Entries flagged needs_wording_confirmation carry wording "
                "written by the converter, not the producer."
            ),
        },
        "languages": {"he": {"categories": categories}},
        "retired": _retired(),
    }

    print(f"categories : {len(categories)}")
    print(f"questions  : {total_q}   (source meta says {src['meta']['total_questions']})")
    print(f"gates      : {total_g}")
    print(f"retired    : {len(doc['retired'])}")
    print(f"ids reused : {assigner.reused}   newly assigned: {len(assigner.fresh)}")

    expected = src["meta"]["total_questions"]
    if total_q != expected:
        print(f"\n❌ QUESTION COUNT MISMATCH: built {total_q}, source claims {expected}")
        return 1
    print(f"\n✅ question count matches the source ({expected})")

    pending = [
        s["id"]
        for c in categories
        for s in _walk_all(c["steps"])
        if s.get("needs_wording_confirmation")
    ]
    if pending:
        print(f"\n⚠ wording written by the converter, NEEDS PRODUCER CONFIRMATION:")
        for gid in pending:
            print(f"   {gid}")

    if write:
        with open(OUTPUT, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"\nwrote {OUTPUT.relative_to(ROOT)}")
    else:
        print("\nDRY RUN — re-run with --write")
    return 0


def _walk_all(steps):
    for s in steps:
        yield s
        if s["kind"] == "gate":
            for opt in s["options"]:
                yield from _walk_all(opt["steps"])


if __name__ == "__main__":
    sys.exit(main("--write" in sys.argv))
