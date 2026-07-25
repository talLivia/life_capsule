"""
Tests for relevance_scorer.py (Prompt 7). embeddings/graph_memory/
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


@pytest.fixture
async def scored_chunks(db_session, test_user, relevance_session_factory):
    """Two candidate chunks whose PARENT segments have distinct importance
    scores, and distinct embeddings of their own — Prompt 12's chunk-level
    parallel to `scored_segments` above."""
    session = InterviewSession(user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)

    important_segment = RawSegment(
        interview_session_id=session.id,
        question_asked="Q1",
        question_index=0,
        transcript="A highly important memory, told across several phrases.",
        importance_score=9.0,
        status="ready",
    )
    mundane_segment = RawSegment(
        interview_session_id=session.id,
        question_asked="Q2",
        question_index=1,
        transcript="A mundane memory.",
        importance_score=1.0,
        status="ready",
    )
    db_session.add(important_segment)
    db_session.add(mundane_segment)
    await db_session.commit()
    await db_session.refresh(important_segment)
    await db_session.refresh(mundane_segment)

    high = TranscriptChunk(
        raw_segment_id=important_segment.id,
        start_sec=0.0,
        end_sec=2.0,
        text="A highly important, highly relevant phrase.",
        sequence_index=0,
        embedding=[1.0, 0.0, 0.0],
        mentioned_entities=["Gila"],
    )
    low = TranscriptChunk(
        raw_segment_id=mundane_segment.id,
        start_sec=0.0,
        end_sec=2.0,
        text="An unrelated phrase.",
        sequence_index=0,
        embedding=[0.0, 1.0, 0.0],
        mentioned_entities=[],
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
    monkeypatch.setattr(rs.graph_memory, "get_episode_entity_names", AsyncMock(return_value=[]))
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
    monkeypatch.setattr(rs.graph_memory, "get_episode_entity_names", AsyncMock(return_value=[]))
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
    monkeypatch.setattr(rs.graph_memory, "get_episode_entity_names", AsyncMock(return_value=[]))
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
        rs.graph_memory, "get_episode_entity_names", AsyncMock(side_effect=RuntimeError("neo4j down"))
    )
    monkeypatch.setattr(rs.cache_service, "get_entity_last_mentioned", AsyncMock(return_value={}))

    # Should not raise despite graph_memory failing for every candidate.
    result = await rs.score_candidates(
        "q", [scored_segments["high"].id, scored_segments["low"].id], "sess-1", "g1"
    )
    assert isinstance(result, list)


async def test_score_candidates_degrades_relevance_when_embedding_fails(
    scored_segments, relevance_session_factory, monkeypatch
):
    monkeypatch.setattr(rs.embeddings, "embed_text", AsyncMock(side_effect=RuntimeError("down")))
    monkeypatch.setattr(rs.graph_memory, "get_episode_entity_names", AsyncMock(return_value=[]))
    monkeypatch.setattr(rs.cache_service, "get_entity_last_mentioned", AsyncMock(return_value={}))

    # Both candidates get relevance=0 (no question embedding); only
    # importance differs, so "high" (importance=9) should still win out.
    result = await rs.score_candidates(
        "q", [scored_segments["high"].id, scored_segments["low"].id], "sess-1", "g1"
    )
    assert scored_segments["high"].id in [s.segment_id for s in result]


# ── Prompt 12: score_chunk_candidates (video-clip mode) ─────────────────────


async def test_score_chunk_candidates_empty_input_returns_empty():
    assert await rs.score_chunk_candidates("q", [], "sess-1") == []


async def test_score_chunk_candidates_ranks_and_filters_by_threshold(
    scored_chunks, relevance_session_factory, monkeypatch
):
    high, low = scored_chunks["high"], scored_chunks["low"]

    monkeypatch.setattr(rs, "_embed_question", AsyncMock(return_value=[1.0, 0.0, 0.0]))
    monkeypatch.setattr(rs.cache_service, "get_entity_last_mentioned", AsyncMock(return_value={}))

    result = await rs.score_chunk_candidates("Tell me more", [high.id, low.id], "sess-1")

    # high: importance inherited from its parent segment (9, norm 1.0),
    # relevance=cos([1,0,0],[1,0,0])=1.0 (norm 1.0) -> combined >= 2.0
    # low: importance from ITS parent (1, norm 0.0), relevance=0 (norm 0.0)
    assert [c.chunk_id for c in result] == [high.id]
    assert result[0].score >= rs.RELEVANCE_THRESHOLD
    assert result[0].raw_segment_id == high.raw_segment_id


async def test_score_chunk_candidates_importance_inherited_from_parent_segment(
    scored_chunks, relevance_session_factory, monkeypatch
):
    """The core Prompt 12 design point: TranscriptChunk has no importance of
    its own — every chunk from the same segment must share that segment's
    score, not default to 0 just because the chunk row itself lacks the
    field."""
    high, low = scored_chunks["high"], scored_chunks["low"]
    monkeypatch.setattr(rs, "_embed_question", AsyncMock(return_value=None))
    monkeypatch.setattr(rs.cache_service, "get_entity_last_mentioned", AsyncMock(return_value={}))

    result = await rs.score_chunk_candidates(
        "q", [high.id, low.id], "sess-1", filter_by_threshold=False
    )

    by_id = {c.chunk_id: c for c in result}
    # importance is min-max normalized across the pair: high's parent (9) > low's parent (1)
    assert by_id[high.id].importance_score == 1.0
    assert by_id[low.id].importance_score == 0.0


async def test_score_chunk_candidates_recency_uses_chunk_mentioned_entities(
    scored_chunks, relevance_session_factory, monkeypatch
):
    """Recency for a chunk comes from ITS OWN mentioned_entities (Prompt 11),
    not a fresh Graphiti lookup — no graph_memory call should happen here at
    all. `low`'s mentioned_entities is empty, so _recency_raw_score short-
    circuits to 0.0 for it WITHOUT calling get_entity_last_mentioned at all
    (same as _recency_raw_score's own "no entities" behavior) — only
    `high`'s non-empty mentioned_entities actually reaches the cache call."""
    high, low = scored_chunks["high"], scored_chunks["low"]
    monkeypatch.setattr(rs, "_embed_question", AsyncMock(return_value=None))
    mock_mentioned = AsyncMock(return_value={"Gila": 1000.0})
    monkeypatch.setattr(rs.cache_service, "get_entity_last_mentioned", mock_mentioned)
    monkeypatch.setattr(rs.time, "time", lambda: 1000.0)

    await rs.score_chunk_candidates("q", [high.id, low.id], "sess-1", filter_by_threshold=False)

    mock_mentioned.assert_called_once_with("sess-1", ["Gila"])


async def test_score_chunk_candidates_skips_missing_chunk(
    scored_chunks, relevance_session_factory, monkeypatch
):
    monkeypatch.setattr(rs, "_embed_question", AsyncMock(return_value=[1.0, 0.0, 0.0]))
    monkeypatch.setattr(rs.cache_service, "get_entity_last_mentioned", AsyncMock(return_value={}))

    result = await rs.score_chunk_candidates(
        "q", [scored_chunks["high"].id, "does-not-exist"], "sess-1"
    )
    assert [c.chunk_id for c in result] == [scored_chunks["high"].id]


async def test_score_chunk_candidates_degrades_relevance_when_embedding_fails(
    scored_chunks, relevance_session_factory, monkeypatch
):
    monkeypatch.setattr(rs.embeddings, "embed_text", AsyncMock(side_effect=RuntimeError("down")))
    monkeypatch.setattr(rs.cache_service, "get_entity_last_mentioned", AsyncMock(return_value={}))

    result = await rs.score_chunk_candidates(
        "q", [scored_chunks["high"].id, scored_chunks["low"].id], "sess-1"
    )
    assert scored_chunks["high"].id in [c.chunk_id for c in result]
