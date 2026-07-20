"""
Tests for analysis_graph.py (Prompt 5).

No real Postgres/Neo4j/Anthropic here — node-level tests mock llm_service /
graph_memory / storage_service / stt_service directly, and the DB layer is
retargeted at the same in-memory SQLite engine the rest of the test suite
uses (analysis_graph.py normally opens sessions via app.database's module-
level AsyncSessionLocal, bypassing FastAPI's DI, so it needs its own
monkeypatch rather than the `client`/`db_session` override). The
human_confirm interrupt/resume path is exercised end-to-end against a real
LangGraph InMemorySaver, which behaves identically to AsyncPostgresSaver
from the graph's point of view — only the storage backend differs.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import analysis_graph as ag
from app.models import InterviewSession, RawSegment

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def analysis_session_factory(test_engine, monkeypatch):
    """Retarget analysis_graph's DB access at the same SQLite engine the
    `client`/`db_session` fixtures use, so segments created via HTTP are
    visible to node functions and vice versa."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(ag, "AsyncSessionLocal", factory)
    return factory


@pytest.fixture
def fake_checkpointer(monkeypatch):
    """One InMemorySaver reused across every _open_checkpointer() call in a
    test, so a pause (run_segment_analysis) and its resume
    (resume_segment_analysis) see the same checkpoint state — a fresh saver
    per call would silently lose the pending interrupt."""
    saver = InMemorySaver()

    @asynccontextmanager
    async def _fake():
        yield saver

    monkeypatch.setattr(ag, "_open_checkpointer", _fake)
    return saver


@pytest.fixture
async def segment(db_session, test_user, analysis_session_factory):
    """A segment already past transcription — most node tests don't need to
    exercise the Whisper/storage path."""
    session = InterviewSession(user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)

    seg = RawSegment(
        interview_session_id=session.id,
        question_asked="Tell me about your childhood",
        question_index=0,
        video_key=f"segments/{test_user.id}/{session.id}/0/take.webm",
        transcript="I grew up in a small house with my grandmother, Gila.",
        status="pending_analysis",
    )
    db_session.add(seg)
    await db_session.commit()
    await db_session.refresh(seg)
    return seg


# ── Helper parsing functions ────────────────────────────────────────────────


def test_parse_json_array_handles_markdown_fence():
    text = '```json\n["childhood", "family"]\n```'
    assert ag._parse_json_array(text) == ["childhood", "family"]


def test_parse_json_array_returns_empty_on_garbage():
    assert ag._parse_json_array("not json at all") == []


def test_parse_importance_score_clamps_range():
    assert ag._parse_importance_score("15") == 10
    assert ag._parse_importance_score("-3") == 0
    assert ag._parse_importance_score("7") == 7
    assert ag._parse_importance_score("no number here") == 5


def test_build_custom_extraction_instructions_empty():
    assert ag._build_custom_extraction_instructions({}) is None


def test_build_custom_extraction_instructions_same_and_different():
    text = ag._build_custom_extraction_instructions(
        {"Gila": {"same_as_uuid": "uuid-1"}, "Dan": {"same_as_uuid": None}}
    )
    assert "uuid-1" in text
    assert "Gila" in text
    assert "distinct from any other same-named entity" in text
    assert "Dan" in text


# ── Node-level tests ─────────────────────────────────────────────────────────


async def test_transcribe_node_reuses_existing_transcript(segment, monkeypatch):
    mock_transcribe = AsyncMock()
    monkeypatch.setattr(ag.stt_service, "transcribe", mock_transcribe)

    result = await ag.transcribe_node({"segment_id": segment.id})

    assert result["transcript"] == segment.transcript
    mock_transcribe.assert_not_awaited()


async def test_transcribe_node_runs_stt_when_missing(db_session, segment, monkeypatch):
    segment.transcript = None
    await db_session.commit()

    monkeypatch.setattr(ag.storage_service, "download_file", AsyncMock(return_value=b"bytes"))
    monkeypatch.setattr(ag.stt_service, "transcribe", AsyncMock(return_value="a fresh transcript"))

    result = await ag.transcribe_node({"segment_id": segment.id})

    assert result["transcript"] == "a fresh transcript"
    await db_session.refresh(segment)
    assert segment.transcript == "a fresh transcript"


async def test_transcribe_node_errors_without_video_key(db_session, segment):
    segment.transcript = None
    segment.video_key = None
    await db_session.commit()

    result = await ag.transcribe_node({"segment_id": segment.id})
    assert "error" in result


async def test_extract_topics_node_persists_parsed_tags(db_session, segment, monkeypatch):
    monkeypatch.setattr(
        ag.llm_service, "generate_response", AsyncMock(return_value='["childhood", "family"]')
    )

    result = await ag.extract_topics_node({"segment_id": segment.id, "transcript": segment.transcript})

    assert result["topic_tags"] == ["childhood", "family"]
    await db_session.refresh(segment)
    assert segment.topic_tags == ["childhood", "family"]


