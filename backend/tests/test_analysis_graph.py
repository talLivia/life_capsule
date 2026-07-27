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


def test_names_are_similar_token_subset_match():
    assert ag._names_are_similar("Gila", "Gila Cohen")
    assert ag._names_are_similar("גילה", "גילה כהן")


def test_names_are_similar_rejects_unrelated_names():
    assert not ag._names_are_similar("Dan Cohen", "Gila")
    assert not ag._names_are_similar("דן כהן", "גילה")
    assert not ag._names_are_similar("Tel Aviv", "Gila")


def test_names_are_similar_rejects_shared_surname_only():
    """Regression test for a second live-smoke-test bug: two different
    people sharing only a surname must NOT count as similar, even though a
    whole-string SequenceMatcher ratio scores this pair (0.57) higher than
    the legitimate "Gila"/"Gila Cohen" match."""
    assert not ag._names_are_similar("Dan Cohen", "Gila Cohen")
    assert not ag._names_are_similar("דן כהן", "גילה כהן")


def test_names_are_similar_single_token_typo_variant():
    """Single-token spelling/transliteration variants still match via the
    strict character-similarity fallback."""
    assert ag._names_are_similar("גילה", "גליה")


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


def test_build_custom_extraction_instructions_uses_fuller_resolved_name():
    """The whole point of this fix: a bare mention ("Moshe") resolved to an
    existing fuller-named entity ("Moshe Cohen") must steer Graphiti using
    the fuller name, not just the ambiguous bare one."""
    text = ag._build_custom_extraction_instructions(
        {"Moshe": {"same_as_uuid": "uuid-1", "resolved_name": "Moshe Cohen"}}
    )
    assert "Moshe Cohen" in text
    assert "uuid-1" in text


# ── Node-level tests ─────────────────────────────────────────────────────────


async def test_transcribe_node_reuses_existing_transcript(segment, monkeypatch):
    # Prompt 11: transcribe_node now calls transcribe_with_timestamps
    # instead of transcribe (needs phrase/word timing for chunk creation) —
    # the reuse-shortcut still skips STT entirely either way.
    mock_transcribe = AsyncMock()
    monkeypatch.setattr(ag.stt_service, "transcribe_with_timestamps", mock_transcribe)

    result = await ag.transcribe_node({"segment_id": segment.id})

    assert result["transcript"] == segment.transcript
    assert "phrases" not in result
    mock_transcribe.assert_not_awaited()


async def test_transcribe_node_runs_stt_when_missing(db_session, segment, monkeypatch):
    segment.transcript = None
    await db_session.commit()

    monkeypatch.setattr(ag.storage_service, "download_file", AsyncMock(return_value=b"bytes"))
    monkeypatch.setattr(
        ag.stt_service,
        "transcribe_with_timestamps",
        AsyncMock(
            return_value={
                "text": "a fresh transcript",
                "phrases": [
                    {
                        "start_sec": 0.0,
                        "end_sec": 1.5,
                        "text": "a fresh transcript",
                        "words": [{"word": "a", "start_sec": 0.0, "end_sec": 0.2}],
                    }
                ],
            }
        ),
    )

    result = await ag.transcribe_node({"segment_id": segment.id})

    assert result["transcript"] == "a fresh transcript"
    assert len(result["phrases"]) == 1
    assert result["phrases"][0]["text"] == "a fresh transcript"
    await db_session.refresh(segment)
    assert segment.transcript == "a fresh transcript"


async def test_transcribe_node_errors_without_video_key(db_session, segment):
    segment.transcript = None
    segment.video_key = None
    await db_session.commit()

    result = await ag.transcribe_node({"segment_id": segment.id})
    assert "error" in result


# ── create_transcript_chunks_node tests (Prompt 11) ─────────────────────────


