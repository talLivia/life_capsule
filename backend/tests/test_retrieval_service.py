"""
Tests for retrieval_service.py (Prompt 6). llm_service and graph_memory are
mocked throughout — this suite verifies retrieval_service's own logic
(topic classification handling, Postgres overlap matching, exclusion/
threshold/cap behavior), not Graphiti's live Cypher behavior (see
scripts/smoke_test_prompt5.py's live-verification approach for that, and
the module docstring's note that graph_memory.find_related_episodes_scored
was spot-checked against a real instance).
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InterviewSession, RawSegment
from app.services import retrieval_service as rsvc

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def retrieval_session_factory(test_engine, monkeypatch):
    """Retarget retrieval_service's DB access at the same SQLite engine the
    `client`/`db_session` fixtures use (it opens its own sessions via the
    module-level AsyncSessionLocal, bypassing FastAPI's DI)."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(rsvc, "AsyncSessionLocal", factory)
    return factory


@pytest.fixture
async def producer_segments(db_session, test_user, retrieval_session_factory):
    """Three 'ready' segments for test_user's archive plus one 'pending'
    segment that must never be matched, covering: a topic match, a
    different topic, and a not-yet-ready segment."""
    session = InterviewSession(user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)

    matching = RawSegment(
        interview_session_id=session.id,
        question_asked="Q1",
        question_index=0,
        transcript="I served in the army for three years.",
        topic_tags=["military service"],
        status="ready",
    )
    other_topic = RawSegment(
        interview_session_id=session.id,
        question_asked="Q2",
        question_index=1,
        transcript="I started working as an engineer.",
        topic_tags=["career"],
        status="ready",
    )
    not_ready = RawSegment(
        interview_session_id=session.id,
        question_asked="Q3",
        question_index=2,
        transcript="Still being processed.",
        topic_tags=["military service"],
        status="pending_analysis",
    )
    for seg in (matching, other_topic, not_ready):
        db_session.add(seg)
    await db_session.commit()
    for seg in (matching, other_topic, not_ready):
        await db_session.refresh(seg)

    return {"session_id": session.id, "matching": matching, "other_topic": other_topic, "not_ready": not_ready}


# ── _short_summary ───────────────────────────────────────────────────────────


def test_short_summary_passes_through_short_text():
    assert rsvc._short_summary("A short story.") == "A short story."


def test_short_summary_truncates_long_text_at_word_boundary():
    text = "word " * 60
    summary = rsvc._short_summary(text)
    assert len(summary) <= rsvc._SUMMARY_MAX_CHARS + 1
    assert summary.endswith("…")
    assert not summary[:-1].endswith(" ")


def test_short_summary_handles_none():
    assert rsvc._short_summary(None) == ""


# ── _classify_topic ──────────────────────────────────────────────────────────


async def test_classify_topic_normalizes_output(monkeypatch):
    monkeypatch.setattr(
        rsvc.llm_service, "generate_response", AsyncMock(return_value='"Military Service"')
    )
    topic = await rsvc._classify_topic("Tell me about the army", "en")
    assert topic == "military service"


async def test_classify_topic_returns_none_on_llm_failure(monkeypatch):
    monkeypatch.setattr(
        rsvc.llm_service, "generate_response", AsyncMock(side_effect=RuntimeError("down"))
    )
    assert await rsvc._classify_topic("anything", "en") is None


async def test_classify_topic_passes_temperature_zero(monkeypatch):
    mock = AsyncMock(return_value="career")
    monkeypatch.setattr(rsvc.llm_service, "generate_response", mock)
    await rsvc._classify_topic("What was your job?", "en")
    assert mock.call_args.kwargs["temperature"] == 0


# ── primary_match ────────────────────────────────────────────────────────────


async def test_primary_match_returns_only_ready_overlapping_segments(
    test_user, producer_segments, retrieval_session_factory, monkeypatch
):
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value="military service"))

    matches = await rsvc.primary_match("Tell me about the army", test_user.id, "en")

    assert [s.id for s in matches] == [producer_segments["matching"].id]


async def test_primary_match_scoped_to_producer(
    test_user, producer_segments, retrieval_session_factory, monkeypatch
):
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value="military service"))

    matches = await rsvc.primary_match("Tell me about the army", "someone-elses-id", "en")

    assert matches == []


async def test_primary_match_empty_when_topic_classification_fails(
    test_user, producer_segments, retrieval_session_factory, monkeypatch
):
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value=None))
    matches = await rsvc.primary_match("???", test_user.id, "en")
    assert matches == []


# ── expand_graph ─────────────────────────────────────────────────────────────


