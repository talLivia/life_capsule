"""
Tests for analysis_graph.py (Prompt 5).

No real Postgres/Anthropic here — node-level tests mock llm_service /
entity_store / storage_service / stt_service directly, and the DB layer is
retargeted at the same in-memory SQLite engine the rest of the test suite
uses (analysis_graph.py normally opens sessions via app.database's module-
level AsyncSessionLocal, bypassing FastAPI's DI, so it needs its own
monkeypatch rather than the `client`/`db_session` override). The
human_confirm interrupt/resume path is exercised end-to-end against a real
LangGraph InMemorySaver, which behaves identically to AsyncPostgresSaver
from the graph's point of view — only the storage backend differs.
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import analysis_graph as ag
from app.models import Entity, EntityMention, InterviewSession, RawSegment
from app.services import entity_extraction, entity_store
from app.services.entity_names import normalize_entity_name
from app.services.entity_extraction import ExtractedEntity

pytestmark = pytest.mark.asyncio


def _extraction_reply(*names):
    """The structured extraction's reply, as the LLM would produce it."""
    import json

    return json.dumps(
        [
            {
                "name": name,
                "type": "person",
                "alternative_type": None,
                "summary": f"{name} in this recording",
            }
            for name in names
        ]
    )


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


def test_apply_entity_resolutions_without_resolutions_changes_nothing():
    entities = [ExtractedEntity(name="Gila", type="person")]
    assert ag._apply_entity_resolutions(entities, {}) == entities


def test_apply_entity_resolutions_renames_onto_the_confirmed_entity():
    """In Postgres the merge is not a suggestion to a graph engine — it is
    UNIQUE (producer_id, normalized_name). So confirming that "Moshe" is the
    existing "Moshe Cohen" is applied by writing the entity under the fuller
    name, which lands it on that row by the merge key."""
    resolved = ag._apply_entity_resolutions(
        [ExtractedEntity(name="Moshe", type="person", summary="an army friend")],
        {"Moshe": {"same_as_uuid": "uuid-1", "resolved_name": "Moshe Cohen"}},
    )
    assert resolved[0].name == "Moshe Cohen"
    # Only the name is redirected; what THIS recording said is untouched.
    assert resolved[0].summary == "an army friend"
    assert resolved[0].type == "person"


def test_apply_entity_resolutions_leaves_a_someone_new_answer_alone():
    resolved = ag._apply_entity_resolutions(
        [ExtractedEntity(name="Moshe", type="person")],
        {"Moshe": {"same_as_uuid": None, "resolved_name": "Moshe"}},
    )
    assert resolved[0].name == "Moshe"


def test_apply_entity_resolutions_ignores_resolutions_for_other_names():
    resolved = ag._apply_entity_resolutions(
        [ExtractedEntity(name="Gila", type="person")],
        {"Moshe": {"same_as_uuid": "uuid-1", "resolved_name": "Moshe Cohen"}},
    )
    assert resolved[0].name == "Gila"


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

    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value=_extraction_reply("carpenter")))
    monkeypatch.setattr(ag.entity_store, "get_entity_candidates", AsyncMock(return_value=[]))

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


async def test_extract_topics_node_titles_the_moment_at_save(
    db_session, segment, monkeypatch
):
    """§1.10 — the ONE title generation point. Written in the same run that
    writes topic_tags; the recording screen and the timeline read the stored
    value as-is, and nothing regenerates it afterwards."""

    def dispatch(messages, system_prompt=None, **kwargs):
        if "title" in (system_prompt or ""):
            return "הבית הקטן של סבתא גילה"
        return '["childhood"]'

    monkeypatch.setattr(
        ag.llm_service, "generate_response", AsyncMock(side_effect=dispatch)
    )

    await ag.extract_topics_node(
        {"segment_id": segment.id, "transcript": segment.transcript}
    )

    await db_session.refresh(segment)
    assert segment.topic_tags == ["childhood"]
    assert segment.moment_title == "הבית הקטן של סבתא גילה"


async def test_a_failed_title_costs_the_title_never_the_tags(
    db_session, segment, monkeypatch
):
    """One attempt per save, accepted in §1.10 — an untitled moment falls
    back to its take label on screen, and the tags must land regardless."""

    def dispatch(messages, system_prompt=None, **kwargs):
        if "title" in (system_prompt or ""):
            raise RuntimeError("down")
        return '["childhood"]'

    monkeypatch.setattr(
        ag.llm_service, "generate_response", AsyncMock(side_effect=dispatch)
    )

    await ag.extract_topics_node(
        {"segment_id": segment.id, "transcript": segment.transcript}
    )

    await db_session.refresh(segment)
    assert segment.topic_tags == ["childhood"]
    assert segment.moment_title is None


async def test_check_entities_node_auto_resolves_settled_exact_match(segment, monkeypatch):
    """A verbatim match the producer has ALREADY confirmed merges silently.

    The other half of always-asking, and the half that makes it bearable: once
    "who is this Gila" has been answered, every later recording that mentions
    her must go through without a question.
    """
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value=_extraction_reply("Gila")))
    monkeypatch.setattr(
        ag.entity_store,
        "get_entity_candidates",
        AsyncMock(
            return_value=[
                {
                    "uuid": "u1",
                    "name": "Gila",
                    "summary": "grandmother",
                    "identity_asked": True,
                }
            ]
        ),
    )

    result = await ag.check_entities_node(
        {"segment_id": segment.id, "group_id": "g1", "transcript": segment.transcript}
    )

    assert result["names_to_check"] == []
    assert result["entity_resolutions"] == {"Gila": {"same_as_uuid": "u1", "resolved_name": "Gila"}}


async def test_check_entities_node_asks_about_unsettled_exact_match(segment, monkeypatch):
    """THE אמנון BUG. A name matching verbatim is not proof of one person.

    This auto-merged, on the assumption that an identical name meant an
    identical person — so an uncle and an army friend both called אמנון landed
    on one row across three recordings with no question ever asked. The merge
    key IS the name, so that merge is not a guess to revisit later; it is the
    loss of the distinction.
    """
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value=_extraction_reply("אמנון")))
    monkeypatch.setattr(
        ag.entity_store,
        "get_entity_candidates",
        AsyncMock(
            return_value=[
                {
                    "uuid": "u1",
                    "name": "אמנון",
                    "type": "person",
                    "summary": "דודו של הדובר מצד אבא",
                    "identity_asked": False,
                }
            ]
        ),
    )

    result = await ag.check_entities_node(
        {"segment_id": segment.id, "group_id": "g1", "transcript": segment.transcript}
    )

    assert result["entity_resolutions"] == {}
    assert [q["name"] for q in result["names_to_check"]] == ["אמנון"]
    question = result["names_to_check"][0]
    assert question["candidates"] == [
        {"uuid": "u1", "name": "אמנון", "summary": "דודו של הדובר מצד אבא"}
    ]
    # Worded for the case, not the generic "is X the same as X" that reads as
    # a typo and invites a reflexive yes — the answer that merges two people.
    assert "דודו של הדובר מצד אבא" in question["question"]
    assert "someone else" in question["question"]


