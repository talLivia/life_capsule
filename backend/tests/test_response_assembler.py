"""
Tests for response_assembler.py — now only the template banks and fixed
constants that survived the 2026-08-19 step-5 retirement (docs/
AVATAR_SHARED_ENGINE_PLAN.md §5). `assemble_response` and its pipeline
were deleted with their tests; both renderers import these banks, so the
properties pinned here (stable rotation, entity-only injection) still
guard production text in BOTH modes.
"""

from app.services import response_assembler as ra


def test_pick_bridge_phrase_cycles_through_bank():
    n = len(ra.BRIDGE_PHRASE_TEMPLATES)
    for i in range(n * 2):
        assert ra._pick_bridge_phrase(i) == ra.BRIDGE_PHRASE_TEMPLATES[i % n]


def test_no_story_about_injects_only_the_entity_and_rotates_stably():
    n = len(ra.NO_MORE_STORY_ABOUT_TEMPLATES)
    for v in range(n * 2):
        line = ra.no_story_about("אמנון", variant=v)
        assert line == ra.NO_MORE_STORY_ABOUT_TEMPLATES[v % n].format(entity="אמנון")
        assert "אמנון" in line


def test_fallback_constants_are_distinct_sentences():
    """The outage line and the no-story line must never be the same words —
    that exact conflation was the documented false-statement bug."""
    assert ra.NO_STORY_FALLBACK != ra.TRANSIENT_FAILURE_FALLBACK
    assert ra.NO_STORY_FALLBACK
    assert ra.TRANSIENT_FAILURE_FALLBACK
