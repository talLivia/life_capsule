"""
Tests for retrieval_service.py (Prompt 6). llm_service and entity_store are
mocked throughout — this suite verifies retrieval_service's own logic
(topic classification handling, Postgres overlap matching, exclusion/
threshold/cap behavior), not Graphiti's live Cypher behavior (see
scripts/smoke_test_prompt5.py's live-verification approach for that, and
the module docstring's note that entity_store.find_segments_mentioning_scored
was spot-checked against a real instance).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Avatar, InterviewSession, Message, RawSegment
from app.models import Session as SessionModel
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


@pytest.fixture
async def chat_session_with_messages(db_session, test_user, retrieval_session_factory):
    """A real WS Session (with the Avatar its FK requires) plus a helper to
    add Message rows at controlled timestamps — _recent_turns' ORDER BY
    (created_at, id) would otherwise tie-break on Message.id, a random
    UUID, not a stable insertion-order key."""
    avatar = Avatar(
        user_id=test_user.id,
        name="A",
        image_url="http://x/i.jpg",
        s3_key="avatars/x/i.jpg",
        status="ready",
    )
    db_session.add(avatar)
    await db_session.flush()
    session = SessionModel(
        user_id=test_user.id, producer_id=test_user.id, avatar_id=avatar.id, status="active"
    )
    db_session.add(session)
    await db_session.flush()

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async def add_message(role: str, content: str, minutes_offset: float):
        msg = Message(
            session_id=session.id,
            role=role,
            content=content,
            content_type="text",
            created_at=base + timedelta(minutes=minutes_offset),
        )
        db_session.add(msg)
        await db_session.flush()
        return msg

    return {"session_id": session.id, "add_message": add_message}


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


# ── _recent_turns / _render_turn_for_history ─────────────────────────────────


async def test_recent_turns_returns_empty_for_session_with_no_messages(retrieval_session_factory):
    turns = await rsvc._recent_turns("no-such-session", rsvc.COREFERENCE_HISTORY_TURNS)
    assert turns == []


async def test_recent_turns_returns_last_n_in_chronological_order(chat_session_with_messages):
    add_message = chat_session_with_messages["add_message"]
    await add_message("user", "who is Gila?", 0)
    await add_message("assistant", "Gila was my neighbor growing up.", 1)
    await add_message("user", "did you love her?", 2)

    turns = await rsvc._recent_turns(chat_session_with_messages["session_id"], 2)

    assert [t["content"] for t in turns] == ["Gila was my neighbor growing up.", "did you love her?"]
    assert [t["role"] for t in turns] == ["assistant", "user"]


async def test_recent_turns_respects_limit_even_with_more_history(chat_session_with_messages):
    add_message = chat_session_with_messages["add_message"]
    for i in range(5):
        await add_message("user", f"question {i}", i)

    turns = await rsvc._recent_turns(chat_session_with_messages["session_id"], 2)
    assert [t["content"] for t in turns] == ["question 3", "question 4"]


def test_render_turn_for_history_passes_through_plain_text():
    assert rsvc._render_turn_for_history("user", "who is Gila?") == "user: who is Gila?"


def test_render_turn_for_history_masks_video_clip_url():
    """video_clip_assembler persists a raw video URL as the assistant's
    Message.content — must never be fed to the coreference LLM call as if
    it were narration."""
    rendered = rsvc._render_turn_for_history(
        "assistant", "http://localhost:8000/uploads/video-clips/abc123.mp4"
    )
    assert rendered == "assistant: (showed a video clip)"


# ── _resolve_coreferences ─────────────────────────────────────────────────────


async def test_resolve_coreferences_skips_llm_call_when_no_history(
    monkeypatch, retrieval_session_factory
):
    """No prior turns (first question of a session) — nothing to resolve
    against, so this must not even attempt an LLM call."""
    mock = AsyncMock()
    monkeypatch.setattr(rsvc.llm_service, "generate_response", mock)

    result = await rsvc._resolve_coreferences("did you love her?", "fresh-session", "en")

    assert result == "did you love her?"
    mock.assert_not_called()


async def test_resolve_coreferences_rewrites_pronoun_using_history(
    monkeypatch, chat_session_with_messages
):
    add_message = chat_session_with_messages["add_message"]
    await add_message("user", "who is Gila?", 0)
    await add_message("assistant", "Gila was my neighbor growing up.", 1)

    mock = AsyncMock(return_value="did you love Gila?")
    monkeypatch.setattr(rsvc.llm_service, "generate_response", mock)

    result = await rsvc._resolve_coreferences(
        "did you love her?", chat_session_with_messages["session_id"], "en"
    )

    assert result == "did you love Gila?"
    # The actual conversation text must reach the LLM call, not just a signal
    # that history exists.
    assert "Gila was my neighbor" in mock.call_args.kwargs["system_prompt"]
    assert mock.call_args.kwargs["temperature"] == 0


async def test_resolve_coreferences_leaves_self_contained_question_unchanged(
    monkeypatch, chat_session_with_messages
):
    add_message = chat_session_with_messages["add_message"]
    await add_message("user", "what did you do for work?", 0)
    await add_message("assistant", "I was an engineer.", 1)

    monkeypatch.setattr(
        rsvc.llm_service, "generate_response", AsyncMock(return_value="what was your favorite food?")
    )

    result = await rsvc._resolve_coreferences(
        "what was your favorite food?", chat_session_with_messages["session_id"], "en"
    )
    assert result == "what was your favorite food?"


async def test_resolve_coreferences_fails_soft_on_llm_error(monkeypatch, chat_session_with_messages):
    add_message = chat_session_with_messages["add_message"]
    await add_message("user", "who is Gila?", 0)

    monkeypatch.setattr(
        rsvc.llm_service, "generate_response", AsyncMock(side_effect=RuntimeError("down"))
    )

    result = await rsvc._resolve_coreferences(
        "did you love her?", chat_session_with_messages["session_id"], "en"
    )
    assert result == "did you love her?"


async def test_resolve_coreferences_masks_video_url_in_history_sent_to_llm(
    monkeypatch, chat_session_with_messages
):
    """End-to-end through _resolve_coreferences (not just _render_turn_for_
    history in isolation): a video-clip-mode assistant turn's raw URL must
    never appear in the prompt sent to the LLM."""
    add_message = chat_session_with_messages["add_message"]
    await add_message("user", "who is Gila?", 0)
    await add_message("assistant", "http://localhost:8000/uploads/video-clips/abc123.mp4", 1)

    mock = AsyncMock(return_value="did you love Gila?")
    monkeypatch.setattr(rsvc.llm_service, "generate_response", mock)

    await rsvc._resolve_coreferences("did you love her?", chat_session_with_messages["session_id"], "en")

    system_prompt = mock.call_args.kwargs["system_prompt"]
    assert "http://" not in system_prompt
    assert "(showed a video clip)" in system_prompt


# ── primary_match ────────────────────────────────────────────────────────────


async def test_primary_match_returns_only_ready_overlapping_segments(
    test_user, producer_segments, retrieval_session_factory, monkeypatch
):
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value="military service"))
    monkeypatch.setattr(rsvc, "_extract_entity_names_from_question", AsyncMock(return_value=[]))
    monkeypatch.setattr(rsvc, "_embed_question_for_primary_match", AsyncMock(return_value=None))

    matches = await rsvc.primary_match("Tell me about the army", test_user.id, "en", "sess-1")

    assert [s.id for s in matches] == [producer_segments["matching"].id]


async def test_primary_match_scoped_to_producer(
    test_user, producer_segments, retrieval_session_factory, monkeypatch
):
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value="military service"))
    monkeypatch.setattr(rsvc, "_extract_entity_names_from_question", AsyncMock(return_value=[]))
    monkeypatch.setattr(rsvc, "_embed_question_for_primary_match", AsyncMock(return_value=None))

    matches = await rsvc.primary_match("Tell me about the army", "someone-elses-id", "en", "sess-1")

    assert matches == []


async def test_primary_match_empty_when_topic_classification_fails(
    test_user, producer_segments, retrieval_session_factory, monkeypatch
):
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value=None))
    monkeypatch.setattr(rsvc, "_extract_entity_names_from_question", AsyncMock(return_value=[]))
    monkeypatch.setattr(rsvc, "_embed_question_for_primary_match", AsyncMock(return_value=None))
    matches = await rsvc.primary_match("???", test_user.id, "en", "sess-1")
    assert matches == []


async def test_primary_match_uses_coreference_resolved_question(
    test_user, producer_segments, retrieval_session_factory, monkeypatch
):
    """A bare pronoun follow-up ("tell me more about it") carries no signal
    of its own — primary_match must classify/extract/embed the RESOLVED
    question, not the raw original."""
    monkeypatch.setattr(
        rsvc, "_resolve_coreferences", AsyncMock(return_value="Tell me about the army")
    )
    mock_classify = AsyncMock(return_value="military service")
    monkeypatch.setattr(rsvc, "_classify_topic", mock_classify)
    monkeypatch.setattr(rsvc, "_extract_entity_names_from_question", AsyncMock(return_value=[]))
    monkeypatch.setattr(rsvc, "_embed_question_for_primary_match", AsyncMock(return_value=None))

    matches = await rsvc.primary_match("tell me more about it", test_user.id, "en", "sess-1")

    assert [s.id for s in matches] == [producer_segments["matching"].id]
    assert mock_classify.call_args.args[0] == "Tell me about the army"


async def test_primary_match_caps_runaway_match_count(
    test_user, producer_segments, retrieval_session_factory, db_session, monkeypatch, caplog
):
    """Confirmed live: an under-specified question can make every signal
    (or just the semantic one) match nearly everything. MAX_PRIMARY_MATCHES
    must reject that as untrustworthy rather than returning it."""
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value=None))
    monkeypatch.setattr(rsvc, "_extract_entity_names_from_question", AsyncMock(return_value=[]))
    matching = producer_segments["matching"]
    other_topic = producer_segments["other_topic"]
    matching.embedding = [1.0, 0.0, 0.0]
    other_topic.embedding = [1.0, 0.0, 0.0]
    db_session.add_all([matching, other_topic])
    await db_session.commit()
    monkeypatch.setattr(
        rsvc, "_embed_question_for_primary_match", AsyncMock(return_value=[1.0, 0.0, 0.0])
    )
    monkeypatch.setattr(rsvc, "MAX_PRIMARY_MATCHES", 1)  # both ready segments "match" -> 2 > 1

    with caplog.at_level("WARNING"):
        matches = await rsvc.primary_match("anything", test_user.id, "en", "sess-1")

    assert matches == []
    assert "MAX_PRIMARY_MATCHES" in caplog.text


# ── primary_match: entity-based signal (Prompt 10 fix) ──────────────────────


async def test_primary_match_finds_segment_by_entity_name_alone(
    test_user, producer_segments, retrieval_session_factory, monkeypatch
):
    """No topic overlap at all — only the entity-name signal should surface
    the segment. This is the exact gap the QA harness found: "tell me about
    Gila" not matching because topic classification doesn't produce person
    names."""
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value="unrelated-topic"))
    monkeypatch.setattr(
        rsvc, "_extract_entity_names_from_question", AsyncMock(return_value=["Gila"])
    )
    monkeypatch.setattr(rsvc, "_embed_question_for_primary_match", AsyncMock(return_value=None))
    monkeypatch.setattr(
        rsvc.entity_store,
        "get_entity_candidates",
        AsyncMock(return_value=[{"uuid": "u1", "name": "Gila", "summary": ""}]),
    )
    mock_find_related = AsyncMock(return_value=[producer_segments["matching"].id])
    monkeypatch.setattr(rsvc.entity_store, "find_segments_mentioning", mock_find_related)

    matches = await rsvc.primary_match("Tell me about Gila", test_user.id, "en", "sess-1")

    assert [s.id for s in matches] == [producer_segments["matching"].id]
    assert mock_find_related.call_args.kwargs["entity_names"] == ["Gila"]
    assert mock_find_related.call_args.kwargs["producer_id"] == test_user.id


async def test_primary_match_unions_topic_and_entity_signals_without_duplicates(
    test_user, producer_segments, retrieval_session_factory, monkeypatch
):
    """Topic matches 'matching', entity resolution ALSO resolves to
    'matching' (e.g. the question both overlaps on topic and names someone
    in it) — must appear once, not twice."""
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value="military service"))
    monkeypatch.setattr(
        rsvc, "_extract_entity_names_from_question", AsyncMock(return_value=["Gila"])
    )
    monkeypatch.setattr(rsvc, "_embed_question_for_primary_match", AsyncMock(return_value=None))
    monkeypatch.setattr(
        rsvc.entity_store,
        "get_entity_candidates",
        AsyncMock(return_value=[{"uuid": "u1", "name": "Gila", "summary": ""}]),
    )
    monkeypatch.setattr(
        rsvc.entity_store,
        "find_segments_mentioning",
        AsyncMock(return_value=[producer_segments["matching"].id]),
    )

    matches = await rsvc.primary_match("Tell me about Gila in the army", test_user.id, "en", "sess-1")

    assert [s.id for s in matches] == [producer_segments["matching"].id]


async def test_primary_match_entity_extraction_ignores_unresolved_names(
    test_user, producer_segments, retrieval_session_factory, monkeypatch
):
    """A name extracted from the question that doesn't lexically match any
    real graph node must not reach find_related_episodes at all — resolving
    against nothing should behave like no entity signal, not an error."""
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value=None))
    monkeypatch.setattr(
        rsvc, "_extract_entity_names_from_question", AsyncMock(return_value=["Nobody"])
    )
    monkeypatch.setattr(rsvc, "_embed_question_for_primary_match", AsyncMock(return_value=None))
    monkeypatch.setattr(rsvc.entity_store, "get_entity_candidates", AsyncMock(return_value=[]))
    mock_find_related = AsyncMock()
    monkeypatch.setattr(rsvc.entity_store, "find_segments_mentioning", mock_find_related)

    matches = await rsvc.primary_match("Tell me about Nobody", test_user.id, "en", "sess-1")

    assert matches == []
    mock_find_related.assert_not_called()


# ── primary_match: semantic (embedding) signal (Prompt 10 fix) ──────────────


async def test_primary_match_finds_segment_by_semantic_similarity_alone(
    test_user, producer_segments, retrieval_session_factory, db_session, monkeypatch
):
    """No topic overlap, no named entity — only the embedding-similarity
    signal should surface the segment. This is the "wedding"/"wife"
    phrasing-gap fix: a question whose topic classification doesn't
    literal-match the segment's topic_tags, but whose semantic content
    clearly is the same thing."""
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value="unrelated-topic"))
    monkeypatch.setattr(rsvc, "_extract_entity_names_from_question", AsyncMock(return_value=[]))
    matching = producer_segments["matching"]
    matching.embedding = [1.0, 0.0, 0.0]
    db_session.add(matching)
    await db_session.commit()
    monkeypatch.setattr(
        rsvc, "_embed_question_for_primary_match", AsyncMock(return_value=[1.0, 0.0, 0.0])
    )

    matches = await rsvc.primary_match("Tell me about your wedding", test_user.id, "en", "sess-1")

    assert [s.id for s in matches] == [matching.id]


async def test_primary_match_semantic_signal_respects_threshold(
    test_user, producer_segments, retrieval_session_factory, db_session, monkeypatch
):
    """A question embedding that's similar-but-not-similar-ENOUGH to any
    segment (below SEMANTIC_MATCH_THRESHOLD) must not surface it — this is
    exactly what separates the calibrated threshold from the "everything
    is somewhat similar" noise floor real embeddings showed."""
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value=None))
    monkeypatch.setattr(rsvc, "_extract_entity_names_from_question", AsyncMock(return_value=[]))
    matching = producer_segments["matching"]
    matching.embedding = [0.6, 0.8, 0.0]
    db_session.add(matching)
    await db_session.commit()
    # cos([1,0,0], [0.6,0.8,0]) = 0.6, comfortably below the 0.68 threshold
    monkeypatch.setattr(
        rsvc, "_embed_question_for_primary_match", AsyncMock(return_value=[1.0, 0.0, 0.0])
    )

    matches = await rsvc.primary_match("Something vaguely related", test_user.id, "en", "sess-1")

    assert matches == []