def _sample_phrases():
    return [
        {
            "start_sec": 0.0,
            "end_sec": 2.0,
            "text": "I grew up in a small house.",
            "words": [{"word": "I", "start_sec": 0.0, "end_sec": 0.2}],
        },
        {
            "start_sec": 2.0,
            "end_sec": 5.0,
            "text": "After the army I worked as a carpenter for years.",
            "words": [{"word": "After", "start_sec": 2.0, "end_sec": 2.3}],
        },
        {
            "start_sec": 5.0,
            "end_sec": 7.0,
            "text": "It was hard work but I enjoyed it.",
            "words": [{"word": "It", "start_sec": 5.0, "end_sec": 5.2}],
        },
    ]


async def test_create_transcript_chunks_node_one_row_per_phrase(db_session, segment, monkeypatch):
    monkeypatch.setattr(ag.embeddings, "embed_text", AsyncMock(return_value=[0.1, 0.2, 0.3]))
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value='["carpentry"]'))

    phrases = _sample_phrases()
    result = await ag.create_transcript_chunks_node({"segment_id": segment.id, "phrases": phrases})

    assert len(result["chunk_ids"]) == 3
    await db_session.refresh(segment, attribute_names=["transcript_chunks"])
    chunks = sorted(segment.transcript_chunks, key=lambda c: c.sequence_index)
    assert len(chunks) == 3
    assert chunks[0].text == "I grew up in a small house."
    assert chunks[0].start_sec == 0.0
    assert chunks[0].end_sec == 2.0
    assert chunks[0].word_timestamps == phrases[0]["words"]
    assert chunks[0].topic_tags == ["carpentry"]
    assert [c.sequence_index for c in chunks] == [0, 1, 2]
    assert chunks[1].text == "After the army I worked as a carpenter for years."


async def test_create_transcript_chunks_node_no_phrases_is_noop(segment):
    result = await ag.create_transcript_chunks_node({"segment_id": segment.id, "phrases": []})
    assert result == {}


async def test_create_transcript_chunks_node_embedding_uses_context_not_storage(
    db_session, segment, monkeypatch
):
    """The embedding call for the MIDDLE phrase must include its neighbors
    (window=1), but the chunk's own stored text must stay just that phrase —
    the context window is search-time-only, never what's returned/played
    back."""
    captured_texts = []

    async def fake_embed(text):
        captured_texts.append(text)
        return [0.5, 0.5]

    monkeypatch.setattr(ag.embeddings, "embed_text", fake_embed)
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value="[]"))

    phrases = _sample_phrases()
    await ag.create_transcript_chunks_node({"segment_id": segment.id, "phrases": phrases})

    middle_call_text = captured_texts[1]
    assert "After the army I worked as a carpenter for years." in middle_call_text
    assert "I grew up in a small house." in middle_call_text
    assert "It was hard work but I enjoyed it." in middle_call_text

    await db_session.refresh(segment, attribute_names=["transcript_chunks"])
    middle = next(c for c in segment.transcript_chunks if c.sequence_index == 1)
    assert middle.text == "After the army I worked as a carpenter for years."


async def test_create_transcript_chunks_node_embedding_failure_is_fail_soft(
    db_session, segment, monkeypatch
):
    monkeypatch.setattr(ag.embeddings, "embed_text", AsyncMock(side_effect=RuntimeError("down")))
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value="[]"))

    result = await ag.create_transcript_chunks_node(
        {"segment_id": segment.id, "phrases": _sample_phrases()}
    )
    assert len(result["chunk_ids"]) == 3

    await db_session.refresh(segment, attribute_names=["transcript_chunks"])
    assert all(c.embedding is None for c in segment.transcript_chunks)


async def test_create_transcript_chunks_node_rerun_replaces_old_chunks(
    db_session, segment, monkeypatch
):
    monkeypatch.setattr(ag.embeddings, "embed_text", AsyncMock(return_value=[0.1]))
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value="[]"))

    await ag.create_transcript_chunks_node({"segment_id": segment.id, "phrases": _sample_phrases()})
    await ag.create_transcript_chunks_node(
        {"segment_id": segment.id, "phrases": _sample_phrases()[:1]}
    )

    await db_session.refresh(segment, attribute_names=["transcript_chunks"])
    assert len(segment.transcript_chunks) == 1


