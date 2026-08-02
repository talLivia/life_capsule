"""
Step 1 of docs/INTERVIEW_RESTRUCTURE.md — the v2 question file and its linter.

The file is 16 categories, 129 questions and 9 nested gates, hand-edited from
here on. At that size a typo does not raise; it produces a flow that quietly
skips a branch, or a question id that stops resolving to a life period. These
tests are the thing that turns those into a failure.

A validator nobody has seen fail is not evidence of anything, so most of these
feed it BROKEN documents and assert it complains.
"""

import copy
import json
from pathlib import Path

import pytest

from app.interview_schema import SCHEMA_VERSION, count_gates, count_questions, validate

V2_PATH = Path(__file__).resolve().parent.parent / "app" / "interview_questions_v2.json"


@pytest.fixture(scope="module")
def doc():
    with open(V2_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── the real file ─────────────────────────────────────────────────────────


def test_the_generated_file_is_structurally_valid(doc):
    errors, _ = validate(doc)
    assert errors == [], f"v2 file has structural errors: {errors}"


def test_it_carries_all_129_questions_and_16_categories(doc):
    cats = doc["languages"]["he"]["categories"]
    assert len(cats) == 16
    assert sum(count_questions(c["steps"]) for c in cats) == 129


def test_every_retired_question_resolves_to_a_category(doc):
    """The 16 existing recordings depend on this. A retired id that resolves
    to nothing drops its recordings off the timeline silently."""
    retired = doc["retired"]
    assert len(retired) == 12
    assert all(r["category"] for r in retired)


def test_gates_express_both_required_patterns(doc):
    """Category gating and sub-section branching, via the SAME construct."""
    cats = {c["id"]: c for c in doc["languages"]["he"]["categories"]}

    # category gating: the whole body lives inside one gate's yes branch
    mil = cats["military_service"]
    assert len(mil["steps"]) == 1 and mil["steps"][0]["kind"] == "gate"
    yes = next(o for o in mil["steps"][0]["options"] if o["value"] == "yes")
    no = next(o for o in mil["steps"][0]["options"] if o["value"] == "no")
    assert count_questions(yes["steps"]) == 8
    assert no["steps"] == [], "answering no must lead to no questions at all"

    # branching: intro questions OUTSIDE the gate, then a nested gate
    rel = cats["relationships"]
    intro = [s for s in rel["steps"] if s["kind"] == "question"]
    assert len(intro) == 4
    outer = next(s for s in rel["steps"] if s["kind"] == "gate")
    rel_yes = next(o for o in outer["options"] if o["value"] == "yes")
    inner = [s for s in rel_yes["steps"] if s["kind"] == "gate"]
    assert len(inner) == 1, "the status question must be nested inside the yes branch"
    statuses = {o["value"]: o for o in inner[0]["options"]}
    assert set(statuses) == {"together", "widowed", "separated_divorced"}
    # 'together' legitimately ends the category — an empty branch is a real
    # outcome, not an unfinished edit
    assert statuses["together"]["steps"] == []
    assert count_questions(statuses["widowed"]["steps"]) == 3


def test_aliyah_asks_two_independent_questions_and_only_one_gates(doc):
    """INTERVIEW_RESTRUCTURE §8.5. Birthplace is a fact-capture gate: stored,
    but it must not decide whether the category runs."""
    cats = {c["id"]: c for c in doc["languages"]["he"]["categories"]}
    steps = cats["aliyah"]["steps"]
    assert [s["kind"] for s in steps] == ["gate", "gate"]

    born, made = steps
    assert born["id"].endswith("born_in_israel")
    # both branches empty => answering it never changes what is asked next
    assert all(o["steps"] == [] for o in born["options"]), "birthplace must not gate"

    made_yes = next(o for o in made["options"] if o["value"] == "yes")
    made_no = next(o for o in made["options"] if o["value"] == "no")
    assert count_questions(made_yes["steps"]) == 9
    assert made_no["steps"] == []
    # so "both no" skips, and "both yes" runs, with no special case for either


def test_converter_written_wording_is_flagged_not_hidden(doc):
    """Three prompts are worded by the converter, not the producer. They must
    stay visible as warnings until confirmed."""
    _, warnings = validate(doc)
    pending = [w for w in warnings if "awaiting producer confirmation" in w]
    assert len(pending) == 3


# ── the validator itself ──────────────────────────────────────────────────


def _break(doc, fn):
    broken = copy.deepcopy(doc)
    fn(broken)
    return validate(broken)[0]


def test_rejects_a_duplicate_id(doc):
    def dupe(d):
        cats = d["languages"]["he"]["categories"]
        cats[1]["steps"][0]["id"] = cats[0]["steps"][0]["id"]

    assert any("duplicate id" in e for e in _break(doc, dupe))


def test_rejects_a_gate_with_one_option(doc):
    def truncate(d):
        for c in d["languages"]["he"]["categories"]:
            for s in c["steps"]:
                if s["kind"] == "gate":
                    s["options"] = s["options"][:1]
                    return

    assert any("at least 2 options" in e for e in _break(doc, truncate))


def test_rejects_an_option_missing_its_steps_key(doc):
    """`steps: []` means "this branch ends" — a missing key is ambiguous
    between that and an unfinished edit, so it must not be tolerated."""

    def drop(d):
        for c in d["languages"]["he"]["categories"]:
            for s in c["steps"]:
                if s["kind"] == "gate":
                    del s["options"][0]["steps"]
                    return

    assert any("steps must be a list" in e for e in _break(doc, drop))


def test_rejects_empty_question_text(doc):
    def blank(d):
        d["languages"]["he"]["categories"][0]["steps"][0]["text"] = "   "

    assert any("text is empty" in e for e in _break(doc, blank))


def test_rejects_a_retired_id_shadowing_a_live_question(doc):
    """The dangerous collision: category_for_question_id searches live steps
    then retired, so one id meaning two questions resolves to whichever comes
    first."""

    def shadow(d):
        live = d["languages"]["he"]["categories"][0]["steps"][0]["id"]
        d["retired"][0]["id"] = live

    errors = _break(doc, shadow)
    assert any("collides with a live" in e for e in errors)


def test_tolerates_a_retired_id_matching_a_category_id(doc):
    """The harmless one, which is real in this file: the outgoing question id
    'military_service' is also an incoming category id. Different indexes, so
    it warns rather than failing the build."""
    errors, warnings = validate(doc)
    assert errors == []
    assert any("also a live category" in w for w in warnings)


def test_rejects_drifted_meta_counts(doc):
    def drift(d):
        d["meta"]["total_questions"] = 999

    assert any("meta.total_questions" in e for e in _break(doc, drift))


def test_rejects_an_unknown_schema_version(doc):
    def bump(d):
        d["schema_version"] = SCHEMA_VERSION + 1

    assert any("schema_version" in e for e in _break(doc, bump))


def test_knows_nothing_about_specific_categories():
    """The Phase 1b property: no category, gate or option value may be named
    in code. A minimal invented document must validate cleanly."""
    invented = {
        "schema_version": SCHEMA_VERSION,
        "meta": {"total_categories": 1, "total_questions": 1, "total_gates": 1},
        "languages": {
            "xx": {
                "categories": [
                    {
                        "id": "a_category_that_does_not_exist",
                        "name": "invented",
                        "steps": [
                            {
                                "kind": "gate",
                                "id": "gate_invented",
                                "text": "a question nobody has asked",
                                "options": [
                                    {"value": "alpha", "label": "A", "steps": [
                                        {"kind": "question", "id": "q_invented",
                                         "text": "and another"}
                                    ]},
                                    {"value": "beta", "label": "B", "steps": []},
                                    {"value": "gamma", "label": "C", "steps": []},
                                ],
                            }
                        ],
                    }
                ]
            }
        },
        "retired": [],
    }
    errors, _ = validate(invented)
    assert errors == [], errors
    assert count_gates(invented["languages"]["xx"]["categories"][0]["steps"]) == 1