async def test_a_place_is_not_asked_whether_it_is_the_same_person(segment, monkeypatch):
    """Four of the five entity types are not people.

    Worth its own test because places are where this fires MOST: תל אביב is
    named in as many recordings as any relative, so "Is this the same person?"
    about a city would be the first thing a producer saw.
    """
    monkeypatch.setattr(
        ag.llm_service,
        "generate_response",
        AsyncMock(
            return_value=json.dumps(
                [{"name": "תל אביב", "type": "place", "alternative_type": None, "summary": "s"}]
            )
        ),
    )
    monkeypatch.setattr(
        ag.entity_store,
        "get_entity_candidates",
        AsyncMock(
            return_value=[
                {
                    "uuid": "p1",
                    "name": "תל אביב",
                    "type": "place",
                    "summary": "where the speaker grew up",
                    "identity_asked": False,
                }
            ]
        ),
    )

    result = await ag.check_entities_node(
        {"segment_id": segment.id, "group_id": "g1", "transcript": segment.transcript}
    )

    question = result["names_to_check"][0]["question"]
    assert "person" not in question
    assert "the same one" in question


async def test_identity_questions_carry_their_wording(segment, monkeypatch):
    """Every identity question says something in words.

    It did not. `_confirmation_question` was called from the per-name
    interrupt, and when chunk 4 batched them `names_to_check` began going
    straight through — so the modal rendered an EMPTY legend above the
    options. Nothing failed, because the options still read sensibly on their
    own, which is why it survived. Asserted here rather than trusted.
    """
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value=_extraction_reply("Gila")))
    monkeypatch.setattr(
        ag.entity_store,
        "get_entity_candidates",
        AsyncMock(
            return_value=[
                {"uuid": "u2", "name": "Gila Cohen", "summary": "a neighbor", "identity_asked": False},
                {"uuid": "u3", "name": "Gila Levi", "summary": "", "identity_asked": True},
            ]
        ),
    )

    result = await ag.check_entities_node(
        {"segment_id": segment.id, "group_id": "g1", "transcript": segment.transcript}
    )

    question = result["names_to_check"][0]["question"]
    assert question.strip()
    assert "Gila Cohen" in question and "Gila Levi" in question


