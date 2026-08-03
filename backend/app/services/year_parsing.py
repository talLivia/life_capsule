"""
Turning what a producer typed into a year, or refusing to.

Phase 3 of docs/FAMILY_TREE_TIMELINE.md.

The two requirements pull against each other on purpose: be forgiving about
FORM ("1973", "בערך 1973", "in 1973"), and refuse rather than guess about
CONTENT. A wrong year silently reorders someone's life on a timeline, and
nothing about the display would look broken — so anything needing a judgement
call is handed back, never rounded into a number.

What that rules out is the tempting part. "early 70s" and "mid 1970s" are
spans; picking 1970 out of either is an invention the producer never made.
Same for "when I was young". Those return `None` with a reason, and the caller
tells the producer it was not understood instead of storing something
plausible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

# Wide enough for a great-grandparent's birth, narrow enough that a stray
# 4-digit number (a house number, a sum of money) is unlikely to pass.
MIN_YEAR = 1850
MAX_FUTURE_SLACK = 1  # allow "next year" for an expected birth, nothing more

# A span, not a date. Checked BEFORE any digits are read, because both the
# 4-digit and 2-digit rules would otherwise happily pull a year out of the
# middle of one ("mid 1970s" -> 1970).
_RANGE_MARKERS = (
    re.compile(r"\d0\s*['’]?s", re.IGNORECASE),          # 70s, 1970s, 70's
    re.compile(r"early|mid|late|beginning of|end of", re.IGNORECASE),
    re.compile(r"תחילת|אמצע|סוף|שנות"),
)


@dataclass(frozen=True)
class YearParse:
    """A parsed year, or a refusal with a reason the producer can act on."""

    year: Optional[int] = None
    reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.year is not None


def parse_year(raw: str, *, today: Optional[date] = None) -> YearParse:
    """A single unambiguous year, or a refusal.

    Accepts a 4-digit year anywhere in the string, so "בערך 1973" and "in 1973"
    both work — the surrounding words carry no information the year does not.

    Accepts a bare 2-digit year ONLY when the century is forced rather than
    chosen: "73" can only mean 1973 because 2073 has not happened. "20" is
    genuinely ambiguous between 1920 and 2020 and is refused, because both are
    answers a producer might really mean.
    """
    text = (raw or "").strip()
    if not text:
        return YearParse(reason="empty")

    latest = (today or date.today()).year + MAX_FUTURE_SLACK

    if any(marker.search(text) for marker in _RANGE_MARKERS):
        return YearParse(reason="that looks like a range, not a single year")

    four = re.findall(r"(?<!\d)(\d{4})(?!\d)", text)
    if four:
        if len(set(four)) > 1:
            # "1973-1975" is a span; storing either end would be a choice the
            # producer did not make.
            return YearParse(reason="more than one year")
        year = int(four[0])
        if year < MIN_YEAR or year > latest:
            return YearParse(reason=f"{year} is outside {MIN_YEAR}-{latest}")
        return YearParse(year=year)

    two = re.findall(r"(?<!\d)(\d{2})(?!\d)", text)
    if len(two) == 1:
        n = int(two[0])
        candidates = [c for c in (1900 + n, 2000 + n) if MIN_YEAR <= c <= latest]
        if len(candidates) == 1:
            return YearParse(year=candidates[0])
        # Both centuries are real answers — an ambiguity, not a formatting
        # problem — so it goes back to the producer.
        return YearParse(
            reason=f"'{two[0]}' could be {candidates[0]} or {candidates[1]}"
        )

    return YearParse(reason="no year found")
