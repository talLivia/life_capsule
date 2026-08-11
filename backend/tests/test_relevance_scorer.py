"""
Tests for relevance_scorer.py (Prompt 7). embeddings/entity_store/
cache_service are mocked — this suite verifies the scoring/normalization/
threshold logic itself, not live embedding-model or Neo4j behavior.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InterviewSession, RawSegment, TranscriptChunk
from app.services import relevance_scorer as rs

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def relevance_session_factory(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(rs, "AsyncSessionLocal", factory)
    return factory


@pytest.fixture
async def scored_segments(db_session, test_user, relevance_session_factory):
    """Two candidate segments with distinct importance/embeddings, so
    normalization across the pair is meaningful."""
    session = InterviewSession(user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)

    high = RawSegment(
        interview_session_id=session.id,
        question_asked="Q1",
        question_index=0,
        transcript="A highly important, highly relevant memory.",
        importance_score=9.0,
        embedding=[1.0, 0.0, 0.0],
        status="ready",
    )
    low = RawSegment(
        interview_session_id=session.id,
        question_asked="Q2",
        question_index=1,
        transcript="A mundane, unrelated memory.",
        importance_score=1.0,
        embedding=[0.0, 1.0, 0.0],
        status="ready",
    )
    db_session.add(high)
    db_session.add(low)
    await db_session.commit()
    await db_session.refresh(high)
    await db_session.refresh(low)
    return {"high": high, "low": low}


# ── _min_max_normalize ───────────────────────────────────────────────────────


def test_min_max_normalize_spreads_values():
    assert rs._min_max_normalize([1, 2, 3]) == [0.0, 0.5, 1.0]


def test_min_max_normalize_no_spread_with_signal():
    assert rs._min_max_normalize([5, 5, 5]) == [1.0, 1.0, 1.0]


def test_min_max_normalize_no_spread_zero_signal():
    assert rs._min_max_normalize([0, 0, 0]) == [0.0, 0.0, 0.0]


def test_min_max_normalize_empty():
    assert rs._min_max_normalize([]) == []


# ── _recency_raw_score ───────────────────────────────────────────────────────


async def test_recency_raw_score_zero_when_never_mentioned(monkeypatch):
    monkeypatch.setattr(rs.cache_service, "get_entity_last_mentioned", AsyncMock(return_value={}))
    assert await rs._recency_raw_score(["Gila"], "sess-1") == 0.0


async def test_recency_raw_score_decays_with_time(monkeypatch):
    monkeypatch.setattr(rs.time, "time", lambda: 1000.0)
    # Mentioned 0 minutes ago -> decay factor 1.0 (exp(0))
    monkeypatch.setattr(
        rs.cache_service, "get_entity_last_mentioned", AsyncMock(return_value={"Gila": 1000.0})
    )
    assert await rs._recency_raw_score(["Gila"], "sess-1") == pytest.approx(1.0)

    # Mentioned 10 minutes ago -> meaningfully decayed, but still > 0
    monkeypatch.setattr(
        rs.cache_service, "get_entity_last_mentioned", AsyncMock(return_value={"Gila": 400.0})
    )
    score = await rs._recency_raw_score(["Gila"], "sess-1")
    assert 0.0 < score < 1.0


async def test_recency_raw_score_uses_most_recent_of_multiple_entities(monkeypatch):
    monkeypatch.setattr(rs.time, "time", lambda: 1000.0)
    monkeypatch.setattr(
        rs.cache_service,
        "get_entity_last_mentioned",
        AsyncMock(return_value={"Old": 100.0, "Recent": 1000.0}),
    )
    assert await rs._recency_raw_score(["Old", "Recent"], "sess-1") == pytest.approx(1.0)


async def test_recency_raw_score_no_entities():
    assert await rs._recency_raw_score([], "sess-1") == 0.0


# ── _embed_question ──────────────────────────────────────────────────────────


async def test_embed_question_returns_vector(monkeypatch):
    monkeypatch.setattr(rs.embeddings, "embed_text", AsyncMock(return_value=[0.1, 0.2]))
    assert await rs._embed_question("hi") == [0.1, 0.2]


async def test_embed_question_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(rs.embeddings, "embed_text", AsyncMock(side_effect=RuntimeError("down")))
    assert await rs._embed_question("hi") is None


# ── score_candidates ─────────────────────────────────────────────────────────


async def test_score_candidates_empty_input_returns_empty():
    assert await rs.score_candidates("q", [], "sess-1", "g1") == []


async def test_score_candidates_ranks_and_filters_by_threshold(
    scored_segments, relevance_session_factory, monkeypatch
):
    high, low = scored_segments["high"], scored_segments["low"]

    monkeypatch.setattr(rs, "_embed_question", AsyncMock(return_value=[1.0, 0.0, 0.0]))
    monkeypatch.setattr(rs.entity_store, "get_entity_names_for_segments", AsyncMock(return_value={}))
    monkeypatch.setattr(rs.cache_service, "get_entity_last_mentioned", AsyncMock(return_value={}))

    result = await rs.score_candidates("Tell me more", [high.id, low.id], "sess-1", "g1")

    # high: importance=9 (norm 1.0), relevance=cos([1,0,0],[1,0,0])=1.0 (norm 1.0) -> combined >= 2.0
    # low: importance=1 (norm 0.0), relevance=cos([1,0,0],[0,1,0])=0.0 (norm 0.0) -> combined = 0.0
    assert [s.segment_id for s in result] == [high.id]
    assert result[0].score >= rs.RELEVANCE_THRESHOLD


async def test_score_candidates_filter_by_threshold_false_returns_everyone(
    scored_segments, relevance_session_factory, monkeypatch
):
    """Prompt 10's QA harness needs to see WHY a candidate didn't bridge, not
    just the ones that did — filter_by_threshold=False must return both
    segments, still sorted by score, with the below-threshold one included."""
    high, low = scored_segments["high"], scored_segments["low"]

    monkeypatch.setattr(rs, "_embed_question", AsyncMock(return_value=[1.0, 0.0, 0.0]))
    monkeypatch.setattr(rs.entity_store, "get_entity_names_for_segments", AsyncMock(return_value={}))
    monkeypatch.setattr(rs.cache_service, "get_entity_last_mentioned", AsyncMock(return_value={}))

    result = await rs.score_candidates(
        "Tell me more", [high.id, low.id], "sess-1", "g1", filter_by_threshold=False
    )

    assert [s.segment_id for s in result] == [high.id, low.id]
    assert result[0].score >= rs.RELEVANCE_THRESHOLD
    assert result[1].score < rs.RELEVANCE_THRESHOLD


async def test_score_candidates_skips_missing_segment(
    scored_segments, relevance_session_factory, monkeypatch
):
    monkeypatch.setattr(rs, "_embed_question", AsyncMock(return_value=[1.0, 0.0, 0.0]))
    monkeypatch.setattr(rs.entity_store, "get_entity_names_for_segments", AsyncMock(return_value={}))
    monkeypatch.setattr(rs.cache_service, "get_entity_last_mentioned", AsyncMock(return_value={}))

    result = await rs.score_candidates(
        "q", [scored_segments["high"].id, "does-not-exist"], "sess-1", "g1"
    )
    assert [s.segment_id for s in result] == [scored_segments["high"].id]


async def test_score_candidates_tolerates_entity_fetch_failure(
    scored_segments, relevance_session_factory, monkeypatch
):
    monkeypatch.setattr(rs, "_embed_question", AsyncMock(return_value=[1.0, 0.0, 0.0]))
    monkeypatch.setattr(
        rs.entity_store,
        "get_entity_names_for_segments",
        AsyncMock(side_effect=RuntimeError("postgres down")),
    )
    monkeypatch.setattr(rs.cache_service, "get_entity_last_mentioned", AsyncMock(return_value={}))

    # Should not raise despite the entity lookup failing: entity names feed
    # only the recency term, so losing them costs one signal, not the score.
    result = await rs.score_candidates(
        "q", [scored_segments["high"].id, scored_segments["low"].id], "sess-1", "g1"
    )
    assert isinstance(result, list)


async def test_score_candidates_degrades_relevance_when_embedding_fails(
    scored_segments, relevance_session_factory, monkeypatch
):
    monkeypatch.setattr(rs.embeddings, "embed_text", AsyncMock(side_effect=RuntimeError("down")))
    monkeypatch.setattr(rs.entity_store, "get_entity_names_for_segments", AsyncMock(return_value={}))
    monkeypatch.setattr(rs.cache_service, "get_entity_last_mentioned", AsyncMock(return_value={}))

    # Both candidates get relevance=0 (no question embedding); only
    # importance differs, so "high" (importance=9) should still win out.
    result = await rs.score_candidates(
        "q", [scored_segments["high"].id, scored_segments["low"].id], "sess-1", "g1"
    )
    assert scored_segments["high"].id in [s.segment_id for s in result]