async def test_check_entities_node_flags_fuzzy_match(segment, monkeypatch):
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value=_extraction_reply("Gila")))
    monkeypatch.setattr(
        ag.entity_store,
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
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value=_extraction_reply("Moshe")))
    monkeypatch.setattr(
        ag.entity_store,
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
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value=_extraction_reply("Moshe")))
    monkeypatch.setattr(
        ag.entity_store,
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
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value=_extraction_reply("Gila")))
    monkeypatch.setattr(ag.entity_store, "get_entity_candidates", AsyncMock(return_value=[]))

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
    monkeypatch.setattr(ag.llm_service, "generate_response", AsyncMock(return_value=_extraction_reply("Dan Cohen")))
    monkeypatch.setattr(
        ag.entity_store,
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
        ag.llm_service, "generate_response", AsyncMock(return_value=_extraction_reply("Gila Cohen"))
    )
    monkeypatch.setattr(
        ag.entity_store,
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


async def test_finalize_ingest_node_writes_entities_and_marks_ready(
    db_session, segment, test_user
):
    result = await ag.finalize_ingest_node(
        {
            "segment_id": segment.id,
            "group_id": test_user.id,
            "transcript": segment.transcript,
            "topic_tags": ["childhood"],
            "extracted_entities": [
                {
                    "name": "Gila",
                    "type": "person",
                    "alternative_type": None,
                    "summary": "the speaker's grandmother",
                }
            ],
            "entity_resolutions": {},
        }
    )

    assert result["status"] == "ready"
    await db_session.refresh(segment)
    assert segment.status == "ready"

    entity = (await db_session.execute(select(Entity))).scalar_one()
    assert (entity.name, entity.type, entity.producer_id) == (
        "Gila",
        "person",
        test_user.id,
    )
    mention = (await db_session.execute(select(EntityMention))).scalar_one()
    assert mention.raw_segment_id == segment.id
    assert mention.summary == "the speaker's grandmother"


async def test_finalize_ingest_node_applies_a_confirmed_resolution(
    db_session, segment, test_user
):
    """A confirmed "Moshe is Moshe Cohen" has to reach the row that gets
    written, or the producer answered a question that changed nothing."""
    await ag.finalize_ingest_node(
        {
            "segment_id": segment.id,
            "group_id": test_user.id,
            "transcript": segment.transcript,
            "extracted_entities": [
                {"name": "Moshe", "type": "person", "alternative_type": None, "summary": None}
            ],
            "entity_resolutions": {
                "Moshe": {"same_as_uuid": "u1", "resolved_name": "Moshe Cohen"}
            },
        }
    )

    entity = (await db_session.execute(select(Entity))).scalar_one()
    assert entity.name == "Moshe Cohen"


async def test_finalize_ingest_node_marks_failed_on_error(db_session, segment, monkeypatch):
    """finalize_ingest_node itself only reports the error — the graph's
    conditional edge routes to `fail_node`, which is what actually persists
    status='failed' (see test_full_pipeline_entity_write_failure_reaches_failed
    below for that end-to-end behavior)."""
    monkeypatch.setattr(
        ag.entity_store,
        "write_segment_entities",
        AsyncMock(side_effect=RuntimeError("postgres down")),
    )

    result = await ag.finalize_ingest_node(
        {"segment_id": segment.id, "group_id": "producer-1", "transcript": segment.transcript}
    )

    assert result["status"] == "failed"
    assert "postgres down" in result["error"]
    # The segment must NOT have been marked ready behind a failed write.
    await db_session.refresh(segment)
    assert segment.status == "pending_analysis"


# ── Full-graph tests (real LangGraph, InMemorySaver) ────────────────────────


async def _mock_all_llm_calls(
    monkeypatch, *, entity_candidates, entity_name="Gila", years_settled=True
):
    """Mock the LLM calls, and by default settle every year question.

    `years_settled` exists because Phase 3 widened year capture to ANY entity
    without a year — so a recording that names one person raises a year
    question and the pipeline correctly pauses. The tests below are about
    other things, and would otherwise all stop at that pause.

    Settling by default keeps each test about its own subject. Pass False to
    assert the pause itself.
    """
    async def fake_generate(messages, system_prompt=None, thinking=False, temperature=None):
        if system_prompt == ag._EXTRACT_TOPICS_SYSTEM_PROMPT:
            return '["childhood"]'
        # startswith, not equality: the extraction prompt now carries the
        # producer's name so the model can tell which of the names in a
        # transcript is the one narrating. Identifying a call by exact
        # prompt text was always brittle; it breaks the moment the prompt
        # legitimately varies per producer.
        if (system_prompt or "").startswith(
            entity_extraction._ENTITY_EXTRACTION_SYSTEM_PROMPT
        ):
            return _extraction_reply(entity_name)
        if system_prompt == ag._IMPORTANCE_SYSTEM_PROMPT:
            return "8"
        raise AssertionError(f"unexpected system_prompt: {system_prompt}")

    monkeypatch.setattr(ag.llm_service, "generate_response", fake_generate)
    monkeypatch.setattr(
        ag.entity_store, "get_entity_candidates", AsyncMock(return_value=entity_candidates)
    )
    monkeypatch.setattr(ag.embeddings, "embed_text", AsyncMock(return_value=[0.1, 0.2, 0.3]))

    async def settled(_db, _producer_id, names):
        if not years_settled:
            return set()
        return {normalize_entity_name(n) for n in names}

    monkeypatch.setattr(ag.entity_store, "names_with_year_settled", settled)


async def test_identity_is_asked_once_per_person_then_never_again(
    db_session, segment, test_user, analysis_session_factory, fake_checkpointer, monkeypatch
):
    """The whole of step 1, end to end, against a real candidate lookup.

    Deliberately does NOT mock `get_entity_candidates`: the stamp is written by
    one node and read by another through the database, and a mocked lookup
    would assert the two halves in isolation while proving nothing about the
    round trip — which is the only thing that decides whether the second
    recording interrupts.

    Both halves matter and they pull in opposite directions. Asking is what
    stops two people with one name silently merging; STOPPING asking is what
    keeps that from becoming a question on every recording that mentions
    anybody, which is how a safeguard turns into something answered without
    reading.
    """
    db_session.add(
        Entity(
            producer_id=test_user.id,
            name="Gila",
            normalized_name=normalize_entity_name("Gila"),
            type="person",
        )
    )
    await db_session.commit()

    # Captured BEFORE the mock goes on, because `ag.entity_store` and
    # `entity_store` are the same module object — reading the attribute back
    # afterwards would restore the mock onto itself.
    real_candidates = entity_store.get_entity_candidates
    await _mock_all_llm_calls(monkeypatch, entity_candidates=[])
    monkeypatch.setattr(ag.entity_store, "get_entity_candidates", real_candidates)

    result = await ag.run_segment_analysis(segment.id)

    assert "__interrupt__" in result, "an unconfirmed verbatim match must ask"
    payload = result["__interrupt__"][0].value
    assert [q["name"] for q in payload["identity_questions"]] == ["Gila"]

    await ag.resume_segment_analysis(
        segment.id,
        {"identity": {"Gila": {"same_as_existing": True}}},
    )

    gila = (
        await db_session.execute(select(Entity).where(Entity.name == "Gila"))
    ).scalars().one()
    await db_session.refresh(gila)
    assert gila.identity_asked_at is not None, "answering settles who this row is"

    # A SECOND recording naming the same person. Nothing about it is unclear
    # any more, so it must run straight through.
    second = RawSegment(
        interview_session_id=segment.interview_session_id,
        question_asked="What else do you remember about her?",
        question_index=1,
        video_key=f"segments/{test_user.id}/x/1/take.webm",
        transcript="Gila taught me to bake.",
        status="pending_analysis",
    )
    db_session.add(second)
    await db_session.commit()
    await db_session.refresh(second)

    again = await ag.run_segment_analysis(second.id)

    assert "__interrupt__" not in again, "a settled person must not be asked about twice"
    await db_session.refresh(second)
    assert second.status == "ready"


async def test_mark_identity_asked_is_scoped_and_set_once(db_session, test_user):
    """Only sets, never clears, and never reaches another producer's rows.

    Both properties are load-bearing rather than defensive. Re-stamping would
    move the timestamp on every later recording, which is harmless now but
    turns the column into "last mentioned" the first time anything reads it as
    a date. Producer scoping is because this is a WRITE keyed by an id that
    arrives from pipeline state.
    """
    from datetime import datetime, timezone

    mine = Entity(
        producer_id=test_user.id,
        name="Gila",
        normalized_name=normalize_entity_name("Gila"),
        type="person",
        identity_asked_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    fresh = Entity(
        producer_id=test_user.id,
        name="Moshe",
        normalized_name=normalize_entity_name("Moshe"),
        type="person",
    )
    db_session.add_all([mine, fresh])
    await db_session.commit()

    stamped = await entity_store.mark_identity_asked(
        db_session, test_user.id, [mine.id, fresh.id]
    )
    await db_session.commit()

    assert stamped == 1, "only the one that had no stamp"
    await db_session.refresh(mine)
    await db_session.refresh(fresh)
    # Compared naive: SQLite has no timezone type and drops the offset on the
    # way back. Postgres, which is what runs, keeps it.
    assert mine.identity_asked_at.replace(tzinfo=None) == datetime(2020, 1, 1)
    assert fresh.identity_asked_at is not None

    # Another producer's id, passed by a caller that should not be trusted.
    someone_else = await entity_store.mark_identity_asked(
        db_session, "not-this-producer", [fresh.id]
    )
    assert someone_else == 0


async def test_human_confirm_stamps_the_existing_row_not_the_new_one(segment):
    """"Someone different" settles the OLD person, and only them.

    The distinction is the reason this is carried as ids rather than derived
    from the entities that get written. Told "this is a different אמנון, call
    him אמנון נחום", the archive now holds two rows — and only the first has
    been confirmed. Stamping the new one would declare a question settled that
    its producer has never been asked.

    `pending:` candidates are excluded for the same reason: they have no row.
    """
    ag.interrupt = lambda payload: {
        "identity": {
            "אמנון": {"same_as_existing": False, "new_name": "אמנון נחום"},
        }
    }
    try:
        result = await ag.human_confirm_node(
            {
                "segment_id": segment.id,
                "group_id": "g1",
                "names_to_check": [
                    {
                        "name": "אמנון",
                        "question": "?",
                        "candidates": [
                            {"uuid": "real-row", "name": "אמנון", "summary": "דוד"},
                            {
                                "uuid": f"{entity_store.PENDING_CANDIDATE_PREFIX}אמנון",
                                "name": "אמנון",
                                "summary": "",
                            },
                        ],
                    }
                ],
                "extracted_entities": [
                    {"name": "אמנון", "type": "person", "alternative_type": None,
                     "summary": "s"}
                ],
            }
        )
    finally:
        import langgraph.types

        ag.interrupt = langgraph.types.interrupt

    assert result["asked_identity_ids"] == ["real-row"]
    # And the recording's own entity is written under the distinguishing name,
    # so the merge key holds them apart.
    assert [e["name"] for e in result["extracted_entities"]] == ["אמנון נחום"]


async def test_full_pipeline_no_ambiguity_reaches_ready(
    db_session, segment, analysis_session_factory, fake_checkpointer, monkeypatch
):
    await _mock_all_llm_calls(monkeypatch, entity_candidates=[])

    result = await ag.run_segment_analysis(segment.id)

    assert "__interrupt__" not in result
    await db_session.refresh(segment)
    assert segment.status == "ready"
    assert segment.topic_tags == ["childhood"]
    assert segment.importance_score == 8.0
    assert segment.embedding == [0.1, 0.2, 0.3]

    # The entity the extraction found is in Postgres, with the summary THIS
    # recording produced attached to the mention rather than to the entity.
    entity = (await db_session.execute(select(Entity))).scalar_one()
    assert entity.name == "Gila"
    mention = (await db_session.execute(select(EntityMention))).scalar_one()
    assert mention.entity_id == entity.id
    assert mention.raw_segment_id == segment.id
    assert mention.summary == "Gila in this recording"


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

    await ag.run_segment_analysis(segment.id)

    await db_session.refresh(segment)
    assert segment.status == "pending_confirmation"
    pending = segment.pending_confirmation
    assert [q["name"] for q in pending["identity_questions"]] == ["Gila"]
    assert pending["identity_questions"][0]["candidates"] == [
        {"uuid": "u2", "name": "Gila Cohen", "summary": "a neighbor"}
    ]
    assert pending["type_questions"] == []

    await ag.resume_segment_analysis(
        segment.id,
        {"identity": {"Gila": {"same_as_existing": True, "candidate_uuid": "u2"}}, "types": {}},
    )

    await db_session.refresh(segment)
    assert segment.status == "ready"
    # The confirmed answer survives the pause and reaches the written row:
    # the fuller resolved name, not the bare "Gila" the extraction returned.
    entity = (await db_session.execute(select(Entity))).scalar_one()
    assert entity.name == "Gila Cohen"


async def test_full_pipeline_asks_identity_and_type_in_ONE_interrupt(
    db_session, segment, analysis_session_factory, fake_checkpointer, monkeypatch
):
    """The whole point of chunk 4. A recording raising both kinds of question
    must pause ONCE with both, not once per question — and one answer must run
    it to completion rather than pausing again."""

    async def fake_generate(messages, system_prompt=None, thinking=False, temperature=None):
        if system_prompt == ag._EXTRACT_TOPICS_SYSTEM_PROMPT:
            return '["childhood"]'
        # startswith, not equality: the extraction prompt now carries the
        # producer's name so the model can tell which of the names in a
        # transcript is the one narrating. Identifying a call by exact
        # prompt text was always brittle; it breaks the moment the prompt
        # legitimately varies per producer.
        if (system_prompt or "").startswith(
            entity_extraction._ENTITY_EXTRACTION_SYSTEM_PROMPT
        ):
            return json.dumps([
                {"name": "Gila", "type": "person", "alternative_type": None, "summary": "s"},
                {"name": "הכפר הירוק", "type": "place",
                 "alternative_type": "organisation", "summary": "s"},
            ])
        if system_prompt == ag._IMPORTANCE_SYSTEM_PROMPT:
            return "8"
        raise AssertionError(f"unexpected system_prompt: {system_prompt}")

    monkeypatch.setattr(ag.llm_service, "generate_response", fake_generate)
    monkeypatch.setattr(ag.embeddings, "embed_text", AsyncMock(return_value=[0.1]))
    monkeypatch.setattr(
        ag.entity_store,
        "get_entity_candidates",
        AsyncMock(return_value=[{"uuid": "u2", "name": "Gila Cohen", "summary": "a neighbor"}]),
    )

    await ag.run_segment_analysis(segment.id)

    await db_session.refresh(segment)
    assert segment.status == "pending_confirmation"
    pending = segment.pending_confirmation
    assert [q["name"] for q in pending["identity_questions"]] == ["Gila"]
    assert [q["name"] for q in pending["type_questions"]] == ["הכפר הירוק"]
    # Exactly two options, always — the extractor names the runner-up rather
    # than reporting a confidence score.
    assert pending["type_questions"][0]["type"] == "place"
    assert pending["type_questions"][0]["alternative_type"] == "organisation"

    await ag.resume_segment_analysis(
        segment.id,
        {
            "identity": {"Gila": {"same_as_existing": True, "candidate_uuid": "u2"}},
            "types": {"הכפר הירוק": "organisation"},
        },
    )

    await db_session.refresh(segment)
    assert segment.status == "ready", "one answer runs it to completion — it never pauses again"

    entities = {
        e.name: e.type
        for e in (await db_session.execute(select(Entity))).scalars().all()
    }
    assert entities == {"Gila Cohen": "person", "הכפר הירוק": "organisation"}


async def test_a_recording_that_raises_nothing_at_all_never_pauses(
    db_session, segment, analysis_session_factory, fake_checkpointer, monkeypatch
):
    """Asking about everything trains the producer to click through without
    reading, which is worse than not asking.

    RENAMED, and the rename is the point. This used to say "no ambiguity",
    meaning no identity and no type question — and it passed for months while
    relation, year and parentage questions were silently skipped, because the
    router knew about those two classes only. "No ambiguity" was never enough
    to justify not pausing; "nothing to ask, of any kind" is.
    """
    await _mock_all_llm_calls(monkeypatch, entity_candidates=[])

    result = await ag.run_segment_analysis(segment.id)

    assert "__interrupt__" not in result
    await db_session.refresh(segment)
    assert segment.status == "ready"
    assert segment.pending_confirmation is None


async def test_a_year_question_alone_is_enough_to_pause(
    db_session, segment, analysis_session_factory, fake_checkpointer, monkeypatch
):
    """The regression guard at pipeline level.

    A recording naming one person with no year and no ambiguity must still
    stop and ask. Before the router read every question class it did not, and
    the live consequence was measurable: not one entity in the archive had
    ever been asked for a year, months after year capture shipped.
    """
    await _mock_all_llm_calls(monkeypatch, entity_candidates=[], years_settled=False)

    result = await ag.run_segment_analysis(segment.id)

    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["identity_questions"] == []
    assert payload["type_questions"] == []
    assert [q["name"] for q in payload["year_questions"]] == ["Gila"]
    await db_session.refresh(segment)
    assert segment.status == "pending_confirmation"


async def test_full_pipeline_entity_write_failure_reaches_failed(
    db_session, segment, analysis_session_factory, fake_checkpointer, monkeypatch
):
    await _mock_all_llm_calls(monkeypatch, entity_candidates=[])
    monkeypatch.setattr(
        ag.entity_store,
        "write_segment_entities",
        AsyncMock(side_effect=RuntimeError("postgres down")),
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


async def _pause_with(monkeypatch, segment_id, *, extraction, candidates):
    """Run the pipeline to its pause, with a given extraction and candidate set."""

    async def fake_generate(messages, system_prompt=None, thinking=False, temperature=None):
        if system_prompt == ag._EXTRACT_TOPICS_SYSTEM_PROMPT:
            return '["childhood"]'
        # startswith, not equality: the extraction prompt now carries the
        # producer's name so the model can tell which of the names in a
        # transcript is the one narrating. Identifying a call by exact
        # prompt text was always brittle; it breaks the moment the prompt
        # legitimately varies per producer.
        if (system_prompt or "").startswith(
            entity_extraction._ENTITY_EXTRACTION_SYSTEM_PROMPT
        ):
            return json.dumps(extraction)
        if system_prompt == ag._IMPORTANCE_SYSTEM_PROMPT:
            return "8"
        raise AssertionError(f"unexpected system_prompt: {system_prompt}")

    monkeypatch.setattr(ag.llm_service, "generate_response", fake_generate)
    monkeypatch.setattr(ag.embeddings, "embed_text", AsyncMock(return_value=[0.1, 0.2, 0.3]))
    monkeypatch.setattr(
        ag.entity_store, "get_entity_candidates", AsyncMock(return_value=candidates)
    )
    await ag.run_segment_analysis(segment_id)


_MOSHE = [{"name": "Moshe", "type": "person", "alternative_type": None, "summary": "s"}]
_TWO_MOSHES = [
    {"uuid": "u1", "name": "Moshe Cohen", "summary": "army friend"},
    {"uuid": "u2", "name": "Moshe Levi", "summary": "neighbor"},
]
_MOSHE_AND_A_TYPE_QUESTION = [
    {"name": "Moshe", "type": "person", "alternative_type": None, "summary": "s"},
    {"name": "הכפר הירוק", "type": "place", "alternative_type": "organisation", "summary": "s"},
]


async def test_confirm_entities_answers_everything_in_one_call(
    client: AsyncClient, db_session, segment, analysis_session_factory,
    fake_checkpointer, auth_headers, monkeypatch,
):
    """One screen, one submit, and the recording is done — where this used to
    take one round trip per ambiguous name."""
    segment_id = segment.id
    await _pause_with(
        monkeypatch, segment_id,
        extraction=_MOSHE_AND_A_TYPE_QUESTION,
        candidates=[{"uuid": "u2", "name": "Moshe Cohen", "summary": "army friend"}],
    )
    db_session.expire_all()

    body = (await client.get(
        "/api/v1/interview/segments/pending-confirmations", headers=auth_headers
    )).json()
    assert len(body) == 1
    payload = body[0]["pending_confirmation"]
    assert [q["name"] for q in payload["identity_questions"]] == ["Moshe"]
    assert [q["name"] for q in payload["type_questions"]] == ["הכפר הירוק"]

    resp = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entities",
        json={
            "identity": {"Moshe": {"same_as_existing": True, "candidate_uuid": "u2"}},
            "types": {"הכפר הירוק": "organisation"},
        },
        headers=auth_headers,
    )
    db_session.expire_all()
    assert resp.status_code == 200
    # confirm-entities returns {segment, applied_type_changes} — the changes
    # are how a producer's type answer is shown to have taken effect, rather
    # than being accepted and silently discarded.
    assert resp.json()["segment"]["status"] == "ready"

    after = await client.get(
        "/api/v1/interview/segments/pending-confirmations", headers=auth_headers
    )
    assert after.json() == [], "one submit clears the recording entirely"


async def test_confirm_entities_rejects_a_partial_submit(
    client: AsyncClient, db_session, segment, analysis_session_factory,
    fake_checkpointer, auth_headers, monkeypatch,
):
    """Both plausible defaults are wrong in opposite directions — "same"
    silently merges two people, "new" silently splits one — so an unanswered
    question is a 400 rather than a guess."""
    segment_id = segment.id
    await _pause_with(
        monkeypatch, segment_id,
        extraction=_MOSHE_AND_A_TYPE_QUESTION,
        candidates=[{"uuid": "u2", "name": "Moshe Cohen", "summary": "army friend"}],
    )
    db_session.expire_all()

    resp = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entities",
        json={"identity": {"Moshe": {"same_as_existing": False}}, "types": {}},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "הכפר הירוק" in resp.json()["detail"]

    # Still pending — a rejected submit must not consume the pause.
    db_session.expire_all()
    still = await client.get(
        "/api/v1/interview/segments/pending-confirmations", headers=auth_headers
    )
    assert len(still.json()) == 1


async def test_extraction_unlocks_when_the_pipeline_pauses_for_answers(
    client: AsyncClient, db_session, segment, analysis_session_factory,
    fake_checkpointer, auth_headers, monkeypatch,
):
    """A paused pipeline must read as NOT processing.

    "Still working on it" and "waiting on you" are different states and the
    frontend renders them differently: `awaiting_confirmation` points at the
    bell, `still_processing` shows a progress bar and polls until it clears.
    Were `pending_confirmation` ever counted as processing, a recording paused
    on a question would show both at once — a progress bar claiming work is in
    flight next to a note saying it is waiting for an answer — and poll for as
    long as the panel stayed open, because nothing would ever finish.

    Pinned because it is a SERVER fact the client has no way to second-guess.
    It mattered more before: the extraction screen used to LOCK on this flag
    while it held the producer in place waiting to hand off to the confirmation
    popup, so a wrong answer here trapped them on a screen with no way out.
    Both the lock and the handoff are gone (docs/GUIDED_INTERVIEW.md §14), and
    the flag still has to be right.
    """
    segment_id = segment.id
    await _pause_with(
        monkeypatch, segment_id,
        extraction=_MOSHE_AND_A_TYPE_QUESTION,
        candidates=[{"uuid": "u2", "name": "Moshe Cohen", "summary": "army friend"}],
    )
    db_session.expire_all()

    body = (await client.get(
        f"/api/v1/interview/segments/{segment_id}/extraction", headers=auth_headers
    )).json()

    assert body["status"] == "pending_confirmation"
    assert body["awaiting_confirmation"] is True
    assert body["still_processing"] is False, (
        "the extraction screen locks on this flag and its handoff is gone"
    )


async def test_confirm_entities_rejects_a_stale_answer(
    client: AsyncClient, db_session, segment, analysis_session_factory,
    fake_checkpointer, auth_headers, monkeypatch,
):
    """Answering a name this screen never asked about means the client is
    working from a payload the pipeline has moved past."""
    segment_id = segment.id
    await _pause_with(monkeypatch, segment_id, extraction=_MOSHE, candidates=_TWO_MOSHES)
    db_session.expire_all()

    resp = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entities",
        json={"identity": {"SomeoneElse": {"same_as_existing": True}}, "types": {}},
        headers=auth_headers,
    )
    assert resp.status_code == 409


async def test_confirm_entities_multi_candidate_validation(
    client: AsyncClient, db_session, segment, analysis_session_factory,
    fake_checkpointer, auth_headers, monkeypatch,
):
    """A bare "Moshe" ambiguous against two existing entities surfaces both,
    rejects an omitted candidate_uuid (no single-candidate default applies
    with 2+), rejects an unlisted one, and succeeds when the right one is
    named."""
    segment_id = segment.id
    await _pause_with(monkeypatch, segment_id, extraction=_MOSHE, candidates=_TWO_MOSHES)
    db_session.expire_all()

    payload = (await client.get(
        "/api/v1/interview/segments/pending-confirmations", headers=auth_headers
    )).json()[0]["pending_confirmation"]
    candidates = payload["identity_questions"][0]["candidates"]
    assert {c["uuid"] for c in candidates} == {"u1", "u2"}

    for bad in ({"same_as_existing": True}, {"same_as_existing": True, "candidate_uuid": "nope"}):
        resp = await client.post(
            f"/api/v1/interview/segments/{segment_id}/confirm-entities",
            json={"identity": {"Moshe": bad}, "types": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 400, bad

    resp = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entities",
        json={"identity": {"Moshe": {"same_as_existing": True, "candidate_uuid": "u1"}},
              "types": {}},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    # confirm-entities returns {segment, applied_type_changes} — the changes
    # are how a producer's type answer is shown to have taken effect, rather
    # than being accepted and silently discarded.
    assert resp.json()["segment"]["status"] == "ready"


async def test_confirm_entities_rejects_a_type_outside_the_two_offered(
    client: AsyncClient, db_session, segment, analysis_session_factory,
    fake_checkpointer, auth_headers, monkeypatch,
):
    """A third value could only come from a client inventing one, and it would
    land in a column with a CHECK constraint on it."""
    segment_id = segment.id
    await _pause_with(
        monkeypatch, segment_id,
        extraction=[{"name": "הכפר הירוק", "type": "place",
                     "alternative_type": "organisation", "summary": "s"}],
        candidates=[],
    )
    db_session.expire_all()

    resp = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entities",
        json={"identity": {}, "types": {"הכפר הירוק": "event"}},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "must be one of" in resp.json()["detail"]


async def test_confirm_entities_409_when_nothing_is_pending(
    client: AsyncClient, segment, auth_headers
):
    resp = await client.post(
        f"/api/v1/interview/segments/{segment.id}/confirm-entities",
        json={"identity": {}, "types": {}},
        headers=auth_headers,
    )
    assert resp.status_code == 409


def test_type_question_gets_the_article_right_in_both_slots():
    """The runner-up is as likely to be the vowel-initial one, so both slots
    need the article — not just the second."""
    q = ag._type_question({"name": "X", "type": "organisation", "alternative_type": "place"})
    assert q == 'Is "X" an organisation or a place?'
    q = ag._type_question({"name": "X", "type": "place", "alternative_type": "organisation"})
    assert q == 'Is "X" a place or an organisation?'


def test_type_questions_only_covers_entities_the_extractor_was_torn_about():
    """Asking about everything trains the producer to click through without
    reading, which is worse than not asking."""
    questions = ag.type_questions([
        {"name": "ניר", "type": "person", "alternative_type": None},
        {"name": "הכפר הירוק", "type": "place", "alternative_type": "organisation"},
    ])
    assert [q["name"] for q in questions] == ["הכפר הירוק"]


def test_type_questions_offers_exactly_the_two_the_extractor_named():
    q = ag.type_questions(
        [{"name": "X", "type": "place", "alternative_type": "organisation"}]
    )[0]
    assert (q["type"], q["alternative_type"]) == ("place", "organisation")


async def test_a_relation_correction_must_name_an_offered_type(
    client: AsyncClient, db_session, segment, analysis_session_factory,
    fake_checkpointer, auth_headers, monkeypatch,
):
    """An invented relation type is REFUSED, not dropped.

    The FK on entity_relations.relation_type is the backstop, but a correction
    that silently does nothing is exactly the bug this feature exists to fix —
    hitting it again on the fix itself would be maddening.
    """
    segment_id = segment.id
    await _pause_with(
        monkeypatch, segment_id, extraction=_MOSHE,
        candidates=[{"uuid": "u2", "name": "Moshe Cohen", "summary": "army friend"}],
    )
    db_session.expire_all()

    resp = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entities",
        json={
            "identity": {"Moshe": {"same_as_existing": False}},
            "types": {},
            "relation_edits": {
                "0": {
                    "relation_type": "brother-ish",
                    "from_name": "__SELF__",
                    "to_name": "Moshe",
                }
            },
        },
        headers=auth_headers,
    )
    # Either the proposal index is not one this recording raised (409) or the
    # type is not one the question offered (400). Both are a loud refusal,
    # which is the property under test.
    assert resp.status_code in (400, 409)
    assert "brother-ish" in resp.text or "not questions" in resp.text.lower()


async def test_identity_asks_about_a_person_from_an_unanswered_recording(
    db_session, test_user, segment, analysis_session_factory, monkeypatch
):
    """The איציק / איציק כהן case, from the live archive.

    Entities are written by finalize_ingest, which runs AFTER the confirmation
    pause. Measured on real data: a recording naming "איציק" sat paused for 91
    seconds waiting on a human, and a second recording naming "איציק כהן" ran
    its identity check inside that window. It matched nothing, asked nothing,
    and created a second person — so two stories about one man had no link
    between them and nothing ever offered to make one.

    Waiting for the earlier write cannot fix it: that write is downstream of
    the human. So the unanswered recording's OWN proposed names are offered as
    candidates instead.
    """
    from app.models import RawSegment

    # An earlier recording, still waiting on the producer, that named איציק.
    earlier = RawSegment(
        interview_session_id=segment.interview_session_id,
        question_asked="army?",
        question_index=1,
        video_key="k",
        transcript="היה לי חבר בשם איציק",
        status="pending_confirmation",
        pending_confirmation={
            "identity_questions": [],
            "type_questions": [
                {"name": "חיל האוויר", "type": "organisation",
                 "alternative_type": "place", "question": "?"}
            ],
            "editable_entities": [{"name": "איציק"}, {"name": "חיל האוויר"}],
        },
    )
    db_session.add(earlier)
    await db_session.commit()

    async def fake_generate(messages, system_prompt=None, thinking=False, temperature=None):
        return json.dumps(
            [{"name": "איציק כהן", "type": "person",
              "alternative_type": None, "summary": "s"}]
        )

    monkeypatch.setattr(ag.llm_service, "generate_response", fake_generate)

    result = await ag.check_entities_node(
        {
            "segment_id": segment.id,
            "group_id": test_user.id,
            "transcript": "נסעתי לקולומביה עם איציק כהן",
        }
    )

    asked = {q["name"]: q for q in result["names_to_check"]}
    assert "איציק כהן" in asked, (
        "the identity question must fire even though איציק has no entity row "
        "yet — the recording that names him is still awaiting confirmation"
    )
    candidates = asked["איציק כהן"]["candidates"]
    assert "איציק" in [c["name"] for c in candidates]
    # The id is a marker, never a real entity id: _apply_entity_resolutions
    # uses it only as a boolean gate and applies the answer by RENAMING.
    pending = next(c for c in candidates if c["name"] == "איציק")
    assert pending["uuid"].startswith(entity_store.PENDING_CANDIDATE_PREFIX)


async def test_answering_yes_to_a_pending_candidate_renames_onto_it(db_session):
    """The answer must still land both recordings on ONE row.

    A candidate with no row would be useless if the answer needed its id. It
    does not: the resolution carries a NAME, and the merge key does the rest
    whenever that row is created.
    """
    resolved = ag._apply_entity_resolutions(
        [ExtractedEntity(name="איציק כהן", type="person", summary="s")],
        {
            "איציק כהן": {
                "same_as_uuid": f"{entity_store.PENDING_CANDIDATE_PREFIX}איציק",
                "resolved_name": "איציק",
            }
        },
    )
    assert resolved[0].name == "איציק", "written under the other recording's name"


async def test_a_crashed_run_is_marked_failed_not_left_stranded(
    db_session, segment, analysis_session_factory, fake_checkpointer, monkeypatch
):
    """A crash must leave a VISIBLE state, not an invisible one.

    _sync_segment_from_result is the only thing that moves a segment off its
    initial status, and it runs after the graph returns. When the run died the
    row kept `pending_transcription` while progress_stage showed how far it
    got — a state that appears nowhere: /talk loads `ready`, the bell queries
    `pending_confirmation`, and this is neither. Seen on a real recording,
    found only because the producer noticed the content was missing.
    """
    segment.status = "pending_transcription"
    segment.progress_stage = "human_confirm"
    await db_session.commit()

    async def boom(*a, **kw):
        raise RuntimeError("checkpointer connection dropped")

    monkeypatch.setattr(ag, "build_graph", lambda checkpointer: type(
        "G", (), {"ainvoke": staticmethod(boom)}
    )())

    with pytest.raises(RuntimeError):
        await ag.run_segment_analysis(segment.id)

    db_session.expire_all()
    await db_session.refresh(segment)
    assert segment.status == "failed", "a crashed run must not stay invisible"
    assert segment.progress_stage is None


async def test_a_crash_never_downgrades_a_finished_segment(
    db_session, segment, analysis_session_factory, fake_checkpointer, monkeypatch
):
    """A late crash must not overwrite a status that already settled — a
    recording that reached `ready` is done, whatever happens on the way out."""
    segment.status = "ready"
    await db_session.commit()

    async def boom(*a, **kw):
        raise RuntimeError("late failure")

    monkeypatch.setattr(ag, "build_graph", lambda checkpointer: type(
        "G", (), {"ainvoke": staticmethod(boom)}
    )())

    with pytest.raises(RuntimeError):
        await ag.run_segment_analysis(segment.id)

    db_session.expire_all()
    await db_session.refresh(segment)
    assert segment.status == "ready"



async def test_someone_new_about_an_identical_name_requires_a_distinguishing_one(
    client: AsyncClient, db_session, segment, analysis_session_factory,
    fake_checkpointer, auth_headers, monkeypatch,
):
    """The silent-discard this closes, and how it was found.

    One entity row ended up holding both an uncle and an army friend, because
    the merge key is UNIQUE (producer_id, normalized_name): writing a second
    "אמנון" lands it on the first. The screen offered "Someone new, not
    listed", the producer chose it, and the archive recorded the opposite —
    with a success message.
    """
    segment_id = segment.id
    await _pause_with(
        monkeypatch, segment_id, extraction=_MOSHE,
        candidates=[
            {"uuid": "u1", "name": "Moshe", "summary": "an uncle"},
            {"uuid": "u2", "name": "Moshe Cohen", "summary": "army friend"},
        ],
    )
    db_session.expire_all()

    refused = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entities",
        json={"identity": {"Moshe": {"same_as_existing": False}}, "types": {}},
        headers=auth_headers,
    )
    assert refused.status_code == 400
    assert "tells them apart" in refused.json()["detail"]

    accepted = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entities",
        json={
            "identity": {
                "Moshe": {"same_as_existing": False, "new_name": "Moshe Levi"}
            },
            "types": {},
        },
        headers=auth_headers,
    )
    assert accepted.status_code == 200


