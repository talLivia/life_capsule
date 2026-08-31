"""Core compression: the deterministic gate, the never-invent contract,
and fail-open on every failure shape. All LLM calls mocked."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import core_compression as cc


def _u(uid, dur=10.0):
    # segment_id is always present on real UtteranceUnits; the three-rule
    # redesign reads it for the category annotation
    return SimpleNamespace(
        unit_id=uid, segment_id="s-test", start_sec=0.0, end_sec=dur, text=f"t-{uid}"
    )


def _explosive(monkeypatch):
    async def boom(**kw):  # pragma: no cover - reaching it IS the failure
        raise AssertionError("LLM called below the threshold")

    monkeypatch.setattr(cc.llm_service, "generate_response", boom)


# ── the gate ────────────────────────────────────────────────────────────────


def test_duration_is_the_metric():
    assert cc.core_duration_sec([_u("u1", 10), _u("u2", 20.5)]) == 30.5


@pytest.mark.asyncio
async def test_under_threshold_is_untouched_no_llm(monkeypatch):
    _explosive(monkeypatch)
    units = [_u(f"u{i}", 10) for i in range(14)]  # 140s < 150s
    out, fu, compressed = await cc.maybe_compress("q", units, "he")
    assert out is units and fu is None and compressed is False


@pytest.mark.asyncio
async def test_exactly_at_threshold_is_untouched(monkeypatch):
    _explosive(monkeypatch)
    units = [_u(f"u{i}", 15) for i in range(10)]  # exactly 150s
    out, _, compressed = await cc.maybe_compress("q", units, "he")
    assert out is units and compressed is False


@pytest.mark.asyncio
async def test_disabled_via_zero_threshold(monkeypatch):
    _explosive(monkeypatch)
    monkeypatch.setattr(settings, "CORE_COMPRESSION_THRESHOLD_SEC", 0)
    units = [_u(f"u{i}", 100) for i in range(20)]
    out, _, compressed = await cc.maybe_compress("q", units, "he")
    assert out is units and compressed is False


# ── over the threshold ──────────────────────────────────────────────────────


def _big():
    return [_u(f"u{i}", 10) for i in range(30)]  # 300s > 150s


@pytest.mark.asyncio
async def test_compresses_to_subset_and_offers_remainder(monkeypatch):
    reply = '{"unit_ids": ["u2", "u0", "u5"], "follow_up": {"question": "עוד?", "unit_ids": ["u7", "u8"]}}'
    monkeypatch.setattr(cc.llm_service, "generate_response", AsyncMock(return_value=reply))
    units = _big()
    out, fu, compressed = await cc.maybe_compress("q", units, "he")
    assert compressed
    assert [u.unit_id for u in out] == ["u2", "u0", "u5"]  # MODEL stitch order (2026-08-31.2)
    assert fu == {"question": "עוד?", "unit_ids": ["u7", "u8"]}


@pytest.mark.asyncio
async def test_never_invent_foreign_ids_dropped(monkeypatch):
    reply = '{"unit_ids": ["u1", "u999", "nonsense"], "follow_up": {"question": "עוד?", "unit_ids": ["u1", "u888", "u3"]}}'
    monkeypatch.setattr(cc.llm_service, "generate_response", AsyncMock(return_value=reply))
    out, fu, compressed = await cc.maybe_compress("q", _big(), "he")
    assert compressed and [u.unit_id for u in out] == ["u1"]
    # offer: foreign dropped, overlap with the kept core dropped
    assert fu["unit_ids"] == ["u3"]


@pytest.mark.asyncio
async def test_all_foreign_fails_open(monkeypatch):
    reply = '{"unit_ids": ["x1", "x2"]}'
    monkeypatch.setattr(cc.llm_service, "generate_response", AsyncMock(return_value=reply))
    units = _big()
    out, fu, compressed = await cc.maybe_compress("q", units, "he")
    assert out is units and compressed is False  # never serve nothing


@pytest.mark.asyncio
async def test_llm_error_fails_open(monkeypatch):
    monkeypatch.setattr(
        cc.llm_service, "generate_response", AsyncMock(side_effect=RuntimeError("down"))
    )
    units = _big()
    out, fu, compressed = await cc.maybe_compress("q", units, "he")
    assert out is units and compressed is False


@pytest.mark.asyncio
async def test_unparseable_fails_open(monkeypatch):
    monkeypatch.setattr(
        cc.llm_service, "generate_response", AsyncMock(return_value="sorry, no JSON here")
    )
    units = _big()
    out, _, compressed = await cc.maybe_compress("q", units, "he")
    assert out is units and compressed is False


@pytest.mark.asyncio
async def test_offer_without_question_or_ids_is_dropped_not_fatal(monkeypatch):
    reply = '{"unit_ids": ["u1", "u2"], "follow_up": {"question": "", "unit_ids": []}}'
    monkeypatch.setattr(cc.llm_service, "generate_response", AsyncMock(return_value=reply))
    out, fu, compressed = await cc.maybe_compress("q", _big(), "he")
    assert compressed and fu is None and [u.unit_id for u in out] == ["u1", "u2"]


# ── the three-rule design (2026-08-30) ──────────────────────────────────────
# Category + close-family enforcement happen in CODE on the model's reply;
# these tests pin that enforcement. All LLM/DB access mocked.


def _cu(uid, seg, dur=10.0, text=None):
    return SimpleNamespace(
        unit_id=uid, segment_id=seg, start_sec=0.0, end_sec=dur, text=text or f"t-{uid}"
    )


def _mixed():
    """30 units, 300s: s-fam is 'children' category, s-child is 'childhood',
    one childhood unit explicitly names a close-family member."""
    units = [_cu(f"u{i}", "s-fam", 10) for i in range(15)]
    units += [_cu(f"u{i}", "s-child", 10) for i in range(15, 30)]
    units[20] = _cu("u20", "s-child", 10, text="גדלתי עם דנה אחותי")
    return units, {"s-fam": "children", "s-child": "childhood"}


def _no_db(monkeypatch, names=("דנה",)):
    async def fake(group_id):
        return set(names)

    monkeypatch.setattr(cc, "_close_family_names", fake)


@pytest.mark.asyncio
async def test_rule1_demotes_nonmatching_category_to_offer(monkeypatch):
    units, cats = _mixed()
    reply = (
        '{"category": "children", "unit_ids": ["u0", "u1", "u16", "u17"],'
        ' "follow_up": {"question": "עוד?", "unit_ids": ["u18"]}}'
    )
    monkeypatch.setattr(cc.llm_service, "generate_response", AsyncMock(return_value=reply))
    _no_db(monkeypatch)
    out, fu, compressed = await cc.maybe_compress("q", units, "he", cats, "g1")
    assert compressed
    # u16/u17 are childhood-category, not close-family: demoted, not served
    assert [u.unit_id for u in out] == ["u0", "u1"]
    # demoted ids LEAD the offer, then the model's own pick; order = input
    assert fu["unit_ids"] == ["u16", "u17", "u18"]


@pytest.mark.asyncio
async def test_rule3_close_family_unit_survives_cross_category(monkeypatch):
    units, cats = _mixed()
    reply = '{"category": "children", "unit_ids": ["u0", "u20", "u21"]}'
    monkeypatch.setattr(cc.llm_service, "generate_response", AsyncMock(return_value=reply))
    _no_db(monkeypatch)  # דנה is first-degree; u20's text names her
    out, fu, compressed = await cc.maybe_compress("q", units, "he", cats, "g1")
    assert compressed
    # u20 stays via RULE 3 (unit-scoped); u21, same recording, does NOT
    assert [u.unit_id for u in out] == ["u0", "u20"]


@pytest.mark.asyncio
async def test_rule3_is_unit_scoped_not_recording_scoped(monkeypatch):
    units, cats = _mixed()
    reply = (
        '{"category": "children", "unit_ids": ["u20", "u21", "u22", "u23"],'
        ' "follow_up": {"question": "עוד?", "unit_ids": []}}'
    )
    monkeypatch.setattr(cc.llm_service, "generate_response", AsyncMock(return_value=reply))
    _no_db(monkeypatch)
    out, fu, _ = await cc.maybe_compress("q", units, "he", cats, "g1")
    assert [u.unit_id for u in out] == ["u20"]
    assert fu["unit_ids"] == ["u21", "u22", "u23"]


@pytest.mark.asyncio
async def test_invalid_declared_category_skips_rule1_not_the_answer(monkeypatch):
    units, cats = _mixed()
    reply = '{"category": "no-such-category", "unit_ids": ["u0", "u16"]}'
    monkeypatch.setattr(cc.llm_service, "generate_response", AsyncMock(return_value=reply))
    _no_db(monkeypatch)
    out, _, compressed = await cc.maybe_compress("q", units, "he", cats, "g1")
    assert compressed
    # fail-open: with no valid declaration nothing is demoted by category
    assert [u.unit_id for u in out] == ["u0", "u16"]


@pytest.mark.asyncio
async def test_rules_emptying_the_core_fails_open(monkeypatch):
    units, cats = _mixed()
    reply = '{"category": "children", "unit_ids": ["u16", "u17"]}'
    monkeypatch.setattr(cc.llm_service, "generate_response", AsyncMock(return_value=reply))
    _no_db(monkeypatch)
    out, fu, compressed = await cc.maybe_compress("q", units, "he", cats, "g1")
    assert out is units and compressed is False  # never serve nothing


@pytest.mark.asyncio
async def test_no_annotations_means_no_category_demotion(monkeypatch):
    # callers without category data (or legacy rows mapping to None) keep
    # the pre-redesign behaviour exactly
    reply = '{"category": "childhood", "unit_ids": ["u2", "u0", "u5"], "follow_up": {"question": "עוד?", "unit_ids": ["u7"]}}'
    monkeypatch.setattr(cc.llm_service, "generate_response", AsyncMock(return_value=reply))
    out, fu, compressed = await cc.maybe_compress("q", _big(), "he")
    assert compressed and [u.unit_id for u in out] == ["u2", "u0", "u5"]  # model order
    assert fu["unit_ids"] == ["u7"]


@pytest.mark.asyncio
async def test_below_threshold_touches_neither_llm_nor_db(monkeypatch):
    _explosive(monkeypatch)

    async def boom_db(group_id):  # pragma: no cover - reaching it IS the failure
        raise AssertionError("DB touched below the threshold")

    monkeypatch.setattr(cc, "_close_family_names", boom_db)
    units = [_cu(f"u{i}", "s", 10) for i in range(14)]  # 140s < 150s
    out, fu, compressed = await cc.maybe_compress("q", units, "he", {"s": "childhood"}, "g1")
    assert out is units and compressed is False


def test_mentions_token_bounded_hebrew_prefixes():
    assert cc._mentions("נסענו לאברהם הביתה", "אברהם")
    assert cc._mentions("אחי ויוסי באו", "יוסי")
    assert not cc._mentions("האברבנאלים הגיעו", "אבר")
    assert not cc._mentions("שרון ורני הלכו", "רן")  # inside another word
    assert not cc._mentions("", "אברהם") and not cc._mentions("טקסט", "")


# ── the size budget (2026-08-31) ────────────────────────────────────────────
# Rules decide eligibility; CORE_COMPRESSION_TARGET_SEC decides how much
# plays. Enforced in code after rule enforcement, truncating at a unit
# boundary; the cut remainder LEADS the offer.


def _iu(uid, seg, idx, start, end, text=None):
    return SimpleNamespace(
        unit_id=uid, segment_id=seg, index=idx,
        start_sec=float(start), end_sec=float(end), text=text or f"t-{uid}",
    )


def _keep_all(monkeypatch, units, question="עוד?"):
    ids = ", ".join(f'"{u.unit_id}"' for u in units)
    reply = (
        '{"category": "childhood", "unit_ids": [' + ids + '],'
        ' "follow_up": {"question": "' + question + '", "unit_ids": []}}'
    )
    monkeypatch.setattr(cc.llm_service, "generate_response", AsyncMock(return_value=reply))


@pytest.mark.asyncio
async def test_budget_truncates_at_unit_boundary(monkeypatch):
    # 30 x 10s non-contiguous units, model keeps all; budget 90 keeps the
    # crossing unit whole -> 9 units, the other 21 LEAD the offer in order
    units = [_iu(f"u{i}", "s", i * 2, 0, 10) for i in range(30)]  # 300s
    _keep_all(monkeypatch, units)
    _no_db(monkeypatch)
    out, fu, compressed = await cc.maybe_compress(
        "q", units, "he", {"s": "childhood"}, "g1"
    )
    assert compressed
    assert [u.unit_id for u in out] == [f"u{i}" for i in range(9)]
    assert fu["unit_ids"] == [f"u{i}" for i in range(9, 30)]


@pytest.mark.asyncio
async def test_budget_counts_intra_run_pauses(monkeypatch):
    # contiguous units: the pause between them plays, so it counts toward
    # the budget exactly as resolve_units_to_clips will play it
    units = [
        _iu("u1", "s", 1, 0, 40),    # 40s speech
        _iu("u2", "s", 2, 50, 90),   # +50s (10s pause + 40s speech) -> span 90
        _iu("u3", "s", 3, 90, 100),  # would start beyond the budget
        _iu("u4", "s", 4, 100, 200),
    ]
    _keep_all(monkeypatch, units)
    _no_db(monkeypatch)
    out, fu, _ = await cc.maybe_compress("q", units, "he", {"s": "childhood"}, "g1")
    assert [u.unit_id for u in out] == ["u1", "u2"]
    assert fu["unit_ids"] == ["u3", "u4"]


@pytest.mark.asyncio
async def test_budget_crossing_unit_kept_whole_and_never_empty(monkeypatch):
    # a single unit longer than the budget is still served in full
    units = [_iu("u1", "s", 1, 0, 200), _iu("u2", "s", 5, 300, 400)]
    _keep_all(monkeypatch, units)
    _no_db(monkeypatch)
    out, fu, compressed = await cc.maybe_compress(
        "q", units, "he", {"s": "childhood"}, "g1"
    )
    assert compressed and [u.unit_id for u in out] == ["u1"]
    assert fu["unit_ids"] == ["u2"]


@pytest.mark.asyncio
async def test_truncated_lead_offer_before_demoted_and_model_picks(monkeypatch):
    # childhood units exhaust the budget; a children-category unit the model
    # kept is rule-demoted; model also offers one id. Offer order:
    # budget-cut continuation first, then demoted, then the model's pick.
    units = [_iu(f"u{i}", "s-child", i * 2, 0, 30) for i in range(4)]  # 120s
    units.append(_iu("u10", "s-fam", 100, 0, 30))
    units.append(_iu("u11", "s-fam", 102, 0, 30))
    reply = (
        '{"category": "childhood",'
        ' "unit_ids": ["u0", "u1", "u2", "u3", "u10"],'
        ' "follow_up": {"question": "עוד?", "unit_ids": ["u11"]}}'
    )
    monkeypatch.setattr(cc.llm_service, "generate_response", AsyncMock(return_value=reply))
    _no_db(monkeypatch)
    out, fu, _ = await cc.maybe_compress(
        "q", units, "he", {"s-child": "childhood", "s-fam": "children"}, "g1"
    )
    # budget 90: u0..u2 span 90, u3 cut; u10 demoted by category
    assert [u.unit_id for u in out] == ["u0", "u1", "u2"]
    assert fu["unit_ids"] == ["u3", "u10", "u11"]


@pytest.mark.asyncio
async def test_zero_budget_disables_truncation(monkeypatch):
    monkeypatch.setattr(settings, "CORE_COMPRESSION_TARGET_SEC", 0)
    units = [_iu(f"u{i}", "s", i * 2, 0, 20) for i in range(10)]  # 200s
    _keep_all(monkeypatch, units)
    _no_db(monkeypatch)
    out, _, compressed = await cc.maybe_compress(
        "q", units, "he", {"s": "childhood"}, "g1"
    )
    assert compressed and len(out) == 10


# ── output-cap headroom (2026-08-31, the 240-unit truncation) ───────────────


@pytest.mark.asyncio
async def test_compression_call_gets_its_own_output_cap(monkeypatch):
    """The 240-unit live failure: under the global 2000-token cap the reply
    (kept-list + offer-list + thinking) truncated mid-JSON and fail-open
    served a 763s core. The call must carry CORE_COMPRESSION_MAX_TOKENS."""
    seen = {}

    async def fake(messages, system_prompt=None, **kw):
        seen.update(kw)
        ids = ", ".join(f'"u{i}"' for i in range(0, 60))
        return '{"category": "childhood", "unit_ids": [' + ids + ']}'

    monkeypatch.setattr(cc.llm_service, "generate_response", fake)
    _no_db(monkeypatch)
    units = [_cu(f"u{i}", "s", 10) for i in range(240)]  # 2400s, way over
    out, fu, compressed = await cc.maybe_compress(
        "q", units, "he", {"s": "childhood"}, "g1"
    )
    assert compressed
    assert seen.get("max_output_tokens") == settings.CORE_COMPRESSION_MAX_TOKENS
    assert settings.CORE_COMPRESSION_MAX_TOKENS >= 8192


@pytest.mark.asyncio
async def test_truncated_mid_json_reply_still_fails_open(monkeypatch):
    """The safety net stays: a reply cut mid-JSON (the exact live shape —
    an unterminated id array) parses to nothing and serves the ORIGINAL
    selection rather than an error or an empty answer."""
    truncated = '{"category": "childhood", "unit_ids": ["u1", "u2", "u3", "u4'
    monkeypatch.setattr(
        cc.llm_service, "generate_response", AsyncMock(return_value=truncated)
    )
    _no_db(monkeypatch)
    units = [_cu(f"u{i}", "s", 10) for i in range(240)]
    out, fu, compressed = await cc.maybe_compress(
        "q", units, "he", {"s": "childhood"}, "g1"
    )
    assert out is units and fu is None and compressed is False



# ── diversity-aware budget fill (2026-08-31.2) ──────────────────────────────


def _keep_ids(monkeypatch, ids, question="עוד?"):
    id_list = ", ".join('"' + i + '"' for i in ids)
    reply = (
        '{"category": "childhood", "unit_ids": [' + id_list + '],'
        ' "follow_up": {"question": "' + question + '", "unit_ids": []}}'
    )
    monkeypatch.setattr(
        cc.llm_service, "generate_response", AsyncMock(return_value=reply)
    )


_THREE_CATS = {"rec-a": "childhood", "rec-b": "childhood", "rec-c": "childhood"}


@pytest.mark.asyncio
async def test_multi_recording_core_represents_every_recording(monkeypatch):
    """The live failure: 3 selected recordings, archive-order tail-cut kept
    the first whole and dropped the other two. Now each gets ~budget/R."""
    units = (
        [_iu("a" + str(i), "rec-a", i, i * 10, i * 10 + 10) for i in range(10)]
        + [_iu("b" + str(i), "rec-b", 100 + i, i * 10, i * 10 + 10) for i in range(10)]
        + [_iu("c" + str(i), "rec-c", 200 + i, i * 10, i * 10 + 10) for i in range(10)]
    )
    _keep_ids(monkeypatch, [u.unit_id for u in units])
    _no_db(monkeypatch)
    out, fu, compressed = await cc.maybe_compress("q", units, "he", _THREE_CATS, "g1")
    assert compressed
    per = {}
    for u in out:
        per.setdefault(u.segment_id, []).append(u)
    assert set(per) == {"rec-a", "rec-b", "rec-c"}
    assert [len(v) for v in per.values()] == [3, 3, 3]  # ~30s each
    assert fu is not None and len(fu["unit_ids"]) == 21  # cut tails lead offer


@pytest.mark.asyncio
async def test_single_recording_identical_to_old_prefix_walk(monkeypatch):
    units = [_iu("u" + str(i), "s", i * 2, 0, 10) for i in range(30)]  # 300s
    _keep_ids(monkeypatch, [u.unit_id for u in units])
    _no_db(monkeypatch)
    out, fu, _ = await cc.maybe_compress("q", units, "he", {"s": "childhood"}, "g1")
    assert [u.unit_id for u in out] == ["u" + str(i) for i in range(9)]
    assert fu["unit_ids"] == ["u" + str(i) for i in range(9, 30)]


@pytest.mark.asyncio
async def test_budget_smaller_than_recording_count(monkeypatch):
    """share < one unit: each recording still contributes its first unit -
    never empty, never single-recording when the input was not."""
    units = [
        _iu("a1", "rec-a", 1, 0, 40), _iu("b1", "rec-b", 10, 0, 40),
        _iu("c1", "rec-c", 20, 0, 40), _iu("c2", "rec-c", 21, 40, 80),
    ]
    _keep_ids(monkeypatch, ["a1", "b1", "c1", "c2"])
    _no_db(monkeypatch)
    monkeypatch.setattr(settings, "CORE_COMPRESSION_THRESHOLD_SEC", 100)
    out, fu, _ = await cc.maybe_compress("q", units, "he", _THREE_CATS, "g1")
    assert [u.unit_id for u in out] == ["a1", "b1", "c1"]
    assert fu["unit_ids"] == ["c2"]


@pytest.mark.asyncio
async def test_model_stitch_order_survives_fill(monkeypatch):
    """The model's reply order (rec-b first) is the composed answer; the
    old code silently flattened it to archive order."""
    units = [
        _iu("a1", "rec-a", 1, 0, 30), _iu("a2", "rec-a", 2, 30, 60),
        _iu("a3", "rec-a", 3, 60, 90),
        _iu("b1", "rec-b", 10, 0, 30), _iu("b2", "rec-b", 11, 30, 60),
        _iu("b3", "rec-b", 12, 60, 90),
    ]
    _keep_ids(monkeypatch, ["b1", "b2", "b3", "a1", "a2", "a3"])  # rec-b leads
    _no_db(monkeypatch)
    monkeypatch.setattr(settings, "CORE_COMPRESSION_THRESHOLD_SEC", 100)
    out, fu, _ = await cc.maybe_compress(
        "q", units, "he", {"rec-a": "childhood", "rec-b": "childhood"}, "g1"
    )
    # share 45s -> b1+b2 (crossing plays whole), a1+a2; b3/a3 cut; the
    # model's rec-b-first order survives (old code flattened to archive order)
    assert [u.unit_id for u in out] == ["b1", "b2", "a1", "a2"]
    assert fu["unit_ids"] == ["b3", "a3"]


@pytest.mark.asyncio
async def test_pass2_remainder_extends_when_one_recording_is_short(monkeypatch):
    units = [
        _iu("a1", "rec-a", 1, 0, 20),  # rec-a has only 20s
        _iu("b1", "rec-b", 10, 0, 30), _iu("b2", "rec-b", 11, 30, 60),
        _iu("b3", "rec-b", 12, 60, 90), _iu("b4", "rec-b", 13, 90, 200),
    ]
    _keep_ids(monkeypatch, ["a1", "b1", "b2", "b3", "b4"])
    _no_db(monkeypatch)
    out, fu, _ = await cc.maybe_compress(
        "q", units, "he", {"rec-a": "childhood", "rec-b": "childhood"}, "g1"
    )
    # pass1: a1 (20s) + b1,b2 (60s; share 45 -> crossing b2 whole) = 80s
    # pass2: remainder -> b3 (crossing, whole); b4 cut
    assert [u.unit_id for u in out] == ["a1", "b1", "b2", "b3"]
    assert fu["unit_ids"] == ["b4"]
