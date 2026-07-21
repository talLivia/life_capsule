"""
Tests for response_assembler.py (Prompt 8). retrieval_service.retrieve and
relevance_scorer.score_candidates are mocked (their own logic is covered by
Prompt 6/7's own test suites) — this suite verifies assembly, bridge-phrase
injection, the no-story fallback, and the visited-set/entity-mention writes
this module owns.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InterviewSession, RawSegment
from app.services import response_assembler as ra
from app.services.relevance_scorer import ScoredSegment
from app.services.retrieval_service import RetrievalResult, RetrievedSegment

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def assembler_session_factory(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(ra, "AsyncSessionLocal", factory)
    return factory


@pytest.fixture
async def segments(db_session, test_user, assembler_session_factory):
    session = InterviewSession(user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)

    rows = {}
    for label, text in [
        ("primary", "I served in the army with Gila."),
        ("primary2", "Gila taught me about leadership."),
        ("candidate1", "Years later I married Gila."),
        ("candidate2", "I also worked with Dan at the tech company."),
    ]:
        seg = RawSegment(
            interview_session_id=session.id,
            question_asked="Q",
            question_index=len(rows),
            transcript=text,
            status="ready",
        )
        db_session.add(seg)
        rows[label] = seg
    await db_session.commit()
    for seg in rows.values():
        await db_session.refresh(seg)
    return rows


def _retrieved(segment_id: str) -> RetrievedSegment:
    return RetrievedSegment(segment_id=segment_id, summary="short summary")


def _scored(segment_id: str, score: float = 1.5) -> ScoredSegment:
    return ScoredSegment(
        segment_id=segment_id,
        summary="short summary",
        score=score,
        recency_score=0.5,
        importance_score=0.5,
        relevance_score=0.5,
    )


# ── _pick_bridge_phrase ──────────────────────────────────────────────────────


def test_pick_bridge_phrase_cycles_through_bank():
    n = len(ra.BRIDGE_PHRASE_TEMPLATES)
    for i in range(n * 2):
        assert ra._pick_bridge_phrase(i) == ra.BRIDGE_PHRASE_TEMPLATES[i % n]


# ── assemble_response ────────────────────────────────────────────────────────


async def test_no_primary_returns_fallback_with_no_side_effects(monkeypatch):
    monkeypatch.setattr(
        ra.retrieval_service, "retrieve", AsyncMock(return_value=RetrievalResult())
    )
    mock_score = AsyncMock()
    mock_add_visited = AsyncMock()
    monkeypatch.setattr(ra.relevance_scorer, "score_candidates", mock_score)
    monkeypatch.setattr(ra.cache_service, "add_visited", mock_add_visited)

    result = await ra.assemble_response("anything", "g1", "en", "sess-1")

    assert result == ra.NO_STORY_FALLBACK
    mock_score.assert_not_called()
    mock_add_visited.assert_not_called()


async def test_primary_only_no_candidates(segments, assembler_session_factory, monkeypatch):
    primary = segments["primary"]
    monkeypatch.setattr(
        ra.retrieval_service,
        "retrieve",
        AsyncMock(return_value=RetrievalResult(primary=[_retrieved(primary.id)], candidates=[])),
    )
    monkeypatch.setattr(ra.relevance_scorer, "score_candidates", AsyncMock(return_value=[]))
    mock_add_visited = AsyncMock()
    mock_record_mentions = AsyncMock()
    monkeypatch.setattr(ra.cache_service, "add_visited", mock_add_visited)
    monkeypatch.setattr(ra.cache_service, "record_entity_mentions", mock_record_mentions)

    result = await ra.assemble_response("Tell me about the army", "g1", "en", "sess-1")

    assert result == primary.transcript
    mock_add_visited.assert_awaited_once_with("sess-1", [primary.id])
    mock_record_mentions.assert_not_called()


async def test_approved_candidate_injects_bridge_phrase(
    segments, assembler_session_factory, monkeypatch
):
    primary, candidate = segments["primary"], segments["candidate1"]

    monkeypatch.setattr(
        ra.retrieval_service,
        "retrieve",
        AsyncMock(
            return_value=RetrievalResult(
                primary=[_retrieved(primary.id)], candidates=[_retrieved(candidate.id)]
            )
        ),
    )
    monkeypatch.setattr(
        ra.relevance_scorer, "score_candidates", AsyncMock(return_value=[_scored(candidate.id)])
    )

    async def fake_entities(segment_id, group_id=None):
        return ["Gila"] if segment_id in (primary.id, candidate.id) else []

    monkeypatch.setattr(ra.graph_memory, "get_episode_entity_names", fake_entities)
    mock_add_visited = AsyncMock()
    mock_record_mentions = AsyncMock()
    monkeypatch.setattr(ra.cache_service, "add_visited", mock_add_visited)
    monkeypatch.setattr(ra.cache_service, "record_entity_mentions", mock_record_mentions)

    result = await ra.assemble_response("Tell me about Gila", "g1", "en", "sess-1")

    assert result.startswith(primary.transcript)
    assert "Gila" in result
    assert result.endswith(candidate.transcript)
    # Bridge phrase template appears with the entity substituted in.
    assert any(
        tpl.format(entity="Gila") in result for tpl in ra.BRIDGE_PHRASE_TEMPLATES
    )

    mock_add_visited.assert_awaited_once_with("sess-1", [primary.id, candidate.id])
    mock_record_mentions.assert_awaited_once_with("sess-1", ["Gila"])


async def test_multiple_candidates_cycle_bridge_phrases(
    segments, assembler_session_factory, monkeypatch
):
    primary, c1, c2 = segments["primary"], segments["candidate1"], segments["candidate2"]

    monkeypatch.setattr(
        ra.retrieval_service,
        "retrieve",
        AsyncMock(
            return_value=RetrievalResult(
                primary=[_retrieved(primary.id)],
                candidates=[_retrieved(c1.id), _retrieved(c2.id)],
            )
        ),
    )
    monkeypatch.setattr(
        ra.relevance_scorer,
        "score_candidates",
        AsyncMock(return_value=[_scored(c1.id, 2.0), _scored(c2.id, 1.5)]),
    )

    async def fake_entities(segment_id, group_id=None):
        return {
            primary.id: ["Gila", "Dan"],
            c1.id: ["Gila"],
            c2.id: ["Dan"],
        }.get(segment_id, [])

    monkeypatch.setattr(ra.graph_memory, "get_episode_entity_names", fake_entities)
    monkeypatch.setattr(ra.cache_service, "add_visited", AsyncMock())
    monkeypatch.setattr(ra.cache_service, "record_entity_mentions", AsyncMock())

    result = await ra.assemble_response("Tell me everything", "g1", "en", "sess-1")

    assert ra.BRIDGE_PHRASE_TEMPLATES[0].format(entity="Gila") in result
    assert ra.BRIDGE_PHRASE_TEMPLATES[1].format(entity="Dan") in result
    assert c1.transcript in result
    assert c2.transcript in result


async def test_candidate_without_shared_entity_is_skipped(
    segments, assembler_session_factory, monkeypatch
):
    primary, candidate = segments["primary"], segments["candidate2"]

    monkeypatch.setattr(
        ra.retrieval_service,
        "retrieve",
        AsyncMock(
            return_value=RetrievalResult(
                primary=[_retrieved(primary.id)], candidates=[_retrieved(candidate.id)]
            )
        ),
    )
    monkeypatch.setattr(
        ra.relevance_scorer, "score_candidates", AsyncMock(return_value=[_scored(candidate.id)])
    )

    async def fake_entities(segment_id, group_id=None):
        # Disjoint entity sets — no shared entity between primary and candidate.
        return ["Gila"] if segment_id == primary.id else ["Dan"]

    monkeypatch.setattr(ra.graph_memory, "get_episode_entity_names", fake_entities)
    mock_add_visited = AsyncMock()
    monkeypatch.setattr(ra.cache_service, "add_visited", mock_add_visited)
    monkeypatch.setattr(ra.cache_service, "record_entity_mentions", AsyncMock())

    result = await ra.assemble_response("Tell me about the army", "g1", "en", "sess-1")

    assert result == primary.transcript  # candidate silently dropped, no bridge
    mock_add_visited.assert_awaited_once_with("sess-1", [primary.id])


async def test_multiple_primary_segments_are_joined(
    segments, assembler_session_factory, monkeypatch
):
    p1, p2 = segments["primary"], segments["primary2"]
    monkeypatch.setattr(
        ra.retrieval_service,
        "retrieve",
        AsyncMock(
            return_value=RetrievalResult(
                primary=[_retrieved(p1.id), _retrieved(p2.id)], candidates=[]
            )
        ),
    )
    monkeypatch.setattr(ra.relevance_scorer, "score_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(ra.cache_service, "add_visited", AsyncMock())

    result = await ra.assemble_response("Tell me about Gila", "g1", "en", "sess-1")

    assert p1.transcript in result
    assert p2.transcript in result
