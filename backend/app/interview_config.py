"""
Loader for the fixed guided-interview question sequence (`/record`, Prompt 4).

Questions are configurable per recording language (`interview_questions.json`,
keyed "he"/"en") rather than hardcoded, since different storytellers record
in different languages (a per-User field — see `User.recording_language` in
models.py) and the question set shown must match the language the
storyteller will actually speak in.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

_CONFIG_PATH = Path(__file__).resolve().parent / "interview_questions.json"

DEFAULT_LANGUAGE = "he"


@lru_cache(maxsize=1)
def _load_all() -> Dict[str, List[Dict[str, Any]]]:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_questions(language: str) -> List[Dict[str, Any]]:
    """Fixed question sequence for a recording language.

    Falls back to DEFAULT_LANGUAGE if the requested language has no
    configured set — better than a 500 for a language not yet localized.
    """
    catalog = _load_all()
    return catalog.get(language) or catalog[DEFAULT_LANGUAGE]


# ── Stable question identity ──────────────────────────────────────────────
#
# `id` is the ONLY stable handle on a question. `question_index` is positional
# and moves whenever the set is edited: insert a question near the front and
# every later index silently points somewhere else, taking historical
# recordings' category with it. See docs/FAMILY_TREE_TIMELINE.md §2A.
#
# Everything category-shaped derives from the JSON at read time. Nothing here
# may be copied into a constant, a migration, or a lookup table — the question
# set is about to roughly double and must need no code change to do it.


@lru_cache(maxsize=1)
def _by_id() -> Dict[str, Dict[str, Any]]:
    """id -> the question, from whichever language defines it first.

    `category` is language-independent (the same id carries the same category
    in every language; only `category_label` is translated), so a single index
    can answer "what category is this" without a language argument.
    """
    index: Dict[str, Dict[str, Any]] = {}
    for questions in _load_all().values():
        for q in questions:
            index.setdefault(q["id"], q)
    return index


@lru_cache(maxsize=1)
def _by_text() -> Dict[str, str]:
    """Verbatim question text -> id, across EVERY language.

    Only for recovering the id of a recording made before it was stored (see
    scripts/backfill_question_ids.py). Exact match by design: a fuzzy match
    that guessed wrong would file a recording under the wrong life period,
    which is worse than leaving it unattributed.
    """
    index: Dict[str, str] = {}
    for questions in _load_all().values():
        for q in questions:
            index.setdefault(q["text"].strip(), q["id"])
    return index


def is_valid_question_id(question_id: str) -> bool:
    """Guards the ingest payload — a client cannot invent an id."""
    return question_id in _by_id()


def question_id_for_text(question_asked: str) -> Optional[str]:
    return _by_text().get((question_asked or "").strip())


def category_for_question_id(question_id: str) -> Optional[str]:
    q = _by_id().get(question_id)
    return q["category"] if q else None


def get_categories(language: str) -> List[Dict[str, Any]]:
    """The life periods, in chronological order, derived from the JSON.

    Order is FIRST APPEARANCE in the question file, so the file's own ordering
    is the chronology and reordering it reorders the timeline — no code change,
    no migration. Returns
    `[{category, category_label, question_ids: [...]}, ...]`.

    This is the single source for anything that groups by life period. A second
    copy of this list anywhere is the bug this function exists to prevent.
    """
    ordered: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    for q in get_questions(language):
        bucket = seen.get(q["category"])
        if bucket is None:
            bucket = {
                "category": q["category"],
                "category_label": q["category_label"],
                "question_ids": [],
            }
            seen[q["category"]] = bucket
            ordered.append(bucket)
        bucket["question_ids"].append(q["id"])
    return ordered