async def test_primary_match_semantic_signal_ignores_segments_without_embedding(
    test_user, producer_segments, retrieval_session_factory, monkeypatch
):
    """producer_segments' fixture segments never set .embedding — the
    semantic signal must skip them (treat as no signal for that segment)
    rather than erroring on a None vs list comparison."""
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value=None))
    monkeypatch.setattr(rsvc, "_extract_entity_names_from_question", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        rsvc, "_embed_question_for_primary_match", AsyncMock(return_value=[1.0, 0.0, 0.0])
    )

    matches = await rsvc.primary_match("Anything", test_user.id, "en", "sess-1")

    assert matches == []


# ── expand_graph ─────────────────────────────────────────────────────────────


async def test_expand_graph_no_entities_returns_empty(producer_segments, monkeypatch):
    monkeypatch.setattr(
        rsvc.entity_store, "get_entity_names_for_segments", AsyncMock(return_value={})
    )
    result = await rsvc.expand_graph([producer_segments["matching"]], set(), "g1")
    assert result == []


async def test_expand_graph_excludes_visited_and_primary_ids(
    producer_segments, retrieval_session_factory, monkeypatch
):
    primary = producer_segments["matching"]
    monkeypatch.setattr(
        rsvc.entity_store,
        "get_entity_names_for_segments",
        AsyncMock(return_value={"any-seg": ["Gila"]}),
    )
    mock_find = AsyncMock(return_value=[])
    monkeypatch.setattr(rsvc.entity_store, "find_segments_mentioning_scored", mock_find)

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
        rsvc.entity_store,
        "get_entity_names_for_segments",
        AsyncMock(return_value={"any-seg": ["Gila"]}),
    )
    monkeypatch.setattr(
        rsvc.entity_store,
        "find_segments_mentioning_scored",
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
        rsvc.entity_store,
        "get_entity_names_for_segments",
        AsyncMock(return_value={"any-seg": ["Gila"]}),
    )
    monkeypatch.setattr(
        rsvc.entity_store,
        "find_segments_mentioning_scored",
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
    monkeypatch.setattr(rsvc, "_extract_entity_names_from_question", AsyncMock(return_value=[]))
    monkeypatch.setattr(rsvc, "_embed_question_for_primary_match", AsyncMock(return_value=None))
    result = await rsvc.retrieve("??", test_user.id, "en", "sess-1")
    assert result.primary == []
    assert result.candidates == []


async def test_retrieve_reads_visited_set_but_never_writes_it(
    test_user, producer_segments, retrieval_session_factory, monkeypatch
):
    monkeypatch.setattr(rsvc, "_classify_topic", AsyncMock(return_value="military service"))
    monkeypatch.setattr(rsvc, "_extract_entity_names_from_question", AsyncMock(return_value=[]))
    monkeypatch.setattr(rsvc, "_embed_question_for_primary_match", AsyncMock(return_value=None))
    monkeypatch.setattr(
        rsvc.entity_store, "get_entity_names_for_segments", AsyncMock(return_value={})
    )
    mock_get_visited = AsyncMock(return_value=set())
    mock_add_visited = AsyncMock()
    monkeypatch.setattr(rsvc.cache_service, "get_visited", mock_get_visited)
    monkeypatch.setattr(rsvc.cache_service, "add_visited", mock_add_visited)

    result = await rsvc.retrieve("Tell me about the army", test_user.id, "en", "sess-1")

    assert [s.segment_id for s in result.primary] == [producer_segments["matching"].id]
    mock_get_visited.assert_awaited_once_with("sess-1")
    mock_add_visited.assert_not_called()
