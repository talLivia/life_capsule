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
from typing import Any, Dict, List

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
