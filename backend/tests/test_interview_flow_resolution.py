"""
Step 2 of docs/INTERVIEW_RESTRUCTURE.md — walking the step tree.

`resolve_steps` is the flow primitive everything later sits on: the accordion's
"where am I", the per-category progress denominator, and whether a category is
complete all resolve against it. A bug here does not raise, it silently skips
or repeats a branch, so the gating combinations are enumerated explicitly.

Run against the REAL v2 file rather than a toy, because the shapes that matter
(a gate whose branches are both empty, a branch that ends with nothing, a gate
nested inside another gate's option) are exactly the ones a toy would omit.
"""

import json
from pathlib import Path

import pytest

from app import interview_config as ic

V2_PATH = Path(__file__).resolve().parent.parent / "app" / "interview_questions_v2.json"


@pytest.fixture
def v2(monkeypatch):
    """Point interview_config at the v2 file. Cutover (step 6) makes this the
    real file; until then the app still loads v1 and this proves the same code
    handles both."""
    with open(V2_PATH, encoding="utf-8") as f:
        doc = json.load(f)
    monkeypatch.setattr(ic, "_load_all", lambda: doc)
    ic.cache_clear()
    yield doc
    ic.cache_clear()


def _cat(category_id):
    return ic.get_category("he", category_id)


# ── the v1 adapter ────────────────────────────────────────────────────────


def test_v1_still_works_unchanged():
    """The live file is still v1. It must adapt into the same shape, or the
    app breaks before cutover ever happens."""
    cats = ic.get_categories("he")
    assert len(cats) == 5
    assert all(c["steps"] for c in cats)
    # v1 has no gates, so every step resolves with no answers at all
    for c in cats:
        assert ic.resolve_steps(c["steps"], {}) == c["steps"]
        assert ic.category_is_settled({"steps": c["steps"]}, {})


def test_v1_and_v2_present_the_same_interface(v2):
    assert len(ic.get_categories("he")) == 16
    assert len(ic.get_questions("he")) == 129


# ── category-level gating ─────────────────────────────────────────────────


def test_unanswered_gate_hides_everything_behind_it(v2):
    """The gate itself is always offered — it has to be asked — but nothing
    after it is knowable until it is answered."""
    cat = _cat("military_service")
    steps = ic.resolve_steps(cat["steps"], {})
    assert len(steps) == 1 and steps[0]["kind"] == "gate"
    assert ic.resolve_questions(cat["steps"], {}) == []
    assert not ic.category_is_settled(cat, {})


def test_no_skips_the_whole_category(v2):
    cat = _cat("military_service")
    answers = {"gate_military_service": "no"}
    assert ic.resolve_questions(cat["steps"], answers) == []
    # settled: nothing further depends on an answer we lack, so the category
    # is finished despite having recorded nothing
    assert ic.category_is_settled(cat, answers)


def test_yes_opens_the_category(v2):
    cat = _cat("military_service")
    answers = {"gate_military_service": "yes"}
    assert len(ic.resolve_questions(cat["steps"], answers)) == 8
    assert ic.category_is_settled(cat, answers)


# ── aliyah: two independent gates, only one of which gates ────────────────


@pytest.mark.parametrize(
    "born,made,expected",
    [
        ("no", "yes", 9),   # the ordinary case
        ("yes", "no", 0),   # native-born
        ("no", "no", 0),    # born elsewhere, never made aliyah — valid
        ("yes", "yes", 9),  # born here, emigrated, later made aliyah
    ],
)
def test_only_the_aliyah_answer_gates(v2, born, made, expected):
    """All four combinations, per INTERVIEW_RESTRUCTURE §8.5. Birthplace is
    captured but must never change what is asked."""
    cat = _cat("aliyah")
    answers = {
        "gate_aliyah_born_in_israel": born,
        "gate_aliyah_made_aliyah": made,
    }
    assert len(ic.resolve_questions(cat["steps"], answers)) == expected
    assert ic.category_is_settled(cat, answers)


def test_birthplace_alone_never_settles_the_category(v2):
    """Answering only the fact-capture gate leaves the real gate open."""
    cat = _cat("aliyah")
    assert not ic.category_is_settled(cat, {"gate_aliyah_born_in_israel": "yes"})


# ── relationships: nested gates ───────────────────────────────────────────


