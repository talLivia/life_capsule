"""Semantic answer cache (answer_cache.py): toggle inertness, the
fresh-conversation gate, the same-name-ambiguity bypass (the highest-risk
false-positive corner), version fingerprint scoping, threshold behaviour,
and fail-open resolution. DB is a per-test in-memory SQLite; embeddings are
mocked — no network anywhere."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
from app.services import answer_cache as ac


def _u(uid, seg="s1", start=0.0, dur=10.0, text=None):
    return SimpleNamespace(
        unit_id=uid, segment_id=seg, index=0,
        start_sec=float(start), end_sec=float(start) + dur, text=text or f"t-{uid}",
    )


def _tag(*surfaces):
    return SimpleNamespace(surfaces=tuple(surfaces), label="x")


VERSION = ("seg-a", "2026-01-01")


@pytest.fixture
async def cache_db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(ac, "AsyncSessionLocal", factory)
    monkeypatch.setattr(settings, "ANSWER_CACHE", "on")
    yield factory
    await engine.dispose()


def _fix_embeddings(monkeypatch, table):
    async def embed(text):
        return table[text]

    def cos(a, b):
        # identical vectors -> 1.0, else a fixed sub-threshold value
        return 1.0 if a == b else 0.5

    monkeypatch.setattr(ac.embeddings, "embed_text", embed)
    monkeypatch.setattr(ac.embeddings, "cosine_similarity", cos)


# ── toggle off is INERT ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_toggle_off_touches_nothing(monkeypatch):
    monkeypatch.setattr(settings, "ANSWER_CACHE", "off")

    async def boom(*a, **k):  # pragma: no cover - reaching it IS the failure
        raise AssertionError("cache touched infrastructure while off")

    monkeypatch.setattr(ac.embeddings, "embed_text", boom)
    monkeypatch.setattr(ac, "AsyncSessionLocal", boom)
    emb, hit = await ac.try_lookup("q", "p1", VERSION, [_u("u1")], {}, set(), [])
    assert emb is None and hit is None
    await ac.store("q", [1.0], "p1", VERSION, [_u("u1")], None)  # no raise


# ── the fresh-conversation gate ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_fresh_conversation_never_looks_up(monkeypatch, cache_db):
    async def boom(*a, **k):  # pragma: no cover
        raise AssertionError("embedded despite non-fresh conversation")

    monkeypatch.setattr(ac.embeddings, "embed_text", boom)
    emb, hit = await ac.try_lookup(
        "q", "p1", VERSION, [_u("u1")], {}, {"s1:0.0"}, []
    )
    assert emb is None and hit is None
    emb, hit = await ac.try_lookup(
        "q", "p1", VERSION, [_u("u1")], {}, set(), [("user", "prev turn")]
    )
    assert emb is None and hit is None


# ── the same-name-ambiguity bypass (highest-risk corner) ────────────────────


@pytest.mark.asyncio
async def test_ambiguous_name_question_bypasses_cache_read_and_write(
    monkeypatch, cache_db
):
    tags = {"seg-x": [_tag("אמנון", "אמנון נחום")]}
    _fix_embeddings(monkeypatch, {"ספר לי על אמנון החבר": [1.0, 0.0]})
    units = [_u("u1")]
    # store a legitimate-looking entry under a NON-ambiguous phrasing first
    await ac.store(
        "ספר לי על אמנון החבר", [1.0, 0.0], "p1", VERSION, units, None
    )
    # the ambiguous question must not even embed, let alone hit
    async def boom(text):  # pragma: no cover
        raise AssertionError("embedded an ambiguous-name question")

    monkeypatch.setattr(ac.embeddings, "embed_text", boom)
    emb, hit = await ac.try_lookup(
        "ספר לי על אמנון", "p1", VERSION, units, tags, set(), []
    )
    assert emb is None and hit is None
    # prefixed mention is still caught (ואמנון), unrelated names are not
    assert ac.question_names_ambiguous_person("מה עשה ואמנון אז?", tags)
    assert not ac.question_names_ambiguous_person("ספר לי על אילנה", tags)
    # substring inside a longer word does NOT trigger the bypass
    assert not ac.question_names_ambiguous_person("מה זה אמנונים?", tags)


# ── hit/miss mechanics ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_then_semantic_hit_preserves_units_and_offer(
    monkeypatch, cache_db
):
    units = [_u("u1", "s1", 0), _u("u2", "s1", 10), _u("u3", "s2", 0)]
    _fix_embeddings(
        monkeypatch,
        {"ספר לי על המשפחה שלך": [1.0], "תספר על המשפחה": [1.0]},
    )
    await ac.store(
        "ספר לי על המשפחה שלך", [1.0], "p1", VERSION,
        [units[0], units[2]],
        {"question": "רוצה עוד?", "unit_keys": ["s1:10.0"]},
        source="prewarm",
    )
    emb, hit = await ac.try_lookup(
        "תספר על המשפחה", "p1", VERSION, units, {}, set(), []
    )
    assert hit is not None
    assert [u.unit_id for u in hit.units] == ["u1", "u3"]  # stored order
    assert hit.raw_follow_up == {"question": "רוצה עוד?", "unit_ids": ["u2"]}
    assert hit.source == "prewarm" and hit.similarity == 1.0


@pytest.mark.asyncio
async def test_below_threshold_misses_and_returns_embedding(
    monkeypatch, cache_db
):
    units = [_u("u1")]
    _fix_embeddings(monkeypatch, {"שאלה א": [1.0], "שאלה ב": [2.0]})
    await ac.store("שאלה א", [1.0], "p1", VERSION, units, None)
    emb, hit = await ac.try_lookup("שאלה ב", "p1", VERSION, units, {}, set(), [])
    assert hit is None and emb == [2.0]  # miss hands back the embedding


@pytest.mark.asyncio
async def test_version_mismatch_is_a_miss(monkeypatch, cache_db):
    units = [_u("u1")]
    _fix_embeddings(monkeypatch, {"שאלה": [1.0]})
    await ac.store("שאלה", [1.0], "p1", ("old", "v"), units, None)
    emb, hit = await ac.try_lookup("שאלה", "p1", VERSION, units, {}, set(), [])
    assert hit is None


@pytest.mark.asyncio
async def test_unresolvable_stored_keys_fail_open_to_full_read(
    monkeypatch, cache_db
):
    _fix_embeddings(monkeypatch, {"שאלה": [1.0]})
    await ac.store("שאלה", [1.0], "p1", VERSION, [_u("uX", "gone-seg", 99)], None)
    # archive re-ingested: the stored key no longer resolves
    current = [_u("u1", "s1", 0)]
    emb, hit = await ac.try_lookup("שאלה", "p1", VERSION, current, {}, set(), [])
    assert hit is None and emb == [1.0]


@pytest.mark.asyncio
async def test_store_gates(monkeypatch, cache_db):
    _fix_embeddings(monkeypatch, {})
    # no embedding (lookup gates said uncacheable) -> nothing stored
    await ac.store("שאלה", None, "p1", VERSION, [_u("u1")], None)
    # empty selection -> nothing stored
    await ac.store("שאלה", [1.0], "p1", VERSION, [], None)
    from sqlalchemy import func, select

    from app.models import AnswerCacheEntry

    async with cache_db() as db:
        n = (await db.execute(select(func.count(AnswerCacheEntry.id)))).scalar()
    assert n == 0


@pytest.mark.asyncio
async def test_restore_same_question_replaces_not_duplicates(
    monkeypatch, cache_db
):
    units = [_u("u1"), _u("u2", start=10)]
    _fix_embeddings(monkeypatch, {"שאלה": [1.0]})
    await ac.store("שאלה", [1.0], "p1", VERSION, [units[0]], None)
    await ac.store("שאלה", [1.0], "p1", VERSION, [units[1]], None)
    from sqlalchemy import func, select

    from app.models import AnswerCacheEntry

    async with cache_db() as db:
        n = (await db.execute(select(func.count(AnswerCacheEntry.id)))).scalar()
    assert n == 1
    emb, hit = await ac.try_lookup("שאלה", "p1", VERSION, units, {}, set(), [])
    assert hit is not None and [u.unit_id for u in hit.units] == ["u2"]


# ── speculative session-scoped entries (milestone 2) ────────────────────────


@pytest.mark.asyncio
async def test_speculative_exact_match_consumes_once(monkeypatch, cache_db):
    units = [_u("u1"), _u("u2", start=10)]
    await ac.store(
        "רוצה לשמוע על הילדים שלי?", [9.0], "p1", VERSION, [units[1]],
        None, source="speculative", session_id="sess-1",
    )
    # wrong session: not served
    hit = await ac.take_speculative(
        "רוצה לשמוע על הילדים שלי?", "p1", "sess-OTHER", VERSION, units
    )
    assert hit is None
    # near-match text: not served (exact match only, no similarity)
    hit = await ac.take_speculative(
        "רוצה לשמוע על הילדים?", "p1", "sess-1", VERSION, units
    )
    assert hit is None
    # exact: served once...
    hit = await ac.take_speculative(
        "רוצה לשמוע על הילדים שלי?", "p1", "sess-1", VERSION, units
    )
    assert hit is not None and [u.unit_id for u in hit.units] == ["u2"]
    assert hit.source == "speculative"
    # ...and consumed
    hit = await ac.take_speculative(
        "רוצה לשמוע על הילדים שלי?", "p1", "sess-1", VERSION, units
    )
    assert hit is None


@pytest.mark.asyncio
async def test_speculative_gates(monkeypatch, cache_db):
    units = [_u("u1")]
    await ac.store(
        "שאלה", [9.0], "p1", VERSION, units, None,
        source="speculative", session_id="sess-1",
    )
    # version mismatch: not served
    assert (
        await ac.take_speculative("שאלה", "p1", "sess-1", ("other", "v"), units)
    ) is None
    # toggle off: inert (no DB access)
    monkeypatch.setattr(settings, "ANSWER_CACHE", "off")

    async def boom(*a, **k):  # pragma: no cover
        raise AssertionError("speculative path touched DB while off")

    monkeypatch.setattr(ac, "AsyncSessionLocal", boom)
    assert (
        await ac.take_speculative("שאלה", "p1", "sess-1", VERSION, units)
    ) is None


@pytest.mark.asyncio
async def test_speculative_never_serves_globally(monkeypatch, cache_db):
    """A session-tagged entry must be invisible to the global semantic
    lookup — its answer was computed WITH conversation context."""
    units = [_u("u1")]
    _fix_embeddings(monkeypatch, {"שאלה": [9.0]})
    await ac.store(
        "שאלה", [9.0], "p1", VERSION, units, None,
        source="speculative", session_id="sess-1",
    )
    emb, hit = await ac.try_lookup("שאלה", "p1", VERSION, units, {}, set(), [])
    assert hit is None


# ── ingest pre-warm (milestone 3) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_prewarm_runs_canonical_questions_through_engine(
    monkeypatch, cache_db
):
    seen = []

    async def fake_select_units(q, gid, lang, sid):
        seen.append((q, gid, lang))
        return SimpleNamespace(
            read_failed=False, selected_units=[_u("u1")], clarify=None
        )

    import app.services.full_archive_retrieval as far

    monkeypatch.setattr(far, "select_units", fake_select_units)

    async def fake_get(self, model, pk):  # user lookup
        return SimpleNamespace(recording_language="he")

    from sqlalchemy.ext.asyncio import AsyncSession

    monkeypatch.setattr(AsyncSession, "get", fake_get)
    await ac.prewarm("p1")
    assert [q for q, _, _ in seen] == ac.CANONICAL_QUESTIONS_HE
    assert all(g == "p1" and l == "he" for _, g, l in seen)


@pytest.mark.asyncio
async def test_prewarm_inert_when_off_and_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "ANSWER_CACHE", "off")

    async def boom(*a, **k):  # pragma: no cover
        raise AssertionError("prewarm ran while off")

    import app.services.full_archive_retrieval as far

    monkeypatch.setattr(far, "select_units", boom)
    await ac.prewarm("p1")  # no raise, no engine call

    # and with the toggle ON, an engine explosion stays contained
    monkeypatch.setattr(settings, "ANSWER_CACHE", "on")

    async def db_boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ac, "AsyncSessionLocal", db_boom)
    await ac.prewarm("p1")  # swallowed, logged
