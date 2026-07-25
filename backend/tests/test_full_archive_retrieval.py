"""
Tests for full_archive_retrieval.py (Prompt 15, the experimental "video_clips_v2"
full-archive-reading mode). Graphiti/LLM/DB side dependencies are mocked;
this suite verifies the module's own logic — transcript formatting,
deterministic range validation + word-boundary snapping, empty-archive and
no-answer handling, and the orchestrator's reuse of the existing
assembly/caching. Existing avatar and video_clips tests are unaffected
(this module is purely additive and imports the v1 assembly code without
modifying it).
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InterviewSession, RawSegment, TranscriptChunk
from app.services import full_archive_retrieval as ar
from app.services import retrieval_service
from app.services.response_assembler import NO_STORY_FALLBACK

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def ar_session_factory(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(ar, "AsyncSessionLocal", factory)
    return factory


def _word_timestamps(text: str, start_sec: float, word_dur: float = 0.4) -> list:
    out = []
    t = start_sec
    for w in text.split(" "):
        out.append({"word": w, "start_sec": round(t, 3), "end_sec": round(t + word_dur, 3)})
        t += word_dur
    return out


def _chunk(seg_id: str, seq: int, start: float, end: float, text: str, with_words: bool = True):
    return TranscriptChunk(
        id=f"{seg_id}-c{seq}",
        raw_segment_id=seg_id,
        start_sec=start,
        end_sec=end,
        text=text,
        sequence_index=seq,
        word_timestamps=_word_timestamps(text, start) if with_words else None,
    )


def _segment(seg_id: str, question: str) -> RawSegment:
    return RawSegment(
        id=seg_id,
        interview_session_id="int-1",
        question_asked=question,
        question_index=0,
        status="ready",
    )


# ── _format_annotated_transcript ─────────────────────────────────────────────


def test_format_annotated_transcript_includes_ids_questions_and_time_markers():
    archive = [
        ar.ArchiveSegment(
            segment=_segment("seg-a", "Tell me about your family"),
            chunks=[
                _chunk("seg-a", 0, 0.0, 3.0, "I have four siblings."),
                _chunk("seg-a", 1, 3.0, 6.0, "My parents are Zvi and Ilana."),
            ],
        ),
        ar.ArchiveSegment(
            segment=_segment("seg-b", "Tell me about the army"),
            chunks=[_chunk("seg-b", 0, 0.0, 2.5, "I served in the air force.")],
        ),
    ]
    out = ar._format_annotated_transcript(archive)

    assert "[segment seg-a] Interview question: Tell me about your family" in out
    assert "[segment seg-b] Interview question: Tell me about the army" in out
    assert "[0.00-3.00] I have four siblings." in out
    assert "[3.00-6.00] My parents are Zvi and Ilana." in out
    assert "[0.00-2.50] I served in the air force." in out
    # Word-level timings must reach the prompt (the fix for mid-phrase
    # entity location) — each word tagged word:START-END.
    assert "word timings:" in out
    # "My parents are Zvi and Ilana." @start=3.0, 0.4s/word -> Zvi is the 4th word.
    assert "Zvi:4.20-4.60" in out


def test_format_word_timings_compact_and_falls_back_when_absent():
    chunk = _chunk("seg-a", 0, 5.0, 7.0, "one two")
    assert ar._format_word_timings(chunk.word_timestamps) == "one:5.00-5.40 two:5.40-5.80"
    # No word timings -> empty string (caller omits the line).
    assert ar._format_word_timings(None) == ""
    assert ar._format_word_timings([]) == ""


# ── _format_entity_map ───────────────────────────────────────────────────────


def test_format_entity_map_empty():
    assert ar._format_entity_map({}) == "(none extracted)"


def test_format_entity_map_sorted_lines():
    out = ar._format_entity_map({"Ilana": ["seg-a"], "Nir": ["seg-a", "seg-b"]})
    assert out == "- Ilana: seg-a\n- Nir: seg-a, seg-b"


# ── _parse_ranges_json ───────────────────────────────────────────────────────


def test_parse_ranges_json_valid():
    text = '[{"segment_id": "seg-a", "start_sec": 3.0, "end_sec": 6.0}]'
    assert ar._parse_ranges_json(text) == [{"segment_id": "seg-a", "start_sec": 3.0, "end_sec": 6.0}]


def test_parse_ranges_json_skips_malformed_elements_keeps_valid():
    text = (
        '[{"segment_id": "seg-a", "start_sec": 1.0, "end_sec": 2.0}, '
        '{"start_sec": 1.0, "end_sec": 2.0}, '  # missing segment_id
        '{"segment_id": "seg-b", "start_sec": "oops", "end_sec": 2.0}, '  # non-numeric
        '"not-an-object"]'
    )
    assert ar._parse_ranges_json(text) == [{"segment_id": "seg-a", "start_sec": 1.0, "end_sec": 2.0}]


def test_parse_ranges_json_empty_array():
    assert ar._parse_ranges_json("[]") == []


def test_parse_ranges_json_non_json_returns_empty():
    assert ar._parse_ranges_json("I could not find anything.") == []


def test_parse_ranges_json_extracts_array_from_surrounding_text():
    text = 'Here you go: [{"segment_id": "seg-a", "start_sec": 0.0, "end_sec": 1.0}] done'
    assert ar._parse_ranges_json(text) == [{"segment_id": "seg-a", "start_sec": 0.0, "end_sec": 1.0}]


# ── _word_intervals_for_segment ──────────────────────────────────────────────


def test_word_intervals_uses_word_timestamps_when_present():
    chunk = _chunk("seg-a", 0, 5.0, 7.0, "one two three")  # 3 words @0.4s from 5.0
    intervals = ar._word_intervals_for_segment([chunk])
    assert intervals == [(5.0, 5.4), (5.4, 5.8), (5.8, 6.2)]


def test_word_intervals_falls_back_to_chunk_boundary_without_words():
    chunk = _chunk("seg-a", 0, 5.0, 8.0, "one two", with_words=False)
    intervals = ar._word_intervals_for_segment([chunk])
    assert intervals == [(5.0, 8.0)]


# ── _snap_range_to_words ─────────────────────────────────────────────────────


def test_snap_range_snaps_to_first_and_last_overlapping_word():
    intervals = [(5.0, 5.4), (5.4, 5.8), (5.8, 6.2), (6.2, 6.6)]
    # A loose 5.2-6.0 request overlaps words 1-3 (5.0-5.4, 5.4-5.8, 5.8-6.2).
    snapped = ar._snap_range_to_words(5.2, 6.0, intervals)
    assert snapped == (5.0, 6.2)


def test_snap_range_returns_none_when_no_speech_overlap():
    intervals = [(5.0, 6.0)]
    # Entirely inside a silence gap after the last word.
    assert ar._snap_range_to_words(10.0, 12.0, intervals) is None


def test_snap_range_returns_none_for_inverted_range():
    intervals = [(5.0, 6.0)]
    assert ar._snap_range_to_words(6.0, 5.0, intervals) is None


# ── validate_ranges ──────────────────────────────────────────────────────────


def _two_segment_archive():
    return [
        ar.ArchiveSegment(
            segment=_segment("seg-a", "Q1"),
            chunks=[_chunk("seg-a", 0, 0.0, 4.0, "one two three four five")],  # words 0.0..2.0
        ),
        ar.ArchiveSegment(
            segment=_segment("seg-b", "Q2"),
            chunks=[_chunk("seg-b", 0, 0.0, 2.0, "alpha beta")],  # words 0.0..0.8
        ),
    ]


def test_validate_ranges_drops_foreign_segment_id():
    archive = _two_segment_archive()
    result = ar.validate_ranges(
        [{"segment_id": "not-in-archive", "start_sec": 0.0, "end_sec": 1.0}], archive
    )
    assert result == []


def test_validate_ranges_drops_range_with_no_speech_overlap():
    archive = _two_segment_archive()
    # seg-a's words only span 0.0-2.0; a 3.0-3.9 request overlaps no words.
    result = ar.validate_ranges(
        [{"segment_id": "seg-a", "start_sec": 3.0, "end_sec": 3.9}], archive
    )
    assert result == []


def test_validate_ranges_snaps_and_preserves_order_across_segments():
    archive = _two_segment_archive()
    raw = [
        {"segment_id": "seg-b", "start_sec": 0.1, "end_sec": 0.5},
        {"segment_id": "seg-a", "start_sec": 0.3, "end_sec": 1.1},
    ]
    result = ar.validate_ranges(raw, archive)
    assert [c.raw_segment_id for c in result] == ["seg-b", "seg-a"]  # order preserved
    # seg-b words: (0.0,0.4),(0.4,0.8); 0.1-0.5 overlaps both -> (0.0,0.8)
    assert (result[0].start_sec, result[0].end_sec) == (0.0, 0.8)
    # seg-a words @0.4s: (0.0,0.4),(0.4,0.8),(0.8,1.2),...; 0.3-1.1 overlaps first 3 -> (0.0,1.2)
    assert (result[1].start_sec, result[1].end_sec) == (0.0, 1.2)
    assert all(c.source_chunk_id.startswith("archive-read:") for c in result)


# ── _load_archive (DB-backed) ────────────────────────────────────────────────


@pytest.fixture
async def seeded_archive(db_session, test_user, ar_session_factory):
    """Two 'ready' segments (with chunks), one 'pending' segment (must be
    excluded), and one 'ready' segment with NO chunks (must be excluded)."""
    session = InterviewSession(id="int-1", user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.flush()

    ready_a = RawSegment(id="seg-a", interview_session_id="int-1", question_asked="Family",
                         question_index=0, status="ready")
    ready_b = RawSegment(id="seg-b", interview_session_id="int-1", question_asked="Army",
                         question_index=1, status="ready")
    pending = RawSegment(id="seg-pending", interview_session_id="int-1", question_asked="Later",
                         question_index=2, status="pending_analysis")
    ready_no_chunks = RawSegment(id="seg-empty", interview_session_id="int-1",
                                 question_asked="Empty", question_index=3, status="ready")
    db_session.add_all([ready_a, ready_b, pending, ready_no_chunks])
    await db_session.flush()

    db_session.add_all([
        TranscriptChunk(raw_segment_id="seg-a", start_sec=0.0, end_sec=3.0, text="fam chunk",
                        sequence_index=0, word_timestamps=_word_timestamps("fam chunk", 0.0)),
        TranscriptChunk(raw_segment_id="seg-b", start_sec=0.0, end_sec=2.0, text="army chunk",
                        sequence_index=0, word_timestamps=_word_timestamps("army chunk", 0.0)),
        # A chunk on the pending segment — must still be excluded via segment status.
        TranscriptChunk(raw_segment_id="seg-pending", start_sec=0.0, end_sec=1.0, text="nope",
                        sequence_index=0),
    ])
    await db_session.commit()
    return {"user": test_user}


async def test_load_archive_only_ready_segments_with_chunks(seeded_archive, test_user):
    archive = await ar._load_archive(test_user.id)
    ids = [a.segment.id for a in archive]
    assert ids == ["seg-a", "seg-b"]  # pending excluded, chunkless-ready excluded
    assert all(len(a.chunks) == 1 for a in archive)


async def test_load_archive_empty_for_unknown_producer(seeded_archive):
    assert await ar._load_archive("nobody") == []


# ── read_and_validate_ranges orchestration ───────────────────────────────────


async def test_read_and_validate_ranges_empty_archive_skips_llm(monkeypatch):
    monkeypatch.setattr(ar, "_load_archive", AsyncMock(return_value=[]))
    llm_mock = AsyncMock()
    monkeypatch.setattr(ar, "_read_archive_for_ranges", llm_mock)

    result = await ar.read_and_validate_ranges("q", "group", "he", "sess")

    assert result == []
    llm_mock.assert_not_called()


async def test_read_and_validate_ranges_no_answer_returns_empty(monkeypatch):
    archive = _two_segment_archive()
    monkeypatch.setattr(ar, "_load_archive", AsyncMock(return_value=archive))
    monkeypatch.setattr(ar, "_build_entity_map", AsyncMock(return_value={}))
    monkeypatch.setattr(retrieval_service, "_recent_turns", AsyncMock(return_value=[]))
    monkeypatch.setattr(ar, "_read_archive_for_ranges", AsyncMock(return_value=[]))

    result = await ar.read_and_validate_ranges("q", "group", "he", "sess")
    assert result == []


async def test_read_and_validate_ranges_happy_path_returns_validated_clips(monkeypatch):
    archive = _two_segment_archive()
    monkeypatch.setattr(ar, "_load_archive", AsyncMock(return_value=archive))
    monkeypatch.setattr(ar, "_build_entity_map", AsyncMock(return_value={"Nir": ["seg-a"]}))
    monkeypatch.setattr(retrieval_service, "_recent_turns", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ar,
        "_read_archive_for_ranges",
        AsyncMock(return_value=[{"segment_id": "seg-a", "start_sec": 0.3, "end_sec": 1.1}]),
    )

    result = await ar.read_and_validate_ranges("q", "group", "he", "sess")
    assert len(result) == 1
    assert result[0].raw_segment_id == "seg-a"
    assert (result[0].start_sec, result[0].end_sec) == (0.0, 1.2)  # snapped to word boundaries


# ── assemble_video_clip_response_v2 orchestration ────────────────────────────


async def test_assemble_v2_no_ranges_returns_no_story(monkeypatch):
    monkeypatch.setattr(ar, "read_and_validate_ranges", AsyncMock(return_value=[]))

    result = await ar.assemble_video_clip_response_v2("q", "group", "he", "sess")
    assert result.no_story is True
    assert result.video_url is None
    assert result.fallback_text == NO_STORY_FALLBACK


async def test_assemble_v2_cache_hit_skips_ffmpeg(monkeypatch):
    clips = [ar.ExpandedClip(raw_segment_id="seg-a", start_sec=0.0, end_sec=1.2,
                             source_chunk_id="archive-read:seg-a")]
    monkeypatch.setattr(ar, "read_and_validate_ranges", AsyncMock(return_value=clips))
    monkeypatch.setattr(ar.cache_service, "get", AsyncMock(return_value="https://cdn/cached.mp4"))
    assemble_mock = AsyncMock()
    monkeypatch.setattr(ar.video_clip_assembler, "_assemble_and_upload_clip", assemble_mock)

    result = await ar.assemble_video_clip_response_v2("q", "group", "he", "sess")
    assert result.video_url == "https://cdn/cached.mp4"
    assemble_mock.assert_not_called()


async def test_assemble_v2_happy_path_assembles_and_caches(monkeypatch):
    clips = [ar.ExpandedClip(raw_segment_id="seg-a", start_sec=0.0, end_sec=1.2,
                             source_chunk_id="archive-read:seg-a")]
    monkeypatch.setattr(ar, "read_and_validate_ranges", AsyncMock(return_value=clips))
    monkeypatch.setattr(ar.cache_service, "get", AsyncMock(return_value=None))
    set_mock = AsyncMock()
    monkeypatch.setattr(ar.cache_service, "set", set_mock)
    monkeypatch.setattr(ar.cache_service, "add_visited", AsyncMock(return_value=True))
    monkeypatch.setattr(
        ar.video_clip_assembler, "_assemble_and_upload_clip",
        AsyncMock(return_value="https://cdn/new.mp4"),
    )

    result = await ar.assemble_video_clip_response_v2("q", "group", "he", "sess")
    assert result.video_url == "https://cdn/new.mp4"
    assert result.no_story is False
    set_mock.assert_called_once()


async def test_assemble_v2_assembly_failure_returns_no_story(monkeypatch):
    clips = [ar.ExpandedClip(raw_segment_id="seg-a", start_sec=0.0, end_sec=1.2,
                             source_chunk_id="archive-read:seg-a")]
    monkeypatch.setattr(ar, "read_and_validate_ranges", AsyncMock(return_value=clips))
    monkeypatch.setattr(ar.cache_service, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ar.video_clip_assembler, "_assemble_and_upload_clip", AsyncMock(return_value=None)
    )

    result = await ar.assemble_video_clip_response_v2("q", "group", "he", "sess")
    assert result.no_story is True
    assert result.fallback_text == NO_STORY_FALLBACK


# ── _read_archive_for_ranges (LLM call structure + fail-soft) ─────────────────


async def test_read_archive_for_ranges_static_transcript_in_system_variable_question_in_user(
    monkeypatch,
):
    """Prompt-cache ordering: the large static archive must be in the
    system prompt, the variable question in the user message."""
    captured = {}

    async def fake_generate(messages, system_prompt=None, thinking=False, temperature=None):
        captured["system"] = system_prompt
        captured["messages"] = messages
        return "[]"

    monkeypatch.setattr(ar.llm_service, "generate_response", fake_generate)

    await ar._read_archive_for_ranges(
        "מי זאת אילנה?", "TRANSCRIPT_HERE", "ENTITY_MAP_HERE", [], "he"
    )

    assert "TRANSCRIPT_HERE" in captured["system"]
    assert "ENTITY_MAP_HERE" in captured["system"]
    assert captured["messages"][0]["role"] == "user"
    assert "מי זאת אילנה?" in captured["messages"][0]["content"]
    # The variable question must NOT be baked into the cacheable system prompt.
    assert "מי זאת אילנה?" not in captured["system"]


async def test_read_archive_for_ranges_includes_history_when_present(monkeypatch):
    captured = {}

    async def fake_generate(messages, system_prompt=None, thinking=False, temperature=None):
        captured["messages"] = messages
        return "[]"

    monkeypatch.setattr(ar.llm_service, "generate_response", fake_generate)
    history = [{"role": "user", "content": "who most influenced you?"},
               {"role": "assistant", "content": "http://x/clip.mp4"}]

    await ar._read_archive_for_ranges("is he still alive?", "T", "E", history, "he")

    user_content = captured["messages"][0]["content"]
    assert "who most influenced you?" in user_content
    # Video-URL assistant turn masked, not leaked as narration.
    assert "http://x/clip.mp4" not in user_content
    assert "(showed a video clip)" in user_content


async def test_read_archive_for_ranges_fails_soft_on_llm_error(monkeypatch):
    monkeypatch.setattr(
        ar.llm_service, "generate_response", AsyncMock(side_effect=RuntimeError("down"))
    )
    result = await ar._read_archive_for_ranges("q", "T", "E", [], "he")
    assert result == []