def test_intro_questions_are_asked_before_any_gate(v2):
    cat = _cat("relationships")
    steps = ic.resolve_steps(cat["steps"], {})
    kinds = [s["kind"] for s in steps]
    assert kinds[:4] == ["question"] * 4, "the 4 intro questions precede the gate"
    assert kinds[4] == "gate"
    assert len(kinds) == 5, "nothing past the unanswered gate is knowable"


def test_no_to_significant_relationship_keeps_only_the_intros(v2):
    cat = _cat("relationships")
    answers = {"gate_relationships_significant": "no"}
    assert len(ic.resolve_questions(cat["steps"], answers)) == 4
    assert ic.category_is_settled(cat, answers)


def test_yes_reveals_the_shared_block_and_a_second_gate(v2):
    """The nested gate must be reachable but unanswered — 4 intro + 9 shared,
    and the status question still to come."""
    cat = _cat("relationships")
    answers = {"gate_relationships_significant": "yes"}
    assert len(ic.resolve_questions(cat["steps"], answers)) == 13
    assert not ic.category_is_settled(cat, answers), "the status gate is still open"


@pytest.mark.parametrize(
    "status,expected",
    [("together", 13), ("widowed", 16), ("separated_divorced", 16)],
)
def test_status_selects_its_own_follow_ups(v2, status, expected):
    cat = _cat("relationships")
    answers = {
        "gate_relationships_significant": "yes",
        "gate_relationships_status": status,
    }
    assert len(ic.resolve_questions(cat["steps"], answers)) == expected
    assert ic.category_is_settled(cat, answers)


def test_together_is_a_real_outcome_not_an_unfinished_branch(v2):
    """It adds no questions and still settles the category — the empty branch
    has to be a first-class outcome."""
    cat = _cat("relationships")
    full = {"gate_relationships_significant": "yes", "gate_relationships_status": "together"}
    partial = {"gate_relationships_significant": "yes"}
    assert ic.resolve_questions(cat["steps"], full) == ic.resolve_questions(cat["steps"], partial)
    assert ic.category_is_settled(cat, full)
    assert not ic.category_is_settled(cat, partial)


# ── robustness ────────────────────────────────────────────────────────────


def test_a_stale_answer_is_treated_as_unanswered(v2):
    """The file can change under a half-finished interview. Asking again is
    recoverable; a 500 during recording is not."""
    cat = _cat("military_service")
    answers = {"gate_military_service": "an_option_that_no_longer_exists"}
    assert ic.resolve_questions(cat["steps"], answers) == []
    assert not ic.category_is_settled(cat, answers)


def test_answers_for_other_gates_are_ignored(v2):
    """Gate answers are stored per session across all categories, so every
    category resolves against the whole set and must ignore the rest."""
    cat = _cat("military_service")
    answers = {
        "gate_military_service": "yes",
        "gate_relationships_significant": "no",
        "gate_aliyah_made_aliyah": "yes",
    }
    assert len(ic.resolve_questions(cat["steps"], answers)) == 8


# ── identity, gates included ──────────────────────────────────────────────


def test_gate_ids_are_not_valid_question_ids(v2):
    """A recording answers a question, never a gate. Accepting a gate id at
    ingest would file footage against a prompt that takes no footage."""
    assert ic.is_valid_gate_id("gate_military_service")
    assert not ic.is_valid_question_id("gate_military_service")
    assert ic.is_valid_question_id("military_service_q01")
    assert not ic.is_valid_gate_id("military_service_q01")


def test_gate_option_values_come_from_the_data(v2):
    """The API validates an answer without naming a single option in code."""
    assert set(ic.gate_option_values("gate_military_service")) == {"yes", "no"}
    assert set(ic.gate_option_values("gate_relationships_status")) == {
        "together", "widowed", "separated_divorced",
    }
    assert ic.gate_option_values("not_a_gate") == []


def test_retired_questions_still_resolve_to_a_category(v2):
    """The 16 existing recordings depend on this after cutover."""
    assert ic.category_for_question_id("childhood_home") == "childhood"
    assert ic.category_for_question_id("post_military_next") == "post_military"
    # ...but are never offered to a producer
    assert "childhood_home" not in {q["id"] for q in ic.get_questions("he")}
    assert not ic.is_valid_question_id("childhood_home")


def test_every_live_question_resolves_to_its_category(v2):
    for q in ic.get_questions("he"):
        assert ic.category_for_question_id(q["id"]) == q["category"]
