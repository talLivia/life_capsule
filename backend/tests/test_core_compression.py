"""Core compression: the deterministic gate, the never-invent contract,
and fail-open on every failure shape. All LLM calls mocked."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import core_compression as cc


def _u(uid, dur=10.0):
    return SimpleNamespace(unit_id=uid, start_sec=0.0, end_sec=dur, text=f"t-{uid}")


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
    assert [u.unit_id for u in out] == ["u0", "u2", "u5"]  # archive order kept
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