async def test_tag_chunks_with_entities_substring_match(db_session, segment, monkeypatch):
    monkeypatch.setattr(ag.embeddings, "embed_text", AsyncMock(return_value=[0.1]))
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value="[]"))
    await ag.create_transcript_chunks_node({"segment_id": segment.id, "phrases": _sample_phrases()})

    await ag._tag_chunks_with_entities(segment.id, ["carpenter"])

    await db_session.refresh(segment, attribute_names=["transcript_chunks"])
    tagged = [c for c in segment.transcript_chunks if c.mentioned_entities]
    assert len(tagged) == 1
    assert tagged[0].text == "After the army I worked as a carpenter for years."
    assert tagged[0].mentioned_entities == ["carpenter"]


def test_normalize_for_entity_match_merges_tet_and_tav():
    """Confirmed live: faster-whisper transcribed "בטבריה" (correct) as
    "בתבריה" — a real Hebrew ASR letter confusion, not a hypothetical."""
    assert ag._normalize_for_entity_match("טבריה") == ag._normalize_for_entity_match("תבריה")


def test_normalize_for_entity_match_merges_final_letter_forms():
    """"כהן" (Cohen) naturally ends in the final-nun form (ן) since it's
    word-final here — the SAME name could appear mid-word elsewhere with
    the regular form (נ) instead, depending on surrounding text, so both
    must normalize to the same comparison form."""
    assert ag._normalize_for_entity_match("כהן") == ag._normalize_for_entity_match("כהנ")


async def test_tag_chunks_with_entities_matches_through_tet_tav_confusion(
    db_session, segment, monkeypatch
):
    """The exact real-world case: Graphiti extracted the correctly-spelled
    "טבריה", but the chunk's own (medium-model-transcribed) text has the
    ASR-confused "תבריה" — normalization must still find the match, and
    the STORED mentioned_entities value must be the ORIGINAL name from
    Graphiti, never the normalized form."""
    monkeypatch.setattr(ag.embeddings, "embed_text", AsyncMock(return_value=[0.1]))
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value="[]"))
    phrases = [
        {
            "start_sec": 0.0,
            "end_sec": 3.0,
            "text": "גדלתי בתבריה עד גיל 14.",
            "words": [{"word": "גדלתי", "start_sec": 0.0, "end_sec": 0.5}],
        }
    ]
    await ag.create_transcript_chunks_node({"segment_id": segment.id, "phrases": phrases})

    await ag._tag_chunks_with_entities(segment.id, ["טבריה"])

    await db_session.refresh(segment, attribute_names=["transcript_chunks"])
    tagged = [c for c in segment.transcript_chunks if c.mentioned_entities]
    assert len(tagged) == 1
    # Stored value is the ORIGINAL Graphiti spelling, not a normalized one.
    assert tagged[0].mentioned_entities == ["טבריה"]


async def test_check_entities_node_tags_chunks_with_entities(db_session, segment, monkeypatch):
    """check_entities_node's own disambiguation return value/behavior is
    completely unaffected — this only verifies the additive chunk-tagging
    side effect runs alongside it."""
    monkeypatch.setattr(ag.embeddings, "embed_text", AsyncMock(return_value=[0.1]))
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value="[]"))
    await ag.create_transcript_chunks_node({"segment_id": segment.id, "phrases": _sample_phrases()})

    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value='["carpenter"]'))
    monkeypatch.setattr(ag.graph_memory, "get_entity_candidates", AsyncMock(return_value=[]))

    result = await ag.check_entities_node(
        {"segment_id": segment.id, "group_id": "g1", "transcript": segment.transcript}
    )
    assert result["names_to_check"] == []
    assert result["entity_resolutions"] == {}

    await db_session.refresh(segment, attribute_names=["transcript_chunks"])
    tagged = [c for c in segment.transcript_chunks if c.mentioned_entities]
    assert len(tagged) == 1
    assert "carpenter" in tagged[0].mentioned_entities