async def test_expand_graph_no_entities_returns_empty(producer_segments, monkeypatch):
    monkeypatch.setattr(rsvc.graph_memory, "get_episode_entity_names", AsyncMock(return_value=[]))
    result = await rsvc.expand_graph([producer_segments["matching"]], set(), "g1")
    assert result == []


async def test_expand_graph_excludes_visited_and_primary_ids(
    producer_segments, retrieval_session_factory, monkeypatch
):
    primary = producer_segments["matching"]
    monkeypatch.setattr(
        rsvc.graph_memory, "get_episode_entity_names", AsyncMock(return_value=["Gila"])
    )
    mock_find = AsyncMock(return_value=[])
    monkeypatch.setattr(rsvc.graph_memory, "find_related_episodes_scored", mock_find)

    await rsvc.expand_graph([primary], {"visited-1"}, "g1")

    exclude_ids = set(mock_find.call_args.kwargs["exclude_ids"])
    assert "visited-1" in exclude_ids
    assert primary.id in exclude_ids


async def test_expand_graph_filters_below_threshold_and_caps(
    producer_segments, retrieval_session_factory, monkeypatch
):
    other = producer_segments["other_topic"]
    not_ready = producer_segments["not_ready"]
    monkeypatch.setattr(
        rsvc.graph_memory, "get_episode_entity_names", AsyncMock(return_value=["Gila"])
    )
    monkeypatch.setattr(
        rsvc.graph_memory,
        "find_related_episodes_scored",
        AsyncMock(
            return_value=[
                {"segment_id": other.id, "shared_entity_count": 2},
                {"segment_id": not_ready.id, "shared_entity_count": 1},
                {"segment_id": "below-threshold", "shared_entity_count": 0},
            ]
        ),
    )

    result = await rsvc.expand_graph(
        [producer_segments["matching"]], set(), "g1"
    )

    # below-threshold (count 0 < MIN_SHARED_ENTITY_COUNT) excluded; the other
    # two qualify and both exist in the DB, so both appear (cap is 2).
    assert [r.segment_id for r in result] == [other.id, not_ready.id]
    assert result[0].summary == other.transcript  # short enough to pass through untruncated


async def test_expand_graph_caps_at_max_candidates(
    db_session, producer_segments, retrieval_session_factory, monkeypatch
):
    session_id = producer_segments["session_id"]
    extra_segments = []
    for i in range(3, 6):
        seg = RawSegment(
            interview_session_id=session_id,
            question_asked=f"Q{i}",
            question_index=i,
            transcript=f"Extra segment {i}",
            topic_tags=["military service"],
            status="ready",
        )
        db_session.add(seg)
        extra_segments.append(seg)
    await db_session.commit()
    for seg in extra_segments:
        await db_session.refresh(seg)

    monkeypatch.setattr(
        rsvc.graph_memory, "get_episode_entity_names", AsyncMock(return_value=["Gila"])
    )
    monkeypatch.setattr(
        rsvc.graph_memory,
        "find_related_episodes_scored",
        AsyncMock(
            return_value=[{"segment_id": seg.id, "shared_entity_count": 5} for seg in extra_segments]
        ),
    )

    result = await rsvc.expand_graph([producer_segments["matching"]], set(), "g1")

    assert len(result) == rsvc.MAX_CANDIDATES


# ── retrieve (orchestration) ─────────────────────────────────────────────────


async def test_retrieve_returns_empty_result_when_no_primary_match(
    test_user, producer_segments, retrieval_session_factory, monkeypatch
):
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value="nonexistent-topic"))
    result = await rsvc.retrieve("??", test_user.id, "en", "sess-1")
    assert result.primary == []
    assert result.candidates == []


async def test_retrieve_reads_visited_set_but_never_writes_it(
    test_user, producer_segments, retrieval_session_factory, monkeypatch
):
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value="military service"))
    monkeypatch.setattr(rsvc.graph_memory, "get_episode_entity_names", AsyncMock(return_value=[]))
    mock_get_visited = AsyncMock(return_value=set())
    mock_add_visited = AsyncMock()
    monkeypatch.setattr(rsvc.cache_service, "get_visited", mock_get_visited)
    monkeypatch.setattr(rsvc.cache_service, "add_visited", mock_add_visited)

    result = await rsvc.retrieve("Tell me about the army", test_user.id, "en", "sess-1")

    assert [s.segment_id for s in result.primary] == [producer_segments["matching"].id]
    mock_get_visited.assert_awaited_once_with("sess-1")
    mock_add_visited.assert_not_called()
