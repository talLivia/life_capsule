"""Pre-filter (PREFILTER_PLAN): inert-by-construction, budget admission,
force-includes, per-conversation pinning + expansion, and the exhaustion
guard. All ranking is mocked — no API calls."""

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import embeddings, prefilter


def _rec(seg_id, chars=1000, embedding="auto"):
    emb = [1.0, 0.0] if embedding == "auto" else embedding
    return SimpleNamespace(
        segment=SimpleNamespace(id=seg_id, embedding=emb),
        chunks=[SimpleNamespace(text="א" * chars)],
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    prefilter.reset_pins()
    monkeypatch.setattr(settings, "PREFILTER", "on")
    monkeypatch.setattr(settings, "PREFILTER_CHAR_BUDGET", 3000)
    yield
    prefilter.reset_pins()


def _mock_rank(monkeypatch, q_vec=(1.0, 0.0)):
    async def fake_embed(text):
        return list(q_vec)

    monkeypatch.setattr(embeddings, "embed_text", fake_embed)


@pytest.mark.asyncio
async def test_inert_when_off_or_under_budget(monkeypatch):
    _mock_rank(monkeypatch)
    archive = [_rec("a"), _rec("b")]  # 2000 chars < 3000 budget
    assert await prefilter.apply("q", "s1", archive, {}, set(), (1,)) is None
    monkeypatch.setattr(settings, "PREFILTER", "off")
    big = [_rec(f"r{i}") for i in range(10)]  # 10000 chars, but toggle off
    assert await prefilter.apply("q", "s1", big, {}, set(), (1,)) is None


@pytest.mark.asyncio
async def test_budget_admission_prefers_similar_recordings(monkeypatch):
    _mock_rank(monkeypatch, (1.0, 0.0))
    # similar (aligned vector) vs dissimilar (orthogonal)
    archive = [
        _rec("far1", embedding=[0.0, 1.0]),
        _rec("near1", embedding=[1.0, 0.0]),
        _rec("far2", embedding=[0.0, 1.0]),
        _rec("near2", embedding=[0.9, 0.1]),
        _rec("far3", embedding=[0.0, 1.0]),
    ]  # 5000 chars > 3000 budget -> 3 admitted
    pf = await prefilter.apply("q", "s1", archive, {}, set(), (1,))
    assert pf is not None and not pf.low_confidence
    assert {"near1", "near2"} <= set(pf.admitted)
    assert pf.excluded == 2


@pytest.mark.asyncio
async def test_force_includes_survive_low_rank(monkeypatch):
    _mock_rank(monkeypatch, (1.0, 0.0))
    archive = [
        _rec("noemb", embedding=None),  # no embedding -> forced
        _rec("tagged", embedding=[0.0, 1.0]),  # same-name tag -> forced
        _rec("shownr", embedding=[0.0, 1.0]),  # shown-state -> forced
        _rec("near", embedding=[1.0, 0.0]),
        _rec("far", embedding=[0.0, 1.0]),
    ]
    pf = await prefilter.apply(
        "q", "s1", archive,
        name_tags={"tagged": ["tag"]},
        shown_keys={"shownr:1.00"},
        version=(1,),
    )
    assert {"noemb", "tagged", "shownr"} <= set(pf.admitted)


@pytest.mark.asyncio
async def test_pinning_is_stable_and_expansion_grows(monkeypatch):
    _mock_rank(monkeypatch, (1.0, 0.0))
    monkeypatch.setattr(settings, "PREFILTER_CHAR_BUDGET", 2500)
    archive = [
        _rec("near", embedding=[1.0, 0.0]),
        _rec("mid", embedding=[0.7, 0.7]),
        _rec("far", embedding=[0.0, 1.0]),
        _rec("far2", embedding=[0.0, 1.0]),
    ]
    pf1 = await prefilter.apply("q1", "s1", archive, {}, set(), (1,))
    assert "far" not in pf1.admitted
    # same session, same question profile: pinned set unchanged
    pf2 = await prefilter.apply("q2", "s1", archive, {}, set(), (1,))
    assert pf2.admitted == pf1.admitted and not pf2.expanded
    assert pf2.set_hash == pf1.set_hash
    # question now about the far topic: set EXPANDS (grow-only)
    _mock_rank(monkeypatch, (0.0, 1.0))
    pf3 = await prefilter.apply("q3", "s1", archive, {}, set(), (1,))
    assert pf3.expanded and "far" in pf3.admitted
    assert set(pf1.admitted) <= set(pf3.admitted)
    assert pf3.set_hash != pf1.set_hash
    # a different session pins its own set
    pf4 = await prefilter.apply("q1", "s2", archive, {}, set(), (1,))
    assert pf4.admitted != pf3.admitted or pf4.set_hash == pf3.set_hash


@pytest.mark.asyncio
async def test_version_change_repins(monkeypatch):
    _mock_rank(monkeypatch, (1.0, 0.0))
    archive = [_rec(f"r{i}", embedding=[1.0, 0.0]) for i in range(5)]
    pf1 = await prefilter.apply("q", "s1", archive, {}, set(), (1,))
    pf2 = await prefilter.apply("q", "s1", archive, {}, set(), (2,))
    assert pf2 is not None  # re-pinned under the new version, no crash


@pytest.mark.asyncio
async def test_ranking_failure_is_low_confidence_archive_order(monkeypatch):
    async def broken(text):
        raise RuntimeError("embedding down")

    monkeypatch.setattr(embeddings, "embed_text", broken)
    archive = [_rec(f"r{i}") for i in range(5)]
    pf = await prefilter.apply("q", "s1", archive, {}, set(), (1,))
    assert pf.low_confidence
    # archive-order fallback: earliest recordings admitted up to budget
    assert {"r0", "r1", "r2"} == set(pf.admitted)


def test_covers_entity_guard():
    assert prefilter.covers_entity(None, ["a", "b"])  # unfiltered: fine
    ok = prefilter.PrefilterResult(
        admitted=frozenset({"a", "b"}), excluded=1,
        low_confidence=False, expanded=False, set_hash="x")
    assert prefilter.covers_entity(ok, ["a", "b"])
    assert not prefilter.covers_entity(ok, ["a", "c"])  # c excluded -> no claim
    lc = prefilter.PrefilterResult(
        admitted=frozenset({"a", "b"}), excluded=0,
        low_confidence=True, expanded=False, set_hash="x")
    assert not prefilter.covers_entity(lc, ["a"])  # low confidence -> no claim


def test_prefilter_default_is_on_since_bulk_import_launch():
    """Flipped 2026-08-27 (BULK_IMPORT_PLAN §7) after both proofs passed.
    Safe fleet-wide because activation self-selects per producer via the
    per-request budget check — under-budget archives render byte-identically
    (the inertness proof pins that)."""
    assert settings.model_fields["PREFILTER"].default == "on"


@pytest.mark.asyncio
async def test_pin_cap_evicts_oldest(monkeypatch):
    _mock_rank(monkeypatch, (1.0, 0.0))
    monkeypatch.setattr(prefilter, "_PIN_CAP", 3)
    archive = [_rec(f"r{i}") for i in range(5)]
    for n in range(5):
        await prefilter.apply("q", f"sess-{n}", archive, {}, set(), (1,))
    assert len(prefilter._PINNED) == 3
    assert "sess-0" not in prefilter._PINNED and "sess-4" in prefilter._PINNED