async def test_embed_transcript_node_persists_vector(db_session, segment, monkeypatch):
    monkeypatch.setattr(ag.embeddings, "embed_text", AsyncMock(return_value=[0.1, 0.2, 0.3]))

    result = await ag.embed_transcript_node(
        {"segment_id": segment.id, "transcript": segment.transcript}
    )

    assert result["embedding"] == [0.1, 0.2, 0.3]
    await db_session.refresh(segment)
    assert segment.embedding == [0.1, 0.2, 0.3]


async def test_embed_transcript_node_tolerates_failure(db_session, segment, monkeypatch):
    monkeypatch.setattr(ag.embeddings, "embed_text", AsyncMock(side_effect=RuntimeError("down")))

    result = await ag.embed_transcript_node(
        {"segment_id": segment.id, "transcript": segment.transcript}
    )

    assert result == {}
    await db_session.refresh(segment)
    assert segment.embedding is None


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
    assert result["entity_resolutions"] == {"Gila": {"same_as_uuid": "u1", "resolved_name": "Gila"}}


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
    assert result["names_to_check"][0]["candidates"] == [
        {"uuid": "u2", "name": "Gila Cohen", "summary": "a neighbor"}
    ]


async def test_check_entities_node_multiple_candidates_stay_ambiguous(segment, monkeypatch):
    """The bug this whole fix addresses: a first-name-only mention ("Moshe")
    that real-matches MULTIPLE existing entities ("Moshe Cohen" AND "Moshe
    Levi") must surface all of them, not silently pick one and ask a plain
    yes/no against it."""
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value='["Moshe"]'))
    monkeypatch.setattr(
        ag.graph_memory,
        "get_entity_candidates",
        AsyncMock(
            return_value=[
                {"uuid": "u1", "name": "Moshe Cohen", "summary": "army friend"},
                {"uuid": "u2", "name": "Moshe Levi", "summary": "neighbor"},
            ]
        ),
    )

    result = await ag.check_entities_node(
        {"segment_id": segment.id, "group_id": "g1", "transcript": segment.transcript}
    )

    assert result["entity_resolutions"] == {}
    assert len(result["names_to_check"]) == 1
    assert result["names_to_check"][0]["name"] == "Moshe"
    assert result["names_to_check"][0]["candidates"] == [
        {"uuid": "u1", "name": "Moshe Cohen", "summary": "army friend"},
        {"uuid": "u2", "name": "Moshe Levi", "summary": "neighbor"},
    ]


async def test_check_entities_node_exact_match_among_others_still_ambiguous(segment, monkeypatch):
    """An exact-name match no longer short-circuits by itself if there's
    ALSO another real (non-exact) match — e.g. a bare "Moshe" exactly
    matching an existing bare "Moshe" node while ALSO fuzzy-matching
    "Moshe Cohen" must still ask, since the exact match isn't necessarily
    the right one."""
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value='["Moshe"]'))
    monkeypatch.setattr(
        ag.graph_memory,
        "get_entity_candidates",
        AsyncMock(
            return_value=[
                {"uuid": "u1", "name": "Moshe", "summary": "mentioned once before"},
                {"uuid": "u2", "name": "Moshe Cohen", "summary": "army friend"},
            ]
        ),
    )

    result = await ag.check_entities_node(
        {"segment_id": segment.id, "group_id": "g1", "transcript": segment.transcript}
    )

    assert result["entity_resolutions"] == {}
    assert len(result["names_to_check"]) == 1
    assert len(result["names_to_check"][0]["candidates"]) == 2


async def test_check_entities_node_no_candidates_means_new_entity(segment, monkeypatch):
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value='["Gila"]'))
    monkeypatch.setattr(ag.graph_memory, "get_entity_candidates", AsyncMock(return_value=[]))

    result = await ag.check_entities_node(
        {"segment_id": segment.id, "group_id": "g1", "transcript": segment.transcript}
    )

    assert result["names_to_check"] == []
    assert result["entity_resolutions"] == {}