async def test_extract_topics_node_tolerates_llm_failure(segment, monkeypatch):
    monkeypatch.setattr(
        ag.llm_service, "generate_response", AsyncMock(side_effect=RuntimeError("down"))
    )
    result = await ag.extract_topics_node({"segment_id": segment.id, "transcript": segment.transcript})
    assert result["topic_tags"] == []


async def test_check_entities_node_auto_resolves_exact_match(segment, monkeypatch):
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value='["Gila"]'))
    monkeypatch.setattr(
        ag.graph_memory,
        "get_entity_candidates",
        AsyncMock(return_value=[{"uuid": "u1", "name": "Gila", "summary": "grandmother"}]),
    )

    result = await ag.check_entities_node(
        {"segment_id": segment.id, "group_id": "g1", "transcript": segment.transcript}
    )

    assert result["names_to_check"] == []
    assert result["entity_resolutions"] == {"Gila": {"same_as_uuid": "u1"}}


async def test_check_entities_node_flags_fuzzy_match(segment, monkeypatch):
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value='["Gila"]'))
    monkeypatch.setattr(
        ag.graph_memory,
        "get_entity_candidates",
        AsyncMock(return_value=[{"uuid": "u2", "name": "Gila Cohen", "summary": "a neighbor"}]),
    )

    result = await ag.check_entities_node(
        {"segment_id": segment.id, "group_id": "g1", "transcript": segment.transcript}
    )

    assert result["entity_resolutions"] == {}
    assert len(result["names_to_check"]) == 1
    assert result["names_to_check"][0]["name"] == "Gila"
    assert result["names_to_check"][0]["candidate_uuid"] == "u2"


async def test_check_entities_node_no_candidates_means_new_entity(segment, monkeypatch):
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value='["Gila"]'))
    monkeypatch.setattr(ag.graph_memory, "get_entity_candidates", AsyncMock(return_value=[]))

    result = await ag.check_entities_node(
        {"segment_id": segment.id, "group_id": "g1", "transcript": segment.transcript}
    )

    assert result["names_to_check"] == []
    assert result["entity_resolutions"] == {}


async def test_score_importance_node_persists_clamped_score(db_session, segment, monkeypatch):
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value="9"))

    result = await ag.score_importance_node({"segment_id": segment.id, "transcript": segment.transcript})

    assert result["importance_score"] == 9.0
    await db_session.refresh(segment)
    assert segment.importance_score == 9.0


async def test_score_importance_node_defaults_on_failure(segment, monkeypatch):
    monkeypatch.setattr(
        ag.llm_service, "generate_response", AsyncMock(side_effect=RuntimeError("down"))
    )
    result = await ag.score_importance_node({"segment_id": segment.id, "transcript": segment.transcript})
    assert result["importance_score"] == 5.0


async def test_finalize_ingest_node_marks_ready_and_builds_instructions(db_session, segment, monkeypatch):
    mock_add_episode = AsyncMock()
    monkeypatch.setattr(ag.graph_memory, "add_episode", mock_add_episode)

    result = await ag.finalize_ingest_node(
        {
            "segment_id": segment.id,
            "group_id": "producer-1",
            "transcript": segment.transcript,
            "topic_tags": ["childhood"],
            "entity_resolutions": {"Gila": {"same_as_uuid": "u1"}},
        }
    )

    assert result["status"] == "ready"
    await db_session.refresh(segment)
    assert segment.status == "ready"

    mock_add_episode.assert_awaited_once()
    kwargs = mock_add_episode.call_args.kwargs
    assert kwargs["group_id"] == "producer-1"
    assert "u1" in kwargs["custom_extraction_instructions"]


async def test_finalize_ingest_node_marks_failed_on_error(db_session, segment, monkeypatch):
    """finalize_ingest_node itself only reports the error — the graph's
    conditional edge routes to `fail_node`, which is what actually persists
    status='failed' (see test_full_pipeline_add_episode_failure_reaches_failed
    below for that end-to-end behavior)."""
    monkeypatch.setattr(
        ag.graph_memory, "add_episode", AsyncMock(side_effect=RuntimeError("neo4j down"))
    )

    result = await ag.finalize_ingest_node(
        {"segment_id": segment.id, "group_id": "producer-1", "transcript": segment.transcript}
    )

    assert result["status"] == "failed"
    assert "neo4j down" in result["error"]


# ── Full-graph tests (real LangGraph, InMemorySaver) ────────────────────────


async def _mock_all_llm_calls(monkeypatch, *, entity_candidates):
    async def fake_generate(messages, system_prompt=None, thinking=False):
        if system_prompt == ag._EXTRACT_TOPICS_SYSTEM_PROMPT:
            return '["childhood"]'
        if system_prompt == ag._ENTITY_NAME_SYSTEM_PROMPT:
            return '["Gila"]'
        if system_prompt == ag._IMPORTANCE_SYSTEM_PROMPT:
            return "8"
        raise AssertionError(f"unexpected system_prompt: {system_prompt}")

    monkeypatch.setattr(ag.llm_service, "generate_response", fake_generate)
    monkeypatch.setattr(
        ag.graph_memory, "get_entity_candidates", AsyncMock(return_value=entity_candidates)
    )


