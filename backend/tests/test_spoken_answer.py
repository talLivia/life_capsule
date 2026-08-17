"""The spoken renderer (avatar mode on the shared engine) —
docs/AVATAR_SHARED_ENGINE_PLAN.md step 2.

The load-bearing test is the verbatim invariant: the rendered text is
EXACTLY the selected units' texts plus phrases from the fixed banks, and
nothing else — never-invent held structurally, not by prompt promise.
"""

import pytest

from app.services import spoken_answer as sa
from app.services.full_archive_retrieval import UnitSelection, UtteranceUnit


def _unit(uid: str, seg: str, index: int, start: float, text: str) -> UtteranceUnit:
    return UtteranceUnit(
        unit_id=uid,
        segment_id=seg,
        index=index,
        start_sec=start,
        end_sec=start + 2.0,
        text=text,
    )


U1 = _unit("u1", "seg-a", 0, 0.0, "נולדתי בטבריה")
U2 = _unit("u2", "seg-a", 1, 2.5, "וגדלתי שם עד גיל שש")
U4 = _unit("u4", "seg-a", 3, 9.0, "אחר כך עברנו דירה")
U7 = _unit("u7", "seg-b", 6, 0.0, "בצבא שירתתי בחיל האוויר")


def _selection(units, **kw) -> UnitSelection:
    return UnitSelection(clips=[], selected_units=units, **kw)


@pytest.fixture
def no_entities(monkeypatch):
    async def none(segment_ids, group_id):
        return {}

    monkeypatch.setattr(sa, "_entity_names_by_segment", none)


@pytest.mark.asyncio
async def test_a_single_run_is_the_verbatim_text_and_nothing_else(no_entities):
    answer = await sa.render_spoken_answer(_selection([U1, U2]), "prod-1")
    assert answer.text == "נולדתי בטבריה וגדלתי שם עד גיל שש"
    assert not answer.no_story and not answer.read_failed and answer.clarify is None


@pytest.mark.asyncio
async def test_bridges_appear_only_between_non_contiguous_runs(no_entities):
    """u1,u2 are contiguous (one run — no bridge inside); u4 starts a second
    run in the same recording — exactly one bridge, from the same-recording
    bank, at the boundary."""
    answer = await sa.render_spoken_answer(_selection([U1, U2, U4]), "prod-1")
    expected_bridge = sa.SAME_RECORDING_BRIDGES[0]
    assert answer.text == f"נולדתי בטבריה וגדלתי שם עד גיל שש {expected_bridge} אחר כך עברנו דירה"


@pytest.mark.asyncio
async def test_cross_recording_bridge_names_a_validated_entity(monkeypatch):
    async def names(segment_ids, group_id):
        assert segment_ids == ["seg-b"]  # only bridge targets are looked up
        return {"seg-b": {"חיל האוויר"}}

    monkeypatch.setattr(sa, "_entity_names_by_segment", names)
    answer = await sa.render_spoken_answer(_selection([U1, U7]), "prod-1")
    bridge = sa.CROSS_RECORDING_BRIDGES_ENTITY[0].format(entity="חיל האוויר")
    assert answer.text == f"נולדתי בטבריה {bridge} בצבא שירתתי בחיל האוויר"


@pytest.mark.asyncio
async def test_cross_recording_bridge_falls_back_to_generic_without_names(no_entities):
    answer = await sa.render_spoken_answer(_selection([U1, U7]), "prod-1")
    assert answer.text == f"נולדתי בטבריה {sa.CROSS_RECORDING_BRIDGES_GENERIC[0]} בצבא שירתתי בחיל האוויר"


@pytest.mark.asyncio
async def test_the_verbatim_invariant_holds_mechanically(no_entities):
    """Strip every unit text, every bank phrase, and the one engine-validated
    follow-up question from the output — nothing may remain. This is the
    test that keeps never-invent structural (the follow-up question is the
    single scoped exception, producer-decided 2026-08-17)."""
    follow_up = {"question": "רוצה לשמוע על הצבא?"}
    answer = await sa.render_spoken_answer(
        _selection([U1, U2, U4, U7], follow_up=follow_up),
        "prod-1",
    )
    residue = answer.text
    for u in (U1, U2, U4, U7):
        residue = residue.replace(u.text, "")
    for bank in (
        sa.SAME_RECORDING_BRIDGES,
        sa.CROSS_RECORDING_BRIDGES_GENERIC,
        [follow_up["question"]],
    ):
        for phrase in bank:
            residue = residue.replace(phrase, "")
    assert residue.strip() == "", f"unexplained residue: {residue!r}"


@pytest.mark.asyncio
async def test_a_follow_up_speaks_the_generated_question_itself_last(no_entities):
    """The offer rides the same utterance as the answer (so the mic reopens
    right after it) and speaks the engine's generated question VERBATIM —
    the same text the chat card shows, no separate generic phrasing."""
    answer = await sa.render_spoken_answer(
        _selection([U1, U2], follow_up={"question": "רוצה לשמוע על הצבא?"}),
        "prod-1",
    )
    assert answer.text == "נולדתי בטבריה וגדלתי שם עד גיל שש רוצה לשמוע על הצבא?"
    assert answer.follow_up == {"question": "רוצה לשמוע על הצבא?"}


@pytest.mark.asyncio
async def test_wording_is_stable_across_calls(no_entities):
    a = await sa.render_spoken_answer(_selection([U1, U4, U7]), "prod-1")
    b = await sa.render_spoken_answer(_selection([U1, U4, U7]), "prod-1")
    assert a.text == b.text


@pytest.mark.asyncio
async def test_read_failed_speaks_the_transient_line_never_no_story(no_entities):
    answer = await sa.render_spoken_answer(
        UnitSelection(clips=[], selected_units=[], read_failed=True), "prod-1"
    )
    assert answer.read_failed
    assert answer.text == sa.TRANSIENT_FAILURE_FALLBACK
    assert not answer.no_story


@pytest.mark.asyncio
async def test_clarify_replaces_the_answer_and_speaks_the_fixed_line(no_entities):
    clarify = {"question": "לאיזה אמנון התכוונת?", "options": ["אמנון", "אמנון נחום"]}
    answer = await sa.render_spoken_answer(
        UnitSelection(clips=[], selected_units=[], clarify=clarify), "prod-1"
    )
    assert answer.clarify == clarify
    assert answer.text == sa.CLARIFY_SPOKEN_LINE
    assert answer.shown_units == []


@pytest.mark.asyncio
async def test_no_story_prefers_the_tailored_line_and_keeps_the_offer(no_entities):
    follow_up = {"question": "רוצה לשמוע על הצבא?"}
    answer = await sa.render_spoken_answer(
        UnitSelection(
            clips=[], selected_units=[], no_story_text="זה כל מה שיש לי על אמנון",
            follow_up=follow_up,
        ),
        "prod-1",
    )
    assert answer.no_story
    assert answer.text == f"זה כל מה שיש לי על אמנון {follow_up['question']}"
    assert answer.follow_up == follow_up


@pytest.mark.asyncio
async def test_shown_units_match_the_video_renderers_shape(no_entities):
    """Both renderers must feed the same history machinery — same keys,
    same key format."""
    answer = await sa.render_spoken_answer(_selection([U1, U7]), "prod-1")
    assert [set(u.keys()) for u in answer.shown_units] == [
        {"key", "unit_id", "text"},
        {"key", "unit_id", "text"},
    ]
    assert answer.shown_units[0]["key"] == "seg-a:0.00"  # _unit_key's %.2f format
    assert answer.shown_units[1]["unit_id"] == "u7"
