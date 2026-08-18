"""
Loader for the guided-interview question set (`/record`).

Questions are configurable per recording language rather than hardcoded, since
different storytellers record in different languages (`User.recording_language`)
and the set shown must match the language they will actually speak in.

## Two file formats, ONE internal shape

The file is moving from a flat list per language (v1) to a **step tree** with
data-driven gating (v2) — see docs/INTERVIEW_RESTRUCTURE.md.

Rather than carry two code paths through the whole module, a v1 file is
ADAPTED into the v2 shape at load time: every question becomes a `question`
step and categories are derived from the `category` field. Everything below
therefore works on one representation, and the step-6 cutover is a file swap
with no code change and no second path to delete.

## The step tree

A category holds an ordered list of steps. A step is either:

  {"kind": "question", "id", "text"}
  {"kind": "gate", "id", "text", "options": [{"value", "label", "steps": [...]}]}

A gate's options each carry their own steps, so category-level gating and
sub-section branching are the same construct at different depths, and gates
nest arbitrarily. `"steps": []` is a real outcome meaning "this branch ends" —
answering "no" to a screening question, or the relationships status that has
no follow-ups.

## What must never happen here

Nothing in this module may name a category, a gate, or an option value. The
question set is data; adding a category or a screening question must require
no code change. This is the Phase 1b property and it is load-bearing —
`test_question_identity.py` and `test_interview_schema.py` both pin it.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

_CONFIG_PATH = Path(__file__).resolve().parent / "interview_questions.json"

DEFAULT_LANGUAGE = "he"

QUESTION = "question"
GATE = "gate"


@lru_cache(maxsize=1)
def _load_all() -> Dict[str, Any]:
    """The raw file, in whatever format it is on disk."""
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _adapt_v1(catalog: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """A flat v1 catalog, expressed in the v2 shape.

    Categories come from consecutive runs of the same `category` value, which
    is exactly how v1's ordering already worked — first appearance wins, and
    that ordering IS the chronology. No gates: v1 had none.
    """
    languages: Dict[str, Any] = {}
    for language, questions in catalog.items():
        categories: List[Dict[str, Any]] = []
        by_id: Dict[str, Dict[str, Any]] = {}
        for q in questions:
            cat = by_id.get(q["category"])
            if cat is None:
                cat = {"id": q["category"], "name": q["category_label"], "steps": []}
                by_id[q["category"]] = cat
                categories.append(cat)
            cat["steps"].append({"kind": QUESTION, "id": q["id"], "text": q["text"]})
        languages[language] = {"categories": categories}
    return {"schema_version": 1, "languages": languages, "retired": []}


@lru_cache(maxsize=1)
def _document() -> Dict[str, Any]:
    """The loaded file normalised to the v2 shape, whichever format it is."""
    raw = _load_all()
    if raw.get("schema_version") == 2:
        return raw
    return _adapt_v1(raw)


def _categories(language: str) -> List[Dict[str, Any]]:
    languages = _document()["languages"]
    block = languages.get(language) or languages[DEFAULT_LANGUAGE]
    return block["categories"]


# ── walking the tree ──────────────────────────────────────────────────────


def iter_steps(steps: List[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    """Every step in the subtree, depth-first, gates included.

    Order is document order, which for questions means the order they would be
    asked if every branch were taken. Callers that need only the REACHABLE
    steps for a given producer want `resolve_steps` instead.
    """
    for step in steps:
        yield step
        if step["kind"] == GATE:
            for option in step["options"]:
                yield from iter_steps(option["steps"])


def _chosen_option(
    gate: Dict[str, Any], answers: Dict[str, str]
) -> Optional[Dict[str, Any]]:
    """The option a gate's stored answer selects, or None if it selects none.

    None covers both "not answered yet" and "answered with a value that is no
    longer an option" — the file can change under a half-finished interview.
    Callers must treat those identically, and sharing this is what guarantees
    they do: when `resolve_steps` and `category_is_settled` each decided it
    for themselves, a stale answer resolved to no steps while still reporting
    the category settled, so the flow silently declared a category finished
    that had never been asked.
    """
    chosen = answers.get(gate["id"])
    return next((o for o in gate["options"] if o["value"] == chosen), None)


def resolve_steps(
    steps: List[Dict[str, Any]], answers: Dict[str, str]
) -> List[Dict[str, Any]]:
    """The steps actually reachable given the gate answers so far, in order.

    This is the flow primitive: it is what "where am I and what is left"
    resolves against. A gate is always included — it has to be asked — but its
    branch only unfolds once answered. An unanswered gate therefore TERMINATES
    the list, because nothing after it inside the same branch is knowable yet.

    An answer naming an option that no longer exists (the file changed under a
    half-finished interview) is treated as unanswered rather than crashing:
    the producer is asked again, which is recoverable, where a 500 is not.
    """
    out: List[Dict[str, Any]] = []
    for step in steps:
        out.append(step)
        if step["kind"] != GATE:
            continue
        option = _chosen_option(step, answers)
        if option is None:
            break  # unanswered (or stale answer) — the rest is not yet knowable
        out.extend(resolve_steps(option["steps"], answers))
    return out


def resolve_questions(
    steps: List[Dict[str, Any]], answers: Dict[str, str]
) -> List[Dict[str, Any]]:
    """Just the question steps from `resolve_steps` — what actually gets
    recorded."""
    return [s for s in resolve_steps(steps, answers) if s["kind"] == QUESTION]


def all_question_ids(language: str) -> List[str]:
    """Every question id in the language's interview, DOCUMENT order, gate
    branches included (129 for he) — the order the source document lists
    them, which is also the order the presenter read them when recording
    the per-question videos (docs/PRESENTER_VIDEOS_PLAN.md). Gates are
    excluded: they are click-answer screens with no presenter video."""
    return [
        step["id"]
        for category in _categories(language)
        for step in iter_steps(category["steps"])
        if step["kind"] == QUESTION
    ]


def category_is_settled(category: Dict[str, Any], answers: Dict[str, str]) -> bool:
    """True when no gate in the reachable path is still unanswered.

    Distinct from "complete": a settled category may still have questions left
    to record. It means the SHAPE is known — nothing further depends on an
    answer we do not have — which is what the progress denominator needs
    before it can be trusted.
    """
    return all(
        s["kind"] != GATE or _chosen_option(s, answers) is not None
        for s in resolve_steps(category["steps"], answers)
    )


# ── categories and questions ──────────────────────────────────────────────


def get_categories(language: str) -> List[Dict[str, Any]]:
    """The life periods in chronological order, derived from the file.

    Order is document order — the file's own ordering IS the chronology, so
    reordering it reorders the interview and the timeline with no code change.

    Each entry carries `question_ids` (every question in the category,
    reachable or not) for callers that only need the mapping, and `steps` for
    callers that need the tree.
    """
    out = []
    for cat in _categories(language):
        out.append(
            {
                "category": cat["id"],
                "category_label": cat["name"],
                "question_ids": [
                    s["id"] for s in iter_steps(cat["steps"]) if s["kind"] == QUESTION
                ],
                "steps": cat["steps"],
            }
        )
    return out


def get_questions(language: str) -> List[Dict[str, Any]]:
    """EVERY question in the set, flattened, in document order.

    Each carries `category`/`category_label` so this stays the shape the
    pre-gating callers expect. Note "every question" now means *every question
    that could be asked down any branch* — under gating it is not the sequence
    a given producer will actually be asked. Use `resolve_questions` for that.
    """
    out = []
    for cat in _categories(language):
        for step in iter_steps(cat["steps"]):
            if step["kind"] == QUESTION:
                out.append(
                    {
                        "id": step["id"],
                        "category": cat["id"],
                        "category_label": cat["name"],
                        "text": step["text"],
                    }
                )
    return out


def get_category(language: str, category_id: str) -> Optional[Dict[str, Any]]:
    return next((c for c in _categories(language) if c["id"] == category_id), None)


# ── stable identity ───────────────────────────────────────────────────────
#
# `id` is the ONLY stable handle. `question_index` is positional and moves
# whenever the set is edited — insert a question near the front and every later
# index points somewhere else, taking historical recordings' category with it.
# See docs/FAMILY_TREE_TIMELINE.md §2A.


@lru_cache(maxsize=1)
def _index() -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str], Dict[str, str]]:
    """(step_by_id, id_by_text, category_by_question_id).

    Built once over every language, since an id means the same thing in all of
    them and `category` is language-independent — only labels are translated.
    Retired questions are folded into the category map ONLY: they must resolve
    for history, and must never be offered to a producer.
    """
    doc = _document()
    by_id: Dict[str, Dict[str, Any]] = {}
    by_text: Dict[str, str] = {}
    category_of: Dict[str, str] = {}

    for block in doc["languages"].values():
        for cat in block["categories"]:
            for step in iter_steps(cat["steps"]):
                by_id.setdefault(step["id"], step)
                by_text.setdefault(step["text"].strip(), step["id"])
                if step["kind"] == QUESTION:
                    category_of.setdefault(step["id"], cat["id"])

    for item in doc.get("retired") or []:
        # setdefault, not assignment: a live question of the same id must win,
        # and interview_schema errors on that collision anyway.
        category_of.setdefault(item["id"], item["category"])
        by_text.setdefault(item["text"].strip(), item["id"])

    return by_id, by_text, category_of


def available_languages() -> List[str]:
    """Every language the question set is written in."""
    return list(_document()["languages"].keys())


def step_text(language: str, step_id: str) -> Optional[str]:
    """A step's text in ONE language.

    `_index()` deliberately cannot answer this: it folds every language
    together because an id means the same thing in all of them, so the text it
    holds is whichever language happened to load first. That is right for
    validation and resolution, and wrong for anything a producer HEARS or
    reads — read-aloud has to speak their own `recording_language`.
    """
    for category in _categories(language):
        for step in iter_steps(category["steps"]):
            if step["id"] == step_id:
                return step["text"]
    return None


def is_valid_question_id(question_id: str) -> bool:
    """Guards the ingest payload — a client cannot invent an id.

    Questions only: a gate id is not something a recording can answer.
    """
    step = _index()[0].get(question_id)
    return step is not None and step["kind"] == QUESTION


def is_valid_gate_id(gate_id: str) -> bool:
    step = _index()[0].get(gate_id)
    return step is not None and step["kind"] == GATE


def get_gate(gate_id: str) -> Optional[Dict[str, Any]]:
    step = _index()[0].get(gate_id)
    return step if step is not None and step["kind"] == GATE else None


def gate_option_values(gate_id: str) -> List[str]:
    """The values a gate will accept — so the API can reject anything else
    without naming a single option anywhere in code."""
    gate = get_gate(gate_id)
    return [o["value"] for o in gate["options"]] if gate else []


def question_id_for_text(question_asked: str) -> Optional[str]:
    """Recover an id from verbatim question text.

    Only for recordings made before the id was stored (see
    scripts/backfill_question_ids.py). Exact match by design: a fuzzy match
    that guessed wrong would file a recording under the wrong life period,
    which is worse than leaving it unattributed.
    """
    return _index()[1].get((question_asked or "").strip())


def category_for_question_id(question_id: str) -> Optional[str]:
    """The life period a question belongs to, live or retired.

    The retired fallback is what keeps recordings of withdrawn questions on
    the timeline instead of silently vanishing.
    """
    return _index()[2].get(question_id)


def get_retired() -> List[Dict[str, Any]]:
    return list(_document().get("retired") or [])


def is_valid_category(category_id: str) -> bool:
    """Guards anything that stores a category id — a client cannot invent one.

    True for live categories in ANY language (ids are language-independent,
    only labels are translated) AND for categories that survive only through
    retired questions: a retired-only category still renders on the timeline
    (FAMILY_TREE_TIMELINE.md §3 correction), so things that attach to a
    category — photos, for one — must be attachable there too.
    """
    if not category_id:
        return False
    for block in _document()["languages"].values():
        if any(cat["id"] == category_id for cat in block["categories"]):
            return True
    return any(item["category"] == category_id for item in get_retired())


def cache_clear() -> None:
    """Drop every memoised view of the file.

    For tests that swap the catalog: forgetting one of these silently tests
    the PREVIOUS question set, so they are cleared together rather than
    individually at each call site.

    Tolerates a caller having replaced one of these functions outright (a test
    monkeypatching `_load_all` to return a fixture) — a plain function has no
    cache, and that is not an error here.
    """
    for fn in (_load_all, _document, _index):
        clear = getattr(fn, "cache_clear", None)
        if clear is not None:
            clear()