async def test_check_entities_node_ignores_unrelated_candidate(segment, monkeypatch):
    """Regression test for a bug the live smoke test caught:
    get_entity_candidates has no minimum-relevance floor and returns a small
    graph's only node even for a completely unrelated query (confirmed live:
    querying "דן כהן" against a graph containing only "גילה" still returned
    "גילה" as a "candidate"). A brand-new, lexically unrelated name must NOT
    spuriously pause for human confirmation."""
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value='["Dan Cohen"]'))
    monkeypatch.setattr(
        ag.graph_memory,
        "get_entity_candidates",
        AsyncMock(return_value=[{"uuid": "u1", "name": "Gila", "summary": "a commander"}]),
    )

    result = await ag.check_entities_node(
        {"segment_id": segment.id, "group_id": "g1", "transcript": segment.transcript}
    )

    assert result["names_to_check"] == []
    assert result["entity_resolutions"] == {}


async def test_check_entities_node_ignores_shared_surname_candidate(segment, monkeypatch):
    """Regression test for a second live-smoke-test bug: "Gila Cohen" (a new
    name) fuzzy-matched an existing "Dan Cohen" node purely on the shared
    surname "Cohen" — two different people, not the same one referred to
    with different specificity, so this must not pause for confirmation."""
    monkeypatch.setattr(
        ag.llm_service, "generate_response", AsyncMock(return_value='["Gila Cohen"]')
    )
    monkeypatch.setattr(
        ag.graph_memory,
        "get_entity_candidates",
        AsyncMock(return_value=[{"uuid": "u1", "name": "Dan Cohen", "summary": "an engineer"}]),
    )

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
    async def fake_generate(messages, system_prompt=None, thinking=False, temperature=None):
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
    # transcribe_node clears the segment's prior episode before entity
    # resolution (see its comment). Unmocked, that reaches a REAL Graphiti
    # client — these are unit tests, and the live Neo4j/aiohttp connections it
    # opened were never closed, surfacing later as an "Event loop is closed"
    # teardown error on the integration test that runs after them.
    monkeypatch.setattr(
        ag.graph_memory, "remove_episodes_for_segment", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(ag.embeddings, "embed_text", AsyncMock(return_value=[0.1, 0.2, 0.3]))


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
    assert segment.embedding == [0.1, 0.2, 0.3]
    mock_add_episode.assert_awaited_once()
    assert mock_add_episode.call_args.kwargs["custom_extraction_instructions"] is None


async def test_full_pipeline_creates_transcript_chunks_for_a_fresh_transcription(
    db_session, segment, analysis_session_factory, fake_checkpointer, monkeypatch
):
    """Prompt 11 wired into the real graph end-to-end: a segment with no
    pre-set transcript gets STT with timestamps, chunks get created from the
    phrases, and the rest of the existing avatar-path pipeline (embedding,
    topics, entities, importance, finalize) still completes exactly as
    before — this new node sits in between without disturbing any of it."""
    segment.transcript = None
    await db_session.commit()

    monkeypatch.setattr(ag.storage_service, "download_file", AsyncMock(return_value=b"video bytes"))
    monkeypatch.setattr(
        ag.stt_service,
        "transcribe_with_timestamps",
        AsyncMock(
            return_value={
                "text": "I grew up with my grandmother Gila. She was a carpenter.",
                "phrases": [
                    {
                        "start_sec": 0.0,
                        "end_sec": 2.0,
                        "text": "I grew up with my grandmother Gila.",
                        "words": [],
                    },
                    {
                        "start_sec": 2.0,
                        "end_sec": 4.0,
                        "text": "She was a carpenter.",
                        "words": [],
                    },
                ],
            }
        ),
    )
    await _mock_all_llm_calls(monkeypatch, entity_candidates=[])
    monkeypatch.setattr(ag.graph_memory, "add_episode", AsyncMock())

    result = await ag.run_segment_analysis(segment.id)

    assert "__interrupt__" not in result
    await db_session.refresh(segment)
    assert segment.status == "ready"
    # Existing avatar-path fields, unaffected by the new node.
    assert segment.transcript == "I grew up with my grandmother Gila. She was a carpenter."
    assert segment.topic_tags == ["childhood"]
    assert segment.embedding == [0.1, 0.2, 0.3]

    await db_session.refresh(segment, attribute_names=["transcript_chunks"])
    chunks = sorted(segment.transcript_chunks, key=lambda c: c.sequence_index)
    assert len(chunks) == 2
    assert chunks[0].text == "I grew up with my grandmother Gila."
    assert chunks[1].text == "She was a carpenter."
    assert chunks[0].embedding == [0.1, 0.2, 0.3]


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
    assert segment.pending_confirmation["candidates"] == [
        {"uuid": "u2", "name": "Gila Cohen", "summary": "a neighbor"}
    ]

    await ag.resume_segment_analysis(
        segment.id, {"same_as_existing": True, "candidate_uuid": "u2"}
    )

    await db_session.refresh(segment)
    assert segment.status == "ready"
    mock_add_episode.assert_awaited_once()
    instructions = mock_add_episode.call_args.kwargs["custom_extraction_instructions"]
    assert "u2" in instructions
    assert "Gila Cohen" in instructions  # the fuller resolved name, not just "Gila"


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


async def test_confirm_entity_multi_candidate_flow(
    client: AsyncClient,
    db_session,
    segment,
    analysis_session_factory,
    fake_checkpointer,
    auth_headers,
    monkeypatch,
):
    """End-to-end HTTP coverage for the fix: a bare "Moshe" ambiguous
    against two existing entities surfaces both, rejects an unlisted
    candidate_uuid, rejects an omitted one (no single-candidate default
    applies with 2+ candidates), and succeeds when the right one is named."""

    async def fake_generate(messages, system_prompt=None, thinking=False, temperature=None):
        if system_prompt == ag._EXTRACT_TOPICS_SYSTEM_PROMPT:
            return '["childhood"]'
        if system_prompt == ag._ENTITY_NAME_SYSTEM_PROMPT:
            return '["Moshe"]'
        if system_prompt == ag._IMPORTANCE_SYSTEM_PROMPT:
            return "8"
        raise AssertionError(f"unexpected system_prompt: {system_prompt}")

    monkeypatch.setattr(ag.llm_service, "generate_response", fake_generate)
    monkeypatch.setattr(
        ag.graph_memory,
        "get_entity_candidates",
        AsyncMock(
            return_value=[
                {"uuid": "u1", "name": "Moshe Cohen", "summary": "army friend"},
                {"uuid": "u2", "name": "Moshe Levi", "summary": "neighbor"},
            ]
        ),
    )
    monkeypatch.setattr(ag.embeddings, "embed_text", AsyncMock(return_value=[0.1, 0.2, 0.3]))
    monkeypatch.setattr(ag.graph_memory, "add_episode", AsyncMock())
    segment_id = segment.id

    await ag.run_segment_analysis(segment_id)
    db_session.expire_all()

    pending = await client.get(
        "/api/v1/interview/segments/pending-confirmations", headers=auth_headers
    )
    candidates = pending.json()[0]["pending_confirmation"]["candidates"]
    assert {c["uuid"] for c in candidates} == {"u1", "u2"}

    # Omitting candidate_uuid with 2+ candidates must not silently default.
    resp = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entity",
        json={"entity_name": "Moshe", "same_as_existing": True},
        headers=auth_headers,
    )
    assert resp.status_code == 400

    # A candidate_uuid not among the pending options must be rejected.
    resp = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entity",
        json={"entity_name": "Moshe", "same_as_existing": True, "candidate_uuid": "not-a-real-uuid"},
        headers=auth_headers,
    )
    assert resp.status_code == 400

    # Naming the right one succeeds.
    resp = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entity",
        json={"entity_name": "Moshe", "same_as_existing": True, "candidate_uuid": "u1"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


async def test_confirm_entity_404_when_not_pending(client: AsyncClient, segment, auth_headers):
    resp = await client.post(
        f"/api/v1/interview/segments/{segment.id}/confirm-entity",
        json={"entity_name": "Gila", "same_as_existing": True},
        headers=auth_headers,
    )
    assert resp.status_code == 409