async def test_someone_new_about_a_DIFFERENT_name_needs_nothing(
    client: AsyncClient, db_session, segment, analysis_session_factory,
    fake_checkpointer, auth_headers, monkeypatch,
):
    """The requirement must stay narrow. A bare "Moshe" that is not the
    existing "Moshe Cohen" already has its own merge key — demanding a third
    name there would be asking the producer to solve a problem they do not
    have."""
    segment_id = segment.id
    await _pause_with(
        monkeypatch, segment_id, extraction=_MOSHE,
        candidates=[{"uuid": "u2", "name": "Moshe Cohen", "summary": "army friend"}],
    )
    db_session.expire_all()

    resp = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entities",
        json={"identity": {"Moshe": {"same_as_existing": False}}, "types": {}},
        headers=auth_headers,
    )
    assert resp.status_code == 200


async def test_a_distinguishing_name_that_is_also_taken_is_refused(
    client: AsyncClient, db_session, segment, test_user, analysis_session_factory,
    fake_checkpointer, auth_headers, monkeypatch,
):
    """Checked against the whole archive, not just this question's candidates:
    any collision merges them, and not merging is the entire point."""
    db_session.add(
        Entity(
            producer_id=test_user.id, name="Moshe Levi",
            normalized_name=normalize_entity_name("Moshe Levi"), type="person",
        )
    )
    await db_session.commit()

    segment_id = segment.id
    await _pause_with(
        monkeypatch, segment_id, extraction=_MOSHE,
        candidates=[
            {"uuid": "u1", "name": "Moshe", "summary": "an uncle"},
            {"uuid": "u2", "name": "Moshe Cohen", "summary": "army friend"},
        ],
    )
    db_session.expire_all()

    resp = await client.post(
        f"/api/v1/interview/segments/{segment_id}/confirm-entities",
        json={
            "identity": {
                "Moshe": {"same_as_existing": False, "new_name": "Moshe Levi"}
            },
            "types": {},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "already someone in your archive" in resp.json()["detail"]


async def test_answering_someone_new_does_not_crash(monkeypatch):
    """A pre-existing UnboundLocalError, found while building the above.

    `corrected_name` was CALLED in the someone-new branch and DEFINED further
    down, so every producer who answered "someone new" got a crash instead of
    an answer. No test covered that branch.
    """
    ag.interrupt = lambda payload: {
        "identity": {"אמנון": {"same_as_existing": False}},
        "types": {}, "relations": {}, "years": {}, "sides": {},
        "name_edits": {}, "parentage": {},
    }
    result = await ag.human_confirm_node(
        {
            "segment_id": "s",
            "names_to_check": [
                {"name": "אמנון", "candidates": [{"uuid": "u1", "name": "אמנון", "summary": ""}]}
            ],
            "extracted_entities": [
                {"name": "אמנון", "type": "person", "alternative_type": None, "summary": "s"}
            ],
            "proposed_relations": [],
        }
    )
    assert result["entity_resolutions"]["אמנון"]["resolved_name"] == "אמנון"


# ── auto-extraction (BULK_IMPORT_PLAN §10) ──────────────────────────────────
# One branch at the single interrupt site: producer's toggle OR the
# segment's import_batch_id skips the interrupt and lets the documented
# silence-defaults keep the extraction exactly as produced.

_AUTO_STATE_NAMES = [
    {
        "name": "אמנון",
        "question": "?",
        "candidates": [{"uuid": "real-row", "name": "אמנון", "summary": "דוד"}],
    }
]


def _confirm_state(segment_id):
    return {
        "segment_id": segment_id,
        "group_id": "g1",
        "names_to_check": _AUTO_STATE_NAMES,
    }


async def test_auto_extraction_toggle_skips_interrupt(segment, test_user, db_session):
    test_user.auto_extraction = True
    await db_session.commit()

    def boom(payload):  # pragma: no cover - reaching it IS the failure
        raise AssertionError("interrupt fired despite auto_extraction=True")

    real = ag.interrupt
    ag.interrupt = boom
    try:
        result = await ag.human_confirm_node(_confirm_state(segment.id))
    finally:
        ag.interrupt = real
    # Silence-default: the unanswered identity question resolves as
    # "someone new" — the as-extracted state, nothing merged.
    res = (result.get("entity_resolutions") or {}).get("אמנון")
    assert res is not None and res["same_as_uuid"] is None


async def test_import_batch_id_auto_confirms_regardless_of_toggle(
    segment, test_user, db_session
):
    test_user.auto_extraction = False  # the toggle is OFF — batch id alone decides
    segment.import_batch_id = "batch-abc"
    await db_session.commit()

    def boom(payload):  # pragma: no cover
        raise AssertionError("interrupt fired for a bulk-imported segment")

    real = ag.interrupt
    ag.interrupt = boom
    try:
        result = await ag.human_confirm_node(_confirm_state(segment.id))
    finally:
        ag.interrupt = real
    res = (result.get("entity_resolutions") or {}).get("אמנון")
    assert res is not None and res["same_as_uuid"] is None


async def test_manual_path_unchanged_without_toggle_or_batch_id(
    segment, test_user, db_session
):
    assert not test_user.auto_extraction and segment.import_batch_id is None
    called = {}

    def record(payload):
        called["payload"] = payload
        return {}

    real = ag.interrupt
    ag.interrupt = record
    try:
        await ag.human_confirm_node(_confirm_state(segment.id))
    finally:
        ag.interrupt = real
    assert "payload" in called  # today's flow: the interrupt still fires


async def test_warm_debounce_skips_while_siblings_in_flight(segment, db_session, test_user):
    from app.models import RawSegment

    # only `segment` exists and it's pending -> no OTHER in flight
    assert not await ag._another_segment_in_flight(test_user.id, segment.id)
    sibling = RawSegment(
        interview_session_id=segment.interview_session_id,
        question_asked="q", question_index=1,
        video_key="segments/x/y/1/z.webm", status="pending_transcription",
    )
    db_session.add(sibling)
    await db_session.commit()
    assert await ag._another_segment_in_flight(test_user.id, segment.id)
    sibling.status = "ready"
    await db_session.commit()
    assert not await ag._another_segment_in_flight(test_user.id, segment.id)


# ── auto path accepts extracted relation proposals (live bug 2026-08-28:
# blanket-silence auto answers discarded every relation - 164 imports,
# 149 entities, zero tree edges) ─────────────────────────────────────────


def _relation_state(segment_id):
    return {
        "segment_id": segment_id,
        "group_id": "g1",
        "names_to_check": [],
        "proposed_relations": [
            {"from_name": "אני", "to_name": "מרים", "relation_type": "spouse"},
        ],
    }


async def test_auto_toggle_accepts_proposed_relations(segment, test_user, db_session):
    """The GENERAL path: auto_extraction toggle on a normal recording (no
    batch id) must keep extracted relations, not silently drop them."""
    test_user.auto_extraction = True
    await db_session.commit()

    def boom(payload):  # pragma: no cover
        raise AssertionError("interrupt fired in auto mode")

    real = ag.interrupt
    ag.interrupt = boom
    try:
        result = await ag.human_confirm_node(_relation_state(segment.id))
    finally:
        ag.interrupt = real
    kept = result.get("proposed_relations")
    assert kept and kept[0]["to_name"] == "מרים"  # accepted, not discarded


async def test_batch_id_accepts_proposed_relations_toggle_off(
    segment, test_user, db_session
):
    test_user.auto_extraction = False
    segment.import_batch_id = "batch-r"
    await db_session.commit()
    real = ag.interrupt
    ag.interrupt = lambda payload: (_ for _ in ()).throw(AssertionError("interrupt"))
    try:
        result = await ag.human_confirm_node(_relation_state(segment.id))
    finally:
        ag.interrupt = real
    assert result.get("proposed_relations")


async def test_manual_silence_still_drops_relations(segment, test_user, db_session):
    assert not test_user.auto_extraction and segment.import_batch_id is None
    real = ag.interrupt
    ag.interrupt = lambda payload: {}  # producer answered nothing
    try:
        result = await ag.human_confirm_node(_relation_state(segment.id))
    finally:
        ag.interrupt = real
    assert not result.get("proposed_relations")  # unchanged manual behaviour
