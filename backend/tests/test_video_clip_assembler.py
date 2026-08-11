"""
Tests for video_clip_assembler.py (Prompt 13) — the video-clip-mode answer
assembly built ALONGSIDE the avatar path's response_assembler.py, not
replacing it. llm_service/retrieval_service/cache_service/storage_service
are mocked; ffmpeg is mocked via subprocess.run the same way
test_animator_simple.py does for the avatar path's own ffmpeg call — real
DB rows (in-memory SQLite) back the boundary-expansion tests since those
need actual neighboring TranscriptChunk rows to walk across.
"""

import subprocess
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InterviewSession, RawSegment, TranscriptChunk
from app.services import video_clip_assembler as vca
from app.services.response_assembler import NO_STORY_FALLBACK

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def vca_session_factory(test_engine, monkeypatch):
    """Autouse since photo_categories_for_segments: the success-path
    orchestration now opens the module-level AsyncSessionLocal itself, so a
    test that mocks every collaborator would still hit the REAL configured
    database without this. No test in this file should ever touch it."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(vca, "AsyncSessionLocal", factory)
    return factory


def _word_timestamps(text: str, start_sec: float = 0.0, word_dur: float = 0.5) -> list:
    words = text.split(" ")
    out = []
    t = start_sec
    for w in words:
        out.append({"word": w, "start_sec": round(t, 3), "end_sec": round(t + word_dur, 3)})
        t += word_dur
    return out


# ── _split_question_into_clauses ────────────────────────────────────────────


async def test_split_question_into_clauses_parses_multi_clause_response(monkeypatch):
    monkeypatch.setattr(
        vca.llm_service,
        "generate_response",
        AsyncMock(return_value='["what did you do for work", "did you enjoy it"]'),
    )
    clauses = await vca._split_question_into_clauses(
        "what did you do for work and did you enjoy it", "en"
    )
    assert clauses == ["what did you do for work", "did you enjoy it"]


async def test_split_question_into_clauses_fails_soft_to_single_clause_on_error(monkeypatch):
    monkeypatch.setattr(
        vca.llm_service, "generate_response", AsyncMock(side_effect=RuntimeError("down"))
    )
    clauses = await vca._split_question_into_clauses("what was your childhood like", "en")
    assert clauses == ["what was your childhood like"]


async def test_split_question_into_clauses_fails_soft_when_llm_returns_empty_array(monkeypatch):
    monkeypatch.setattr(vca.llm_service, "generate_response", AsyncMock(return_value="[]"))
    clauses = await vca._split_question_into_clauses("a single-part question", "en")
    assert clauses == ["a single-part question"]


# ── _locate_substring_in_word_timestamps ────────────────────────────────────


def test_locate_substring_in_word_timestamps_finds_exact_match():
    text = "I worked as a teacher for many years and loved it"
    wts = _word_timestamps(text)
    located = vca._locate_substring_in_word_timestamps("I worked as a teacher", wts)
    assert located is not None
    start, end = located
    assert start == wts[0]["start_sec"]
    assert end == wts[4]["end_sec"]  # "teacher" is the 5th word (index 4)


def test_locate_substring_in_word_timestamps_returns_none_when_not_found():
    wts = _word_timestamps("I worked as a teacher")
    assert vca._locate_substring_in_word_timestamps("I flew airplanes", wts) is None


def test_locate_substring_in_word_timestamps_returns_none_for_empty_input():
    assert vca._locate_substring_in_word_timestamps("", []) is None
    assert vca._locate_substring_in_word_timestamps("something", []) is None


# ── _verify_and_pinpoint_chunk ───────────────────────────────────────────────


def _make_chunk(text: str, **kwargs) -> TranscriptChunk:
    defaults = dict(
        id=kwargs.pop("id", "chunk-1"),
        raw_segment_id=kwargs.pop("raw_segment_id", "seg-1"),
        start_sec=0.0,
        end_sec=10.0,
        text=text,
        sequence_index=0,
        word_timestamps=_word_timestamps(text),
    )
    defaults.update(kwargs)
    return TranscriptChunk(**defaults)


async def test_verify_and_pinpoint_chunk_relevant_with_pinpointed_substring(monkeypatch):
    text = "I worked as a teacher for many years and then I opened a small bakery"
    chunk = _make_chunk(text)
    monkeypatch.setattr(
        vca.llm_service,
        "generate_response",
        AsyncMock(
            return_value='{"relevant": true, "answer_substrings": '
            '["I worked as a teacher for many years"], "covered_clause_indices": [0]}'
        ),
    )
    result = await vca._verify_and_pinpoint_chunk(chunk, "what did you do for work?", ["what did you do for work?"])
    assert result is not None
    assert result.covered_clause_indices == [0]
    assert len(result.answer_ranges) == 1
    start, end = result.answer_ranges[0]
    assert start == chunk.word_timestamps[0]["start_sec"]
    # Narrower than the full chunk's own boundaries — the whole point of pinpointing.
    assert end < chunk.end_sec


async def test_verify_and_pinpoint_chunk_rejects_non_relevant_chunk(monkeypatch):
    """A genuine "relevant: false" verdict is a real rejection, NOT fail-soft."""
    chunk = _make_chunk("Something about the weather that day.")
    monkeypatch.setattr(
        vca.llm_service,
        "generate_response",
        AsyncMock(return_value='{"relevant": false, "answer_substrings": [], "covered_clause_indices": []}'),
    )
    result = await vca._verify_and_pinpoint_chunk(chunk, "what did you do for work?", ["what did you do for work?"])
    assert result is None


async def test_verify_and_pinpoint_chunk_fails_soft_on_llm_exception(monkeypatch):
    chunk = _make_chunk("I worked as a teacher for many years.")
    monkeypatch.setattr(
        vca.llm_service, "generate_response", AsyncMock(side_effect=RuntimeError("down"))
    )
    result = await vca._verify_and_pinpoint_chunk(chunk, "what did you do for work?", ["what did you do for work?"])
    assert result is not None
    assert result.answer_ranges == [(chunk.start_sec, chunk.end_sec)]
    # Never claim coverage that was never actually verified.
    assert result.covered_clause_indices == []


async def test_verify_and_pinpoint_chunk_fails_soft_on_llm_unparseable_response(monkeypatch):
    chunk = _make_chunk("I worked as a teacher for many years.")
    monkeypatch.setattr(
        vca.llm_service, "generate_response", AsyncMock(return_value="not json at all")
    )
    result = await vca._verify_and_pinpoint_chunk(chunk, "what did you do for work?", ["what did you do for work?"])
    assert result is not None
    assert result.answer_ranges == [(chunk.start_sec, chunk.end_sec)]
    assert result.covered_clause_indices == []


async def test_verify_and_pinpoint_chunk_fails_soft_on_hallucinated_substring(monkeypatch):
    """relevant=true but the model's answer_substrings entry is not verbatim
    in the chunk's text — falls back to whole-chunk boundaries, but the
    candidate is still kept (it WAS judged relevant, unlike the rejection
    case above)."""
    chunk = _make_chunk("I worked as a teacher for many years.")
    monkeypatch.setattr(
        vca.llm_service,
        "generate_response",
        AsyncMock(
            return_value='{"relevant": true, "answer_substrings": '
            '["I flew fighter jets in the war"], "covered_clause_indices": [0]}'
        ),
    )
    result = await vca._verify_and_pinpoint_chunk(chunk, "what did you do for work?", ["what did you do for work?"])
    assert result is not None
    assert result.answer_ranges == [(chunk.start_sec, chunk.end_sec)]
    # Coverage claim itself is trusted (that part of the model's output wasn't hallucinated).
    assert result.covered_clause_indices == [0]


async def test_verify_and_pinpoint_chunk_multi_clause_coverage_on_multi_idea_chunk(monkeypatch):
    """A chunk containing multiple distinct ideas can cover more than one
    clause of a multi-part question."""
    text = "I worked as a teacher for many years and I truly loved every day of it"
    chunk = _make_chunk(text)
    monkeypatch.setattr(
        vca.llm_service,
        "generate_response",
        AsyncMock(
            return_value='{"relevant": true, "answer_substrings": '
            f'["{text}"], "covered_clause_indices": [0, 1]}}'
        ),
    )
    clauses = ["what did you do for work", "did you enjoy it", "tell me a story"]
    result = await vca._verify_and_pinpoint_chunk(chunk, "what did you do for work, did you enjoy it, tell me stories", clauses)
    assert result is not None
    assert result.covered_clause_indices == [0, 1]


async def test_verify_and_pinpoint_chunk_returns_multiple_non_contiguous_sub_ranges(monkeypatch):
    """The core new behavior: relevance-per-sub-topic, not a duration
    split. A chunk running 4 sub-topics together (childhood/Tiberias,
    siblings, parents, schooling) where only 2 (siblings, parents) answer
    the question must return exactly those 2 sub-ranges, dropping the
    irrelevant preamble/postamble entirely — never the whole chunk just
    because narrowing is "hard"."""
    text = (
        "I grew up in a small town. I have four siblings named Nir Chen Adi and Raz. "
        "Our old house had a red door and a big garden out back. "
        "My parents are named Zvi and Ilana. Later I went away to boarding school."
    )
    chunk = _make_chunk(text, end_sec=20.0)  # real headroom for this longer text's word timestamps
    monkeypatch.setattr(
        vca.llm_service,
        "generate_response",
        AsyncMock(
            return_value=(
                '{"relevant": true, "answer_substrings": '
                '["I have four siblings named Nir Chen Adi and Raz.", '
                '"My parents are named Zvi and Ilana."], '
                '"covered_clause_indices": [0]}'
            )
        ),
    )
    result = await vca._verify_and_pinpoint_chunk(chunk, "tell me about your family", ["tell me about your family"])
    assert result is not None
    assert len(result.answer_ranges) == 2
    (s1, e1), (s2, e2) = result.answer_ranges
    # Both sub-ranges are strictly inside the chunk's full bounds, and the
    # first ends before the second starts — the "boarding school", "small
    # town", and "house/garden" sub-topics are all excluded, not just
    # de-prioritized.
    assert chunk.start_sec < s1 < e1 < s2 < e2 < chunk.end_sec


async def test_verify_and_pinpoint_chunk_drops_only_hallucinated_entry_keeps_valid_ones(monkeypatch):
    """One array entry is hallucinated (not verbatim), another is real —
    the real one must survive rather than discarding everything."""
    text = "I worked as a teacher for many years and I truly loved every day of it"
    chunk = _make_chunk(text)
    monkeypatch.setattr(
        vca.llm_service,
        "generate_response",
        AsyncMock(
            return_value=(
                '{"relevant": true, "answer_substrings": '
                '["I worked as a teacher for many years", "I flew fighter jets in Korea"], '
                '"covered_clause_indices": [0]}'
            )
        ),
    )
    result = await vca._verify_and_pinpoint_chunk(chunk, "what did you do for work?", ["what did you do for work?"])
    assert result is not None
    assert len(result.answer_ranges) == 1
    start, end = result.answer_ranges[0]
    assert end < chunk.end_sec  # the real, narrowed entry survived


def test_merge_adjacent_ranges_collapses_touching_ranges():
    merged = vca._merge_adjacent_ranges([(0.0, 5.0), (5.05, 8.0)])
    assert merged == [(0.0, 8.0)]


def test_merge_adjacent_ranges_keeps_real_gaps_separate():
    merged = vca._merge_adjacent_ranges([(0.0, 5.0), (12.0, 15.0)])
    assert merged == [(0.0, 5.0), (12.0, 15.0)]


def test_merge_adjacent_ranges_handles_empty_and_single():
    assert vca._merge_adjacent_ranges([]) == []
    assert vca._merge_adjacent_ranges([(1.0, 2.0)]) == [(1.0, 2.0)]


# ── _topics_overlap_or_similar ───────────────────────────────────────────────


def test_topics_overlap_or_similar_shared_tag():
    a = _make_chunk("a", topic_tags=["work", "family"])
    b = _make_chunk("b", topic_tags=["family", "school"])
    assert vca._topics_overlap_or_similar(a, b) is True


def test_topics_overlap_or_similar_non_overlapping_tags_is_real_drift_even_with_similar_embeddings():
    """When BOTH sides have topic_tags, that explicit signal is trusted over
    embeddings — non-overlapping tags means real drift, not a fallback
    opportunity, even if the embeddings happen to be close."""
    a = _make_chunk("a", topic_tags=["work"], embedding=[1.0, 0.0, 0.0])
    b = _make_chunk("b", topic_tags=["family"], embedding=[0.9, 0.1, 0.0])
    assert vca._topics_overlap_or_similar(a, b) is False


def test_topics_overlap_or_similar_falls_back_to_embedding_when_one_side_has_no_tags():
    a = _make_chunk("a", topic_tags=["work"], embedding=[1.0, 0.0, 0.0])
    b = _make_chunk("b", topic_tags=[], embedding=[0.9, 0.1, 0.0])
    assert vca._topics_overlap_or_similar(a, b) is True


def test_topics_overlap_or_similar_no_signal_returns_false():
    a = _make_chunk("a")
    b = _make_chunk("b")
    assert vca._topics_overlap_or_similar(a, b) is False


def test_topics_overlap_or_similar_dissimilar_embeddings_returns_false():
    a = _make_chunk("a", embedding=[1.0, 0.0, 0.0])
    b = _make_chunk("b", embedding=[0.0, 1.0, 0.0])
    assert vca._topics_overlap_or_similar(a, b) is False


# ── _expand_chunk_boundaries ─────────────────────────────────────────────────


@pytest.fixture
async def segment_with_chunks(db_session, test_user, vca_session_factory):
    session = InterviewSession(user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)

    segment = RawSegment(
        interview_session_id=session.id,
        question_asked="Tell me about your career",
        question_index=0,
        transcript="full transcript",
        importance_score=5.0,
        video_key="segments/u1/s1/0/abc.webm",
        status="ready",
    )
    db_session.add(segment)
    await db_session.commit()
    await db_session.refresh(segment)

    async def add_chunk(seq, start, end, text, topic_tags=None):
        chunk = TranscriptChunk(
            raw_segment_id=segment.id,
            start_sec=start,
            end_sec=end,
            text=text,
            sequence_index=seq,
            word_timestamps=_word_timestamps(text, start_sec=start),
            topic_tags=topic_tags if topic_tags is not None else ["work"],
        )
        db_session.add(chunk)
        await db_session.commit()
        await db_session.refresh(chunk)
        return chunk

    return {"segment": segment, "add_chunk": add_chunk}


async def test_expand_chunk_boundaries_extends_to_neighboring_chunks(segment_with_chunks):
    add_chunk = segment_with_chunks["add_chunk"]
    await add_chunk(0, 0.0, 5.0, "Before context about the job.")
    anchor = await add_chunk(1, 5.0, 10.0, "I loved my work as a teacher.")
    await add_chunk(2, 10.0, 14.0, "After context about the job.")

    verified = vca.VerifiedChunk(chunk=anchor, answer_ranges=[(6.0, 9.0)])
    expanded = await vca._expand_chunk_boundaries(verified)

    assert len(expanded) == 1
    assert expanded[0].raw_segment_id == anchor.raw_segment_id
    assert expanded[0].start_sec == 0.0
    assert expanded[0].end_sec == 14.0


async def test_expand_chunk_boundaries_stops_at_topic_drift(segment_with_chunks):
    add_chunk = segment_with_chunks["add_chunk"]
    await add_chunk(0, 0.0, 5.0, "Unrelated story about a vacation.", topic_tags=["travel"])
    anchor = await add_chunk(1, 5.0, 10.0, "I loved my work as a teacher.", topic_tags=["work"])
    await add_chunk(2, 10.0, 14.0, "More about my teaching career.", topic_tags=["work"])

    verified = vca.VerifiedChunk(chunk=anchor, answer_ranges=[(6.0, 9.0)])
    expanded = await vca._expand_chunk_boundaries(verified)

    assert len(expanded) == 1
    # Backward neighbor has an unrelated topic — must NOT be pulled in.
    assert expanded[0].start_sec == 6.0
    # Forward neighbor shares the topic — still extends that direction.
    assert expanded[0].end_sec == 14.0


async def test_expand_chunk_boundaries_stops_at_silence_gap(segment_with_chunks):
    add_chunk = segment_with_chunks["add_chunk"]
    anchor = await add_chunk(0, 10.0, 15.0, "I loved my work as a teacher.", topic_tags=["work"])
    # Gap of 20s between anchor end (15.0) and this neighbor's start (35.0) — over MAX_SILENCE_GAP_SEC.
    await add_chunk(1, 35.0, 40.0, "Much later, a different topic came up.", topic_tags=["work"])

    verified = vca.VerifiedChunk(chunk=anchor, answer_ranges=[(11.0, 14.0)])
    expanded = await vca._expand_chunk_boundaries(verified)

    assert len(expanded) == 1
    assert expanded[0].start_sec == 11.0
    assert expanded[0].end_sec == 14.0


async def test_expand_chunk_boundaries_stops_at_max_duration(segment_with_chunks):
    add_chunk = segment_with_chunks["add_chunk"]
    anchor = await add_chunk(0, 0.0, 5.0, "I loved my work as a teacher.", topic_tags=["work"])
    # This neighbor alone would push total duration well past MAX_CLIP_DURATION_SEC (30s).
    await add_chunk(1, 5.0, 50.0, "A very long continuation of the same topic.", topic_tags=["work"])

    verified = vca.VerifiedChunk(chunk=anchor, answer_ranges=[(1.0, 4.0)])
    expanded = await vca._expand_chunk_boundaries(verified)

    assert len(expanded) == 1
    assert expanded[0].start_sec == 1.0
    assert expanded[0].end_sec == 4.0


async def test_expand_chunk_boundaries_preserves_internal_gap_between_sub_ranges(
    segment_with_chunks,
):
    """The core new behavior at the expansion layer: TWO non-contiguous
    sub-ranges within the SAME chunk must produce TWO separate ExpandedClip
    pieces with the internal gap intact — expansion only extends the
    OUTER edges (into neighboring chunks), never bridges the deliberate
    internal exclusion back together."""
    add_chunk = segment_with_chunks["add_chunk"]
    before = await add_chunk(0, 0.0, 5.0, "Before context.", topic_tags=["work"])
    anchor = await add_chunk(
        1, 5.0, 20.0, "Siblings part then unrelated then parents part.", topic_tags=["work"]
    )
    after = await add_chunk(2, 20.0, 24.0, "After context.", topic_tags=["work"])

    # Two sub-ranges inside the anchor chunk, with a real internal gap (10.0-14.0).
    verified = vca.VerifiedChunk(chunk=anchor, answer_ranges=[(6.0, 10.0), (14.0, 18.0)])
    expanded = await vca._expand_chunk_boundaries(verified)

    assert len(expanded) == 2
    # First piece: start extended back into the "before" neighbor; own end unchanged.
    assert expanded[0].start_sec == before.start_sec
    assert expanded[0].end_sec == 10.0
    # Second piece: own start unchanged; end extended forward into the "after" neighbor.
    assert expanded[1].start_sec == 14.0
    assert expanded[1].end_sec == after.end_sec
    # The internal gap (10.0-14.0) is never bridged.
    assert expanded[0].end_sec < expanded[1].start_sec


# ── ffmpeg trim / concat ─────────────────────────────────────────────────────


async def test_trim_clip_uses_synchronous_subprocess(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, capture_output=True, timeout=None):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake video")
    output = tmp_path / "out.mp4"

    await vca._trim_clip(source, 5.0, 9.0, output)

    cmd = captured["cmd"]
    assert cmd[0] == "ffmpeg"
    assert "-ss" in cmd and "5.000" in cmd
    assert "-to" in cmd and "9.000" in cmd
    assert str(source) in cmd
    assert str(output) in cmd


async def test_trim_clip_raises_on_ffmpeg_failure(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output=True, timeout=None):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"ffmpeg exploded")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="ffmpeg trim failed"):
        await vca._trim_clip(tmp_path / "in.mp4", 0.0, 1.0, tmp_path / "out.mp4")


async def test_run_ffmpeg_passes_timeout_to_subprocess(monkeypatch):
    """subprocess.run has no timeout by default — a stuck ffmpeg process
    would otherwise hang the whole WS turn forever with no exception."""
    captured = {}

    def fake_run(cmd, capture_output=True, timeout=None):
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    vca._run_ffmpeg(["ffmpeg", "-y"])
    assert captured["timeout"] == vca.FFMPEG_TIMEOUT_SECONDS


async def test_run_ffmpeg_converts_timeout_expired_to_runtime_error(monkeypatch):
    def fake_run(cmd, capture_output=True, timeout=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="ffmpeg timed out"):
        vca._run_ffmpeg(["ffmpeg", "-y"])


async def test_trim_clip_raises_runtime_error_when_ffmpeg_hangs(monkeypatch, tmp_path):
    """End-to-end through _trim_clip (not just _run_ffmpeg in isolation): a
    hung ffmpeg process must surface as a catchable RuntimeError, not block
    the awaiting WS turn forever."""

    def fake_run(cmd, capture_output=True, timeout=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="ffmpeg timed out"):
        await vca._trim_clip(tmp_path / "in.mp4", 0.0, 1.0, tmp_path / "out.mp4")


async def test_concat_clips_uses_stream_copy_concat_demuxer(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, capture_output=True, timeout=None):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    clip_a.write_bytes(b"a")
    clip_b.write_bytes(b"b")
    output = tmp_path / "final.mp4"

    await vca._concat_clips([clip_a, clip_b], output)

    cmd = captured["cmd"]
    assert cmd[0] == "ffmpeg"
    assert "-f" in cmd and "concat" in cmd
    assert "-c" in cmd and "copy" in cmd
    # The temporary list file must not survive past the call.
    assert not output.with_suffix(".txt").exists()


async def test_concat_clips_raises_and_cleans_up_list_file_on_failure(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output=True, timeout=None):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"concat failed")

    monkeypatch.setattr(subprocess, "run", fake_run)

    clip_a = tmp_path / "a.mp4"
    clip_a.write_bytes(b"a")
    output = tmp_path / "final.mp4"

    with pytest.raises(RuntimeError, match="ffmpeg concat failed"):
        await vca._concat_clips([clip_a], output)
    assert not output.with_suffix(".txt").exists()


# ── _assemble_and_upload_clip (non-contiguous, multi-recording) ────────────


async def test_assemble_and_upload_clip_multi_recording_non_contiguous(monkeypatch):
    """Two clips from two DIFFERENT raw_segment_ids (non-contiguous, across
    recordings) get individually trimmed and concatenated into one output."""
    clip_a = vca.ExpandedClip(raw_segment_id="seg-a", start_sec=1.0, end_sec=3.0, source_chunk_id="c-a")
    clip_b = vca.ExpandedClip(raw_segment_id="seg-b", start_sec=10.0, end_sec=12.0, source_chunk_id="c-b")

    segments = {
        "seg-a": RawSegment(id="seg-a", video_key="videos/seg-a.mp4", interview_session_id="x", question_asked="q", question_index=0),
        "seg-b": RawSegment(id="seg-b", video_key="videos/seg-b.mp4", interview_session_id="x", question_asked="q", question_index=1),
    }
    monkeypatch.setattr(vca, "_fetch_segment_videos", AsyncMock(return_value=segments))
    monkeypatch.setattr(
        vca.storage_service, "download_file", AsyncMock(return_value=b"fake source bytes")
    )
    monkeypatch.setattr(vca.storage_service, "upload_file", AsyncMock(return_value="key"))
    monkeypatch.setattr(
        vca.storage_service, "serving_url", AsyncMock(return_value="https://cdn.example/final.mp4")
    )

    trim_calls = []

    async def fake_trim(source_path, start_sec, end_sec, output_path):
        trim_calls.append((str(source_path), start_sec, end_sec))
        output_path.write_bytes(b"trimmed")

    concat_calls = []

    async def fake_concat(clip_paths, output_path):
        concat_calls.append([str(p) for p in clip_paths])
        output_path.write_bytes(b"concatenated")

    monkeypatch.setattr(vca, "_trim_clip", fake_trim)
    monkeypatch.setattr(vca, "_concat_clips", fake_concat)

    url = await vca._assemble_and_upload_clip([clip_a, clip_b], "group-1", "session-1")

    assert url == "https://cdn.example/final.mp4"
    assert len(trim_calls) == 2
    assert len(concat_calls) == 1
    assert len(concat_calls[0]) == 2


async def test_assemble_and_upload_clip_returns_none_when_every_clip_fails(monkeypatch):
    clip_a = vca.ExpandedClip(raw_segment_id="seg-a", start_sec=1.0, end_sec=3.0, source_chunk_id="c-a")
    segments = {
        "seg-a": RawSegment(id="seg-a", video_key="videos/seg-a.mp4", interview_session_id="x", question_asked="q", question_index=0),
    }
    monkeypatch.setattr(vca, "_fetch_segment_videos", AsyncMock(return_value=segments))
    monkeypatch.setattr(
        vca.storage_service, "download_file", AsyncMock(side_effect=RuntimeError("storage down"))
    )
    upload_mock = AsyncMock()
    monkeypatch.setattr(vca.storage_service, "upload_file", upload_mock)

    url = await vca._assemble_and_upload_clip([clip_a], "group-1", "session-1")

    assert url is None
    upload_mock.assert_not_called()


async def test_assemble_and_upload_clip_skips_segment_with_no_video_key(monkeypatch):
    clip_a = vca.ExpandedClip(raw_segment_id="seg-a", start_sec=1.0, end_sec=3.0, source_chunk_id="c-a")
    segments = {
        "seg-a": RawSegment(id="seg-a", video_key=None, interview_session_id="x", question_asked="q", question_index=0),
    }
    monkeypatch.setattr(vca, "_fetch_segment_videos", AsyncMock(return_value=segments))
    download_mock = AsyncMock()
    monkeypatch.setattr(vca.storage_service, "download_file", download_mock)

    url = await vca._assemble_and_upload_clip([clip_a], "group-1", "session-1")

    assert url is None
    download_mock.assert_not_called()


# ── assemble_video_clip_response (orchestrator) ─────────────────────────────


async def test_assemble_video_clip_response_no_candidates_returns_no_story(monkeypatch):
    monkeypatch.setattr(vca.retrieval_service, "retrieve_chunks", AsyncMock(return_value=[]))

    result = await vca.assemble_video_clip_response("anything", "group-1", "en", "session-1")

    assert result.no_story is True
    assert result.video_url is None
    assert result.fallback_text == NO_STORY_FALLBACK


async def test_assemble_video_clip_response_all_candidates_rejected_returns_no_story(monkeypatch):
    chunk = _make_chunk("Something unrelated.")
    monkeypatch.setattr(vca.retrieval_service, "retrieve_chunks", AsyncMock(return_value=[chunk]))
    monkeypatch.setattr(vca, "_split_question_into_clauses", AsyncMock(return_value=["q"]))
    monkeypatch.setattr(vca, "_verify_and_pinpoint_chunk", AsyncMock(return_value=None))

    result = await vca.assemble_video_clip_response("anything", "group-1", "en", "session-1")

    assert result.no_story is True
    assert result.video_url is None


async def test_assemble_video_clip_response_cache_hit_skips_ffmpeg_assembly(monkeypatch):
    chunk = _make_chunk("I worked as a teacher.")
    verified = vca.VerifiedChunk(chunk=chunk, answer_ranges=[(0.0, 2.0)])
    expanded = [vca.ExpandedClip(raw_segment_id="seg-1", start_sec=0.0, end_sec=2.0, source_chunk_id=chunk.id)]

    monkeypatch.setattr(vca.retrieval_service, "retrieve_chunks", AsyncMock(return_value=[chunk]))
    monkeypatch.setattr(vca, "_split_question_into_clauses", AsyncMock(return_value=["q"]))
    monkeypatch.setattr(vca, "_verify_and_pinpoint_chunk", AsyncMock(return_value=verified))
    monkeypatch.setattr(vca, "_expand_chunk_boundaries", AsyncMock(return_value=expanded))
    monkeypatch.setattr(vca.cache_service, "get", AsyncMock(return_value="https://cdn.example/cached.mp4"))
    assemble_mock = AsyncMock()
    monkeypatch.setattr(vca, "_assemble_and_upload_clip", assemble_mock)

    result = await vca.assemble_video_clip_response("what did you do for work?", "group-1", "en", "session-1")

    assert result.video_url == "https://cdn.example/cached.mp4"
    assemble_mock.assert_not_called()


async def test_assemble_video_clip_response_happy_path_uploads_and_caches(monkeypatch):
    chunk = _make_chunk("I worked as a teacher.", raw_segment_id="seg-1")
    verified = vca.VerifiedChunk(chunk=chunk, answer_ranges=[(0.0, 2.0)], covered_clause_indices=[0])
    expanded = [vca.ExpandedClip(raw_segment_id="seg-1", start_sec=0.0, end_sec=2.0, source_chunk_id=chunk.id)]

    monkeypatch.setattr(vca.retrieval_service, "retrieve_chunks", AsyncMock(return_value=[chunk]))
    monkeypatch.setattr(vca, "_split_question_into_clauses", AsyncMock(return_value=["what did you do for work?"]))
    monkeypatch.setattr(vca, "_verify_and_pinpoint_chunk", AsyncMock(return_value=verified))
    monkeypatch.setattr(vca, "_expand_chunk_boundaries", AsyncMock(return_value=expanded))
    monkeypatch.setattr(vca.cache_service, "get", AsyncMock(return_value=None))
    set_mock = AsyncMock()
    monkeypatch.setattr(vca.cache_service, "set", set_mock)
    monkeypatch.setattr(vca.cache_service, "add_visited", AsyncMock(return_value=True))
    monkeypatch.setattr(
        vca, "_assemble_and_upload_clip", AsyncMock(return_value="https://cdn.example/new.mp4")
    )

    result = await vca.assemble_video_clip_response("what did you do for work?", "group-1", "en", "session-1")

    assert result.video_url == "https://cdn.example/new.mp4"
    assert result.uncovered_clauses == []
    set_mock.assert_called_once()


async def test_assemble_video_clip_response_reports_uncovered_clauses(monkeypatch):
    chunk = _make_chunk("I worked as a teacher.", raw_segment_id="seg-1")
    # Only clause 0 is covered — clause 1 ("did you enjoy it") is not, by any chunk.
    verified = vca.VerifiedChunk(chunk=chunk, answer_ranges=[(0.0, 2.0)], covered_clause_indices=[0])
    expanded = [vca.ExpandedClip(raw_segment_id="seg-1", start_sec=0.0, end_sec=2.0, source_chunk_id=chunk.id)]

    monkeypatch.setattr(vca.retrieval_service, "retrieve_chunks", AsyncMock(return_value=[chunk]))
    monkeypatch.setattr(
        vca,
        "_split_question_into_clauses",
        AsyncMock(return_value=["what did you do for work", "did you enjoy it"]),
    )
    monkeypatch.setattr(vca, "_verify_and_pinpoint_chunk", AsyncMock(return_value=verified))
    monkeypatch.setattr(vca, "_expand_chunk_boundaries", AsyncMock(return_value=expanded))
    monkeypatch.setattr(vca.cache_service, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(vca.cache_service, "set", AsyncMock())
    monkeypatch.setattr(vca.cache_service, "add_visited", AsyncMock(return_value=True))
    monkeypatch.setattr(
        vca, "_assemble_and_upload_clip", AsyncMock(return_value="https://cdn.example/new.mp4")
    )

    result = await vca.assemble_video_clip_response(
        "what did you do for work, did you enjoy it", "group-1", "en", "session-1"
    )

    assert result.uncovered_clauses == ["did you enjoy it"]


async def test_assemble_video_clip_response_no_video_survives_assembly_returns_no_story(monkeypatch):
    chunk = _make_chunk("I worked as a teacher.", raw_segment_id="seg-1")
    verified = vca.VerifiedChunk(chunk=chunk, answer_ranges=[(0.0, 2.0)])
    expanded = [vca.ExpandedClip(raw_segment_id="seg-1", start_sec=0.0, end_sec=2.0, source_chunk_id=chunk.id)]

    monkeypatch.setattr(vca.retrieval_service, "retrieve_chunks", AsyncMock(return_value=[chunk]))
    monkeypatch.setattr(vca, "_split_question_into_clauses", AsyncMock(return_value=["q"]))
    monkeypatch.setattr(vca, "_verify_and_pinpoint_chunk", AsyncMock(return_value=verified))
    monkeypatch.setattr(vca, "_expand_chunk_boundaries", AsyncMock(return_value=expanded))
    monkeypatch.setattr(vca.cache_service, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(vca, "_assemble_and_upload_clip", AsyncMock(return_value=None))

    result = await vca.assemble_video_clip_response("anything", "group-1", "en", "session-1")

    assert result.no_story is True
    assert result.video_url is None
    assert result.fallback_text == NO_STORY_FALLBACK


# ── photo_categories_for_segments (MEDIA_GALLERY.md §9.4) ───────────────────


async def _seed_segment(factory, user_id: str, segment_id: str, question_id):
    async with factory() as db:
        session = InterviewSession(id=f"is-{segment_id}", user_id=user_id, status="active")
        db.add(session)
        await db.flush()
        db.add(
            RawSegment(
                id=segment_id,
                interview_session_id=session.id,
                question_asked="q",
                question_index=0,
                status="ready",
                question_id=question_id,
            )
        )
        await db.commit()


async def test_photo_categories_resolve_dedupe_and_keep_play_order(
    vca_session_factory, test_user
):
    """A LOOKUP through question_id, never a classification — and the order
    is the order the answer plays its recordings, deduped, so the /talk
    gallery leads with the period the clip opens in."""
    from app import interview_config

    cats = interview_config.get_categories("he")
    q_a = cats[0]["question_ids"][0]
    q_b = cats[1]["question_ids"][0]
    await _seed_segment(vca_session_factory, test_user.id, "seg-a", q_a)
    await _seed_segment(vca_session_factory, test_user.id, "seg-b", q_b)
    await _seed_segment(vca_session_factory, test_user.id, "seg-a2", q_a)

    # b first, then a twice — the result is b's category then a's, once each.
    result = await vca.photo_categories_for_segments(["seg-b", "seg-a", "seg-a2"])
    assert result == [cats[1]["category"], cats[0]["category"]]


async def test_photo_categories_skip_what_cannot_be_resolved(
    vca_session_factory, test_user
):
    """A segment with no question id (an upload outside the guided set), an
    invented id, or an unknown segment contributes NOTHING — the gallery
    never guesses a period (the year-attribution rule, applied to photos)."""
    await _seed_segment(vca_session_factory, test_user.id, "seg-none", None)
    await _seed_segment(vca_session_factory, test_user.id, "seg-bogus", "not-a-question-id")

    assert await vca.photo_categories_for_segments(
        ["seg-none", "seg-bogus", "seg-missing"]
    ) == []
    assert await vca.photo_categories_for_segments([]) == []