async def test_full_pipeline_no_ambiguity_reaches_ready(
    db_session, segment, analysis_session_factory, fake_checkpointer, monkeypatch
):
    await _mock_all_llm_calls(monkeypatch, entity_candidates=[])
    mock_add_episode = AsyncMock()
    monkeypatch.setattr(ag.graph_memory, "add_episode", mock_add_episode)

    result = await ag.run_segment_analysis(segment.id)

    assert "__interrupt__" not in result
    await db_session.refresh(segment)
    assert segment.status == "ready"
    assert segment.topic_tags == ["childhood"]
    assert segment.importance_score == 8.0
    mock_add_episode.assert_awaited_once()
    assert mock_add_episode.call_args.kwargs["custom_extraction_instructions"] is None


async def test_full_pipeline_pauses_then_resumes_on_ambiguous_entity(
    db_session, segment, analysis_session_factory, fake_checkpointer, monkeypatch
):
    await _mock_all_llm_calls(
        monkeypatch,
        entity_candidates=[{"uuid": "u2", "name": "Gila Cohen", "summary": "a neighbor"}],
    )
    mock_add_episode = AsyncMock()
    monkeypatch.setattr(ag.graph_memory, "add_episode", mock_add_episode)

    await ag.run_segment_analysis(segment.id)

    await db_session.refresh(segment)
    assert segment.status == "pending_confirmation"
    assert segment.pending_confirmation["entity_name"] == "Gila"
    assert segment.pending_confirmation["candidate_uuid"] == "u2"

    await ag.resume_segment_analysis(
        segment.id, {"same_as_existing": True, "candidate_uuid": "u2"}
    )

    await db_session.refresh(segment)
    assert segment.status == "ready"
    mock_add_episode.assert_awaited_once()
    assert "u2" in mock_add_episode.call_args.kwargs["custom_extraction_instructions"]


async def test_full_pipeline_add_episode_failure_reaches_failed(
    db_session, segment, analysis_session_factory, fake_checkpointer, monkeypatch
):
    await _mock_all_llm_calls(monkeypatch, entity_candidates=[])
    monkeypatch.setattr(
        ag.graph_memory, "add_episode", AsyncMock(side_effect=RuntimeError("neo4j down"))
    )

    result = await ag.run_segment_analysis(segment.id)

    assert result["status"] == "failed"
    await db_session.refresh(segment)
    assert segment.status == "failed"


# ── HTTP API tests ───────────────────────────────────────────────────────────
#
# NOTE: run_segment_analysis/resume_segment_analysis write through a
# separately-monkeypatched session (analysis_session_factory), not the
# `db_session` object the `client` fixture's get_db() override hands to
# every request. `db_session` is a single long-lived Session for the whole
# test (expire_on_commit=False), so once it has the segment/interview_session
# rows in its identity map it will keep serving those cached instances to
# the app's own queries unless explicitly expired — hence `expire_all()`
# after every out-of-band write below.


async def test_confirm_entity_endpoint_resumes_and_returns_ready(
    client: AsyncClient,
    db_session,
    segment,
    analysis_session_factory,
    fake_checkpointer,
    auth_headers,
    monkeypatch,
):
    await _mock_all_llm_calls(
        monkeypatch,
        entity_candidates=[{"uuid": "u2", "name": "Gila Cohen", "summary": "a neighbor"}],
    )
    monkeypatch.setattr(ag.graph_memory, "add_episode", AsyncMock())
    segment_id = segment.id

    await ag.run_segment_analysis(segment_id)
    db_session.expire_all()

    pending = await client.get("/api/v1/interview/segments/pending-confirmations", headers=auth_headers)
    assert pending.status_code == 200
    body = pending.json()
    assert len(body) == 1
    assert body[0]["segment_id"] == segment_id
    assert body[0]["pending_confirmation"]["entity_name"] == "Gila"

    resp = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entity",
        json={"entity_name": "Gila", "same_as_existing": True, "candidate_uuid": "u2"},
        headers=auth_headers,
    )
    db_session.expire_all()
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"

    pending_after = await client.get(
        "/api/v1/interview/segments/pending-confirmations", headers=auth_headers
    )
    assert pending_after.json() == []


async def test_confirm_entity_rejects_stale_question(
    client: AsyncClient,
    db_session,
    segment,
    analysis_session_factory,
    fake_checkpointer,
    auth_headers,
    monkeypatch,
):
    await _mock_all_llm_calls(
        monkeypatch,
        entity_candidates=[{"uuid": "u2", "name": "Gila Cohen", "summary": "a neighbor"}],
    )
    monkeypatch.setattr(ag.graph_memory, "add_episode", AsyncMock())
    segment_id = segment.id
    await ag.run_segment_analysis(segment_id)
    db_session.expire_all()

    resp = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entity",
        json={"entity_name": "SomeoneElse", "same_as_existing": True},
        headers=auth_headers,
    )
    assert resp.status_code == 409


async def test_confirm_entity_404_when_not_pending(client: AsyncClient, segment, auth_headers):
    resp = await client.post(
        f"/api/v1/interview/segments/{segment.id}/confirm-entity",
        json={"entity_name": "Gila", "same_as_existing": True},
        headers=auth_headers,
    )
    assert resp.status_code == 409
