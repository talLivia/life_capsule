"""
Tests for full_archive_retrieval.py (Prompt 15, the experimental "video_clips_v2"
full-archive-reading mode). Graphiti/LLM/DB side dependencies are mocked;
this suite verifies the module's own logic — transcript formatting,
deterministic range validation + word-boundary snapping, empty-archive and
no-answer handling, and the orchestrator's reuse of the existing
assembly/caching. Existing avatar-path tests are unaffected
(this module is purely additive and imports the v1 assembly code without
modifying it).
"""

from unittest.mock import AsyncMock

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InterviewSession, RawSegment, TranscriptChunk
from app.services import entity_store
from app.services import full_archive_retrieval as ar
from app.services import retrieval_service
from app.services import response_assembler as ra
from app.services.response_assembler import (
    NO_STORY_FALLBACK,
    TRANSIENT_FAILURE_FALLBACK,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def ar_session_factory(test_engine, monkeypatch):
    """Autouse, and pinning EVERY module this file's code paths open a
    session through — not just ar's own:

    - video_clip_assembler: v2's success path calls its
      photo_categories_for_segments, which opens that module's factory;
    - retrieval_service: select_units runs _recent_turns through ITS factory.

    Before this was autouse, tests that didn't request it quietly ran those
    reads against the REAL configured database — which surfaced as an
    order-dependent "attached to a different loop" failure the moment an
    earlier test file had used the real engine's pool on its own event loop.
    No test in this file should ever reach that engine."""
    from app.services import retrieval_service as _rsvc
    from app.services import video_clip_assembler as _vca

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(ar, "AsyncSessionLocal", factory)
    monkeypatch.setattr(_vca, "AsyncSessionLocal", factory)
    monkeypatch.setattr(_rsvc, "AsyncSessionLocal", factory)
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


_QUESTION_INDICES: dict = {}


def _segment(seg_id: str, question: str, question_index: int = None) -> RawSegment:
    """question_index defaults to one per DISTINCT question text.

    It used to be hardcoded to 0, which was harmless when a question could
    only have one recording. Now question_index is what identifies takes of
    the same question, so a fixture that gave every segment index 0 would
    quietly declare every test recording a sibling of every other. Deriving
    it from the question text keeps the fixture honest by default: same
    question text = siblings, different text = different questions. Pass it
    explicitly to build siblings that word the question differently.
    """
    if question_index is None:
        question_index = _QUESTION_INDICES.setdefault(question, len(_QUESTION_INDICES))
    return RawSegment(
        id=seg_id,
        interview_session_id="int-1",
        question_asked=question,
        question_index=question_index,
        status="ready",
    )


# ── utterance units: splitting ───────────────────────────────────────────────


def _gapped_words(spec: list, start_sec: float = 0.0, word_dur: float = 0.4) -> list:
    """Build word_timestamps from (word, gap_before_sec) pairs so tests can
    place REAL pauses — the plain _word_timestamps helper emits contiguous
    words (all gaps 0), which by design never splits."""
    out = []
    t = start_sec
    for word, gap in spec:
        t += gap
        out.append({"word": word, "start_sec": round(t, 3), "end_sec": round(t + word_dur, 3)})
        t += word_dur
    return out


def _paced_words(n_words: int, break_after: list, start_sec: float = 0.0,
                 small: float = 0.02, big: float = 1.0) -> list:
    """n_words with a SMALL gap between each, and a BIG gap after each index in
    `break_after`. Because the threshold is a high percentile of the gap
    distribution, big gaps only register as unit breaks when they are genuine
    outliers — so keep len(break_after) well under 10% of the gaps."""
    spec = []
    for i in range(n_words):
        if i == 0:
            gap = 0.0
        elif (i - 1) in break_after:
            gap = big
        else:
            gap = small
        spec.append(("w%d" % i, gap))
    return _gapped_words(spec, start_sec=start_sec)


def _chunk_with(seg_id: str, seq: int, words: list, text: str):
    return TranscriptChunk(
        raw_segment_id=seg_id,
        start_sec=words[0]["start_sec"],
        end_sec=words[-1]["end_sec"],
        text=text,
        sequence_index=seq,
        word_timestamps=words,
    )


def test_percentile_interpolates():
    assert ar._percentile([0.0, 1.0], 50) == 0.5
    assert ar._percentile([5.0], 90) == 5.0
    assert ar._percentile([], 90) == 0.0


def test_gap_threshold_adapts_to_the_recordings_own_distribution():
    """A fast talker and a slow talker must get DIFFERENT thresholds from the
    same code — that's the point of deriving it per recording."""
    fast = [_chunk_with("s", 0, _gapped_words(
        [("a", 0.0), ("b", 0.02), ("c", 0.02), ("d", 0.02), ("e", 0.30)]), "fast")]
    slow = [_chunk_with("s", 0, _gapped_words(
        [("a", 0.0), ("b", 0.20), ("c", 0.20), ("d", 0.20), ("e", 1.20)]), "slow")]
    t_fast = ar._segment_gap_threshold(fast)
    t_slow = ar._segment_gap_threshold(slow)
    assert t_fast is not None and t_slow is not None
    assert t_slow > t_fast


def test_gap_threshold_none_when_too_few_gaps():
    one_word = [_chunk_with("s", 0, _gapped_words([("a", 0.0)]), "a")]
    assert ar._segment_gap_threshold(one_word) is None


def test_split_breaks_at_long_pause_only():
    words = _paced_words(21, break_after=[9])  # one long pause after w9
    item = ar.ArchiveSegment(
        segment=_segment("seg-a", "Q"), chunks=[_chunk_with("seg-a", 0, words, "text")]
    )
    units, nxt = ar._split_segment_into_units(item, 1)
    assert len(units) == 2
    assert units[0].unit_id == "u1" and units[1].unit_id == "u2"
    assert nxt == 3
    # The break lands exactly at the pause, and times come straight from the
    # word timestamps — exact, never estimated.
    assert units[0].end_sec == words[9]["end_sec"]
    assert units[1].start_sec == words[10]["start_sec"]
    assert units[0].start_sec == words[0]["start_sec"]
    assert units[1].end_sec == words[-1]["end_sec"]


def test_split_uniform_pacing_yields_one_unit():
    """No gap exceeds the percentile of an all-equal distribution, so a
    steadily-paced passage is NOT chopped up arbitrarily."""
    words = _paced_words(12, break_after=[])  # perfectly even pacing
    item = ar.ArchiveSegment(
        segment=_segment("seg-a", "Q"), chunks=[_chunk_with("seg-a", 0, words, "text")]
    )
    units, _ = ar._split_segment_into_units(item, 1)
    assert len(units) == 1


def test_chunk_without_word_timings_becomes_one_unit():
    item = ar.ArchiveSegment(
        segment=_segment("seg-a", "Q"),
        chunks=[_chunk("seg-a", 0, 5.0, 8.0, "no timings here", with_words=False)],
    )
    units, _ = ar._split_segment_into_units(item, 1)
    assert len(units) == 1
    assert (units[0].start_sec, units[0].end_sec) == (5.0, 8.0)


def test_units_are_numbered_across_the_whole_archive():
    """Cross-recording linking depends on ONE id namespace spanning every
    recording, not per-segment numbering."""
    archive = [
        ar.ArchiveSegment(
            segment=_segment("seg-a", "Q1"),
            chunks=[_chunk_with("seg-a", 0, _paced_words(11, break_after=[5]), "a")],
        ),
        ar.ArchiveSegment(
            segment=_segment("seg-b", "Q2"),
            chunks=[_chunk_with("seg-b", 0, _paced_words(11, break_after=[5]), "b")],
        ),
    ]
    units = ar._build_units(archive)
    assert [u.unit_id for u in units] == ["u1", "u2", "u3", "u4"]
    assert [u.segment_id for u in units] == ["seg-a", "seg-a", "seg-b", "seg-b"]


# ── _format_annotated_transcript ─────────────────────────────────────────────


def test_format_annotated_transcript_lists_numbered_units_with_exact_times():
    archive = [
        ar.ArchiveSegment(
            segment=_segment("seg-a", "Tell me about your family"),
            chunks=[_chunk_with("seg-a", 0, _paced_words(11, break_after=[5]), "text")],
        ),
    ]
    units = ar._build_units(archive)
    out = ar._format_annotated_transcript(archive, units)

    # Segment UUIDs must NOT appear — they were the only other id-shaped
    # thing in the prompt and the model would sometimes return one instead
    # of a unit id, silently becoming a no-story.
    assert "seg-a" not in out
    assert "RECORDING 1" in out
    assert "interview question: Tell me about your family" in out
    assert "u1 [" in out and "u2 [" in out
    # Word-level timings are deliberately NOT exposed any more — the model
    # selects units, so time arithmetic must not be invited back in.
    assert "word timings:" not in out


# ── _format_entity_map ───────────────────────────────────────────────────────


def test_format_entity_map_empty():
    assert ar._format_entity_map({}, {}) == "(none extracted)"


def test_format_entity_map_prints_recording_ordinals_not_uuids():
    """THE fix. This block used to print raw segment UUIDs while the
    transcript block labelled the same recordings RECORDING 1..N, so the
    model could read "Ilana: 502fb283…" and had no way to discover that was
    RECORDING 1. Every pointer here was unresolvable."""
    out = ar._format_entity_map(
        {"Ilana": ["seg-a"], "Nir": ["seg-a", "seg-b"]},
        {"seg-a": 1, "seg-b": 2},
    )
    assert out == "- Ilana: RECORDING 1\n- Nir: RECORDING 1, RECORDING 2"
    assert "seg-a" not in out


def test_format_entity_map_omits_entities_whose_recordings_are_not_printed():
    """A recording whose units were all filtered out gets no RECORDING
    heading, so a pointer to it is the same unresolvable reference in a
    quieter form — omit the entity rather than invent a number."""
    out = ar._format_entity_map(
        {"Ilana": ["seg-a"], "Ghost": ["seg-missing"]}, {"seg-a": 1}
    )
    assert out == "- Ilana: RECORDING 1"


def test_format_entity_map_is_empty_when_nothing_is_resolvable():
    out = ar._format_entity_map({"Ghost": ["seg-missing"]}, {"seg-a": 1})
    assert out == "(none extracted)"


def test_entity_map_ordinals_match_the_headings_the_transcript_prints():
    """The invariant the whole fix rests on: a RECORDING number in the entity
    map must be the SAME recording the transcript block gave that number to.
    Both derive from _recording_ordinals so they cannot drift — this asserts
    it against the rendered text rather than trusting the shared call."""
    archive = [
        ar.ArchiveSegment(
            segment=_segment("seg-a", "About your childhood"),
            chunks=[_chunk("seg-a", 0, 0.0, 2.0, "I grew up in Tiberias")],
        ),
        ar.ArchiveSegment(
            segment=_segment("seg-b", "About the army"),
            chunks=[_chunk("seg-b", 0, 0.0, 2.0, "I served in the air force")],
        ),
    ]
    units = ar._build_units(archive)
    ordinals = ar._recording_ordinals(archive, units)

    transcript = ar._format_annotated_transcript(archive, units)
    entity_block = ar._format_entity_map({"Tiberias": ["seg-a"]}, ordinals)

    # The entity map points at RECORDING 1, and RECORDING 1's heading in the
    # transcript is the childhood recording — the one that names Tiberias.
    assert entity_block == "- Tiberias: RECORDING 1"
    heading = next(
        line for line in transcript.splitlines() if line.startswith("RECORDING 1")
    )
    assert "About your childhood" in heading


# ── _parse_unit_selection ────────────────────────────────────────────────────


def test_parse_unit_selection_object_form():
    assert ar._parse_unit_selection('{"unit_ids": ["u3", "u4"]}') == (["u3", "u4"], 0)


def test_parse_unit_selection_bare_array_fallback():
    assert ar._parse_unit_selection('["u3", "u4"]') == (["u3", "u4"], 0)


def test_parse_unit_selection_empty():
    assert ar._parse_unit_selection('{"unit_ids": []}') == ([], 0)


def test_parse_unit_selection_non_json_returns_empty():
    assert ar._parse_unit_selection("I could not find anything.") == ([], 0)


def test_parse_unit_selection_extracts_from_surrounding_text():
    assert ar._parse_unit_selection('Here: {"unit_ids": ["u7"]} done') == (["u7"], 0)


def test_parse_unit_selection_tolerates_bare_numbers():
    assert ar._parse_unit_selection('{"unit_ids": [7, "u8"]}') == (["u7", "u8"], 0)


# ── resolve_units_to_clips ───────────────────────────────────────────────────


def _units_fixture():
    """seg-a -> u1,u2,u3 ; seg-b -> u4,u5 (21/11 words so the inserted long
    pauses are true outliers above the percentile)."""
    a_words = _paced_words(21, break_after=[6, 13])
    b_words = _paced_words(11, break_after=[5])
    archive = [
        ar.ArchiveSegment(
            segment=_segment("seg-a", "Q1"),
            chunks=[_chunk_with("seg-a", 0, a_words, "a")],
        ),
        ar.ArchiveSegment(
            segment=_segment("seg-b", "Q2"),
            chunks=[_chunk_with("seg-b", 0, b_words, "b")],
        ),
    ]
    units = ar._build_units(archive)
    assert [u.unit_id for u in units] == ["u1", "u2", "u3", "u4", "u5"], (
        "fixture must split as expected, got %r" % ([u.unit_id for u in units],)
    )
    return units


def test_resolve_units_drops_unknown_ids():
    units = _units_fixture()
    assert ar.resolve_units_to_clips(["nope"], units) == []


def test_resolve_units_merges_consecutive_into_one_clip():
    units = _units_fixture()
    clips = ar.resolve_units_to_clips(["u1", "u2"], units)
    assert len(clips) == 1
    assert clips[0].start_sec == units[0].start_sec
    assert clips[0].end_sec == units[1].end_sec


def test_resolve_units_keeps_non_consecutive_separate():
    units = _units_fixture()
    clips = ar.resolve_units_to_clips(["u1", "u3"], units)
    assert len(clips) == 2
    assert clips[0].end_sec == units[0].end_sec
    assert clips[1].start_sec == units[2].start_sec


def test_resolve_units_does_not_merge_across_segments():
    """u3 (seg-a) and u4 (seg-b) are consecutive by index but different
    recordings — merging them would splice unrelated footage."""
    units = _units_fixture()
    clips = ar.resolve_units_to_clips(["u3", "u4"], units)
    assert len(clips) == 2
    assert [c.raw_segment_id for c in clips] == ["seg-a", "seg-b"]


def test_resolve_units_preserves_model_order_and_dedupes():
    units = _units_fixture()
    clips = ar.resolve_units_to_clips(["u4", "u1", "u4"], units)
    assert [c.raw_segment_id for c in clips] == ["seg-b", "seg-a"]
    assert all(c.source_chunk_id.startswith("archive-read:") for c in clips)


def _reference_resolve_units_to_clips(unit_ids, units):
    """FROZEN copy of resolve_units_to_clips as it stood BEFORE the
    _group_selected_runs extraction (AVATAR_SHARED_ENGINE_PLAN §1.2) — the
    oracle proving the split changed nothing. Deliberately verbatim, not
    simplified: the old implementation itself is the spec."""
    by_id = {u.unit_id: u for u in units}

    selected = []
    seen = set()
    for uid in unit_ids:
        unit = by_id.get(uid)
        if unit is None:
            continue
        if uid in seen:
            continue
        seen.add(uid)
        selected.append(unit)

    clips = []
    prev = None
    for unit in selected:
        if (
            prev is not None
            and unit.segment_id == prev.segment_id
            and unit.index == prev.index + 1
        ):
            clips[-1] = ar.ExpandedClip(
                raw_segment_id=clips[-1].raw_segment_id,
                start_sec=clips[-1].start_sec,
                end_sec=unit.end_sec,
                source_chunk_id=clips[-1].source_chunk_id,
            )
        else:
            clips.append(
                ar.ExpandedClip(
                    raw_segment_id=unit.segment_id,
                    start_sec=unit.start_sec,
                    end_sec=unit.end_sec,
                    source_chunk_id=f"archive-read:{unit.segment_id}",
                )
            )
        prev = unit
    return clips


def test_resolve_units_matches_the_pre_extraction_oracle():
    """The §1.2 equivalence proof: the post-split implementation must equal
    the frozen pre-split implementation elementwise over every named edge
    case — including reversed adjacency, which no other test pins — plus
    every 2- and 3-permutation of the fixture's five units."""
    from itertools import permutations

    units = _units_fixture()
    ids = [u.unit_id for u in units]

    cases = [
        [],
        ["nope"],
        ["u1"],
        ["u1", "u2"],
        ["u1", "u2", "u3"],
        ["u1", "u3"],
        ["u2", "u1"],          # reversed adjacency — two clips, directional check
        ["u3", "u4"],          # cross-segment index adjacency — never merges
        ["u4", "u1", "u4"],    # duplicate dropped entirely
        ["u1", "u1"],
        ["u5", "u4"],
        ["u1", "nope", "u2"],  # unknown id must not break a run
        ids,                    # everything, in file order
        list(reversed(ids)),
    ]
    cases += [list(p) for p in permutations(ids, 2)]
    cases += [list(p) for p in permutations(ids, 3)]

    for seq in cases:
        assert ar.resolve_units_to_clips(seq, units) == _reference_resolve_units_to_clips(
            seq, units
        ), seq


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


def _resolvable_archive():
    """Archive whose words split into real units (see _units_fixture)."""
    return [
        ar.ArchiveSegment(
            segment=_segment("seg-a", "Q1"),
            chunks=[_chunk_with("seg-a", 0, _paced_words(21, break_after=[6, 13]), "a")],
        ),
        ar.ArchiveSegment(
            segment=_segment("seg-b", "Q2"),
            chunks=[_chunk_with("seg-b", 0, _paced_words(11, break_after=[5]), "b")],
        ),
    ]


async def test_read_and_validate_ranges_no_answer_returns_empty(monkeypatch):
    archive = _resolvable_archive()
    monkeypatch.setattr(ar, "_load_archive", AsyncMock(return_value=archive))
    monkeypatch.setattr(ar, "_build_entity_map", AsyncMock(return_value={}))
    monkeypatch.setattr(retrieval_service, "_recent_turns", AsyncMock(return_value=[]))
    monkeypatch.setattr(ar, "_read_archive_for_ranges", AsyncMock(return_value=ar.ArchiveRead(unit_ids=[])))

    result = await ar.read_and_validate_ranges("q", "group", "he", "sess")
    assert result == []


async def test_read_and_validate_ranges_happy_path_returns_validated_clips(monkeypatch):
    archive = _resolvable_archive()
    monkeypatch.setattr(ar, "_load_archive", AsyncMock(return_value=archive))
    monkeypatch.setattr(ar, "_build_entity_map", AsyncMock(return_value={"Nir": ["seg-a"]}))
    monkeypatch.setattr(retrieval_service, "_recent_turns", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ar,
        "_read_archive_for_ranges",
        AsyncMock(return_value=ar.ArchiveRead(unit_ids=["u1"])),
    )

    result = await ar.read_and_validate_ranges("q", "group", "he", "sess")
    units = ar._build_units(archive)
    assert len(result) == 1
    assert result[0].raw_segment_id == "seg-a"
    # Times are DERIVED from the selected unit — nothing to snap.
    assert (result[0].start_sec, result[0].end_sec) == (units[0].start_sec, units[0].end_sec)


# ── assemble_video_clip_response_v2 orchestration ────────────────────────────


async def test_assemble_v2_no_ranges_returns_no_story(monkeypatch):
    monkeypatch.setattr(
        ar, "select_units",
        AsyncMock(return_value=ar.UnitSelection(clips=[], selected_units=[])),
    )

    result = await ar.assemble_video_clip_response_v2("q", "group", "he", "sess")
    assert result.no_story is True
    assert result.video_url is None
    assert result.fallback_text == NO_STORY_FALLBACK


async def test_assemble_v2_cache_hit_skips_ffmpeg(monkeypatch):
    clips = [ar.ExpandedClip(raw_segment_id="seg-a", start_sec=0.0, end_sec=1.2,
                             source_chunk_id="archive-read:seg-a")]
    monkeypatch.setattr(
        ar, "select_units",
        AsyncMock(return_value=ar.UnitSelection(clips=clips, selected_units=[])),
    )
    monkeypatch.setattr(ar.cache_service, "get", AsyncMock(return_value="https://cdn/cached.mp4"))
    assemble_mock = AsyncMock()
    monkeypatch.setattr(ar.video_clip_assembler, "_assemble_and_upload_clip", assemble_mock)

    result = await ar.assemble_video_clip_response_v2("q", "group", "he", "sess")
    assert result.video_url == "https://cdn/cached.mp4"
    assemble_mock.assert_not_called()


async def test_assemble_v2_happy_path_assembles_and_caches(monkeypatch):
    clips = [ar.ExpandedClip(raw_segment_id="seg-a", start_sec=0.0, end_sec=1.2,
                             source_chunk_id="archive-read:seg-a")]
    monkeypatch.setattr(
        ar, "select_units",
        AsyncMock(return_value=ar.UnitSelection(clips=clips, selected_units=[])),
    )
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
    monkeypatch.setattr(
        ar, "select_units",
        AsyncMock(return_value=ar.UnitSelection(clips=clips, selected_units=[])),
    )
    monkeypatch.setattr(ar.cache_service, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ar.video_clip_assembler, "_assemble_and_upload_clip", AsyncMock(return_value=None)
    )

    result = await ar.assemble_video_clip_response_v2("q", "group", "he", "sess")
    assert result.no_story is True
    assert result.fallback_text == NO_STORY_FALLBACK


# ── _expand_about_passages (deterministic passage completion) ────────────────
#
# The shapes mirror the live archive this was built against: one recording
# that is a single passage end to end (the friend's army recording), and one
# recording holding TWO passages separated by a >2s pause (family enumeration,
# then the uncle's story). The person boundary bar is the whole point: the
# 323f88d prompt bullet was reverted for redirecting an answer onto the wrong
# same-named person, and expansion must be structurally unable to do that.


def _passage_units():
    """seg-army: u1-u4, one passage (gaps <= 1.0s).
    seg-uncle: u5,u6 | 3.5s pause | u7,u8 — two passages.
    seg-other: u9, someone else's recording."""
    return [
        ar.UtteranceUnit("u1", "seg-army", 1, 0.0, 2.0, "בשירות הצבאי"),
        ar.UtteranceUnit("u2", "seg-army", 2, 2.5, 4.0, "שלוש שנים"),
        ar.UtteranceUnit("u3", "seg-army", 3, 5.0, 7.0, "חברים טובים"),
        ar.UtteranceUnit("u4", "seg-army", 4, 7.5, 9.0, "יש את אמנון"),
        ar.UtteranceUnit("u5", "seg-uncle", 5, 0.0, 2.0, "אנחנו חמישה"),
        ar.UtteranceUnit("u6", "seg-uncle", 6, 2.3, 4.0, "עדי ורז"),
        ar.UtteranceUnit("u7", "seg-uncle", 7, 7.5, 9.0, "יש לי דוד אמנון"),
        ar.UtteranceUnit("u8", "seg-uncle", 8, 9.2, 10.0, "בר ודור"),
        ar.UtteranceUnit("u9", "seg-other", 9, 0.0, 3.0, "טבריה"),
    ]


def test_expand_about_completes_a_single_passage_recording():
    out = ar._expand_about_passages(
        ["u4"], "אמנון", {"אמנון": ["seg-army"]}, _passage_units()
    )
    assert out == ["u1", "u2", "u3", "u4"]


def test_expand_about_stops_at_a_passage_boundary():
    # The naming unit sits in the SECOND passage of the uncle's recording;
    # the family enumeration before the 3.5s pause must not ride along.
    out = ar._expand_about_passages(
        ["u7"], "אמנון נחום", {"אמנון נחום": ["seg-uncle"]}, _passage_units()
    )
    assert out == ["u7", "u8"]


def test_expand_about_cannot_reach_another_persons_recording():
    # `about` names the friend, but every selected unit is in the uncle's
    # recording — the intersection is empty and expansion is a no-op. This is
    # the structural person-boundary guarantee.
    out = ar._expand_about_passages(
        ["u7"], "אמנון", {"אמנון": ["seg-army"], "אמנון נחום": ["seg-uncle"]},
        _passage_units(),
    )
    assert out == ["u7"]


def test_expand_about_never_adds_a_recording_the_model_did_not_pick():
    # The friend's entity spans two recordings; only one was selected from.
    # The other must not appear, however related it is.
    out = ar._expand_about_passages(
        ["u4"], "אמנון", {"אמנון": ["seg-army", "seg-uncle"]}, _passage_units()
    )
    assert out == ["u1", "u2", "u3", "u4"]


def test_expand_about_leaves_other_selected_recordings_untouched():
    # A unit selected OUTSIDE the named person's recordings stays exactly
    # where the model ordered it, unexpanded.
    out = ar._expand_about_passages(
        ["u9", "u4"], "אמנון", {"אמנון": ["seg-army"]}, _passage_units()
    )
    assert out == ["u9", "u1", "u2", "u3", "u4"]


def test_expand_about_noop_without_about_or_resolution():
    units = _passage_units()
    assert ar._expand_about_passages(["u4"], None, {"אמנון": ["seg-army"]}, units) == ["u4"]
    # A name the archive does not hold resolves to nothing — same contract as
    # the no-story line: never act on a name the archive cannot confirm.
    assert ar._expand_about_passages(["u4"], "משה", {"אמנון": ["seg-army"]}, units) == ["u4"]
    assert ar._expand_about_passages([], "אמנון", {"אמנון": ["seg-army"]}, units) == []


def test_expand_about_dedupes_units_sharing_a_passage():
    out = ar._expand_about_passages(
        ["u3", "u4"], "אמנון", {"אמנון": ["seg-army"]}, _passage_units()
    )
    assert out == ["u1", "u2", "u3", "u4"]


def test_expand_about_keeps_unknown_ids_for_downstream_validation():
    # Unknown ids are resolve_units_to_clips' job to drop (with its warning);
    # expansion passes them through rather than silently swallowing them.
    out = ar._expand_about_passages(
        ["nope", "u4"], "אמנון", {"אמנון": ["seg-army"]}, _passage_units()
    )
    assert out == ["nope", "u1", "u2", "u3", "u4"]


async def test_select_units_expands_about_passages_end_to_end(monkeypatch):
    """The expansion reaches clips, selected_units AND follow-up validation:
    a follow-up offering a unit the expansion just added is no longer 'more'
    and must be dropped."""
    units = _passage_units()
    archive = [
        ar.ArchiveSegment(segment=_segment("seg-army", "Q1"), chunks=[]),
        ar.ArchiveSegment(segment=_segment("seg-uncle", "Q2"), chunks=[]),
        ar.ArchiveSegment(segment=_segment("seg-other", "Q3"), chunks=[]),
    ]
    monkeypatch.setattr(
        ar, "_archive_bundle",
        AsyncMock(return_value=(archive, {"אמנון": ["seg-army"]}, units, {})),
    )
    monkeypatch.setattr(ar, "_load_shown_units", AsyncMock(return_value=(set(), [])))
    monkeypatch.setattr(retrieval_service, "_recent_turns", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ar, "_read_archive_for_ranges",
        AsyncMock(return_value=ar.ArchiveRead(
            unit_ids=["u4"],
            about="אמנון",
            follow_up={"question": "עוד?", "unit_ids": ["u2"]},
        )),
    )

    selection = await ar.select_units("ספר לי על אמנון", "group", "he", "sess")

    assert [u.unit_id for u in selection.selected_units] == ["u1", "u2", "u3", "u4"]
    assert len(selection.clips) == 1  # one continuous passage -> one clip
    assert selection.follow_up is None  # u2 is now part of the answer
    assert selection.no_story_text is None  # about with units never names a no-story


# ── _read_archive_for_ranges (LLM call structure + fail-soft) ─────────────────


async def test_read_archive_for_ranges_static_transcript_in_system_variable_question_in_user(
    monkeypatch,
):
    """Prompt-cache ordering: the large static archive must be in the
    system prompt, the variable question in the user message."""
    captured = {}

    async def fake_generate(messages, system_prompt=None, thinking=False,
                            temperature=None, **kwargs):
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

    async def fake_generate(messages, system_prompt=None, thinking=False,
                            temperature=None, **kwargs):
        captured["messages"] = messages
        return "[]"

    monkeypatch.setattr(ar.llm_service, "generate_response", fake_generate)
    turns = [{"role": "user", "content": "who most influenced you?"},
             {"role": "assistant", "content": "http://x/clip.mp4"}]
    history_block = ar._format_history_block(turns, [])

    await ar._read_archive_for_ranges("is he still alive?", "T", "E", history_block, "he")

    user_content = captured["messages"][0]["content"]
    assert "who most influenced you?" in user_content
    # Video-URL assistant turn masked, not leaked as narration.
    assert "http://x/clip.mp4" not in user_content
    assert "(showed a video clip)" in user_content


async def test_read_archive_for_ranges_fails_soft_on_llm_error(monkeypatch):
    monkeypatch.setattr(
        ar.llm_service, "generate_response", AsyncMock(side_effect=RuntimeError("down"))
    )
    result = await ar._read_archive_for_ranges("q", "T", "E", "", "he")
    # Fail-soft still, but the failure is REPORTED rather than disguised as
    # an empty selection — the whole point of ArchiveRead.failed.
    assert result.unit_ids == []
    assert result.failed is True


# ── already-shown handling (visited-set wired into v2) ───────────────────────


async def test_assemble_v2_still_answers_when_units_were_already_shown(monkeypatch):
    """A standalone question ALWAYS gets a real answer, even when the units
    answering it were played earlier ("tell me about your family" then "who is
    your mother?"). Already-shown is an ordering hint, never a block."""
    clips = [ar.ExpandedClip(raw_segment_id="seg-a", start_sec=0.0, end_sec=1.2,
                             source_chunk_id="archive-read:seg-a")]
    monkeypatch.setattr(
        ar, "select_units",
        AsyncMock(return_value=ar.UnitSelection(clips=clips, selected_units=[])),
    )
    monkeypatch.setattr(ar.cache_service, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(ar.cache_service, "set", AsyncMock(return_value=True))
    monkeypatch.setattr(ar.cache_service, "add_visited", AsyncMock(return_value=True))
    monkeypatch.setattr(ar.video_clip_assembler, "_assemble_and_upload_clip",
                        AsyncMock(return_value="https://cdn/x.mp4"))

    result = await ar.assemble_video_clip_response_v2("q", "group", "he", "sess")
    assert result.no_story is False
    assert result.video_url == "https://cdn/x.mp4"


def test_transcript_marks_already_shown_units():
    archive = [
        ar.ArchiveSegment(
            segment=_segment("seg-a", "Q1"),
            chunks=[_chunk_with("seg-a", 0, _paced_words(11, break_after=[5]), "a")],
        ),
    ]
    units = ar._build_units(archive)
    shown = {ar._unit_key(units[0].segment_id, units[0].start_sec)}
    out = ar._format_annotated_transcript(archive, units, shown)
    lines = [ln for ln in out.splitlines() if ln.strip().startswith("u")]
    assert "[ALREADY SHOWN]" in lines[0]
    assert "[ALREADY SHOWN]" not in lines[1]


def test_unit_key_is_stable_against_renumbering():
    """Unit ids are positional, so persisted history must key on something
    that survives a new recording being ingested."""
    assert ar._unit_key("seg-a", 3.3333) == ar._unit_key("seg-a", 3.3339)
    assert ar._unit_key("seg-a", 3.33) != ar._unit_key("seg-b", 3.33)


# ── history block carries what was actually played ──────────────────────────


def test_history_block_renders_played_unit_text():
    turns = [
        {"role": "user", "content": "how did you meet your wife?"},
        {"role": "assistant", "content": "http://x/clip.mp4"},
    ]
    per_turn = [[{"key": "seg-a:0.00", "unit_id": "u12", "text": "met her on an app"}]]
    out = ar._format_history_block(turns, per_turn)
    assert "how did you meet your wife?" in out
    assert "u12" in out and "met her on an app" in out
    assert "http://x/clip.mp4" not in out


def test_history_block_falls_back_when_units_unknown():
    turns = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "http://x/clip.mp4"},
    ]
    out = ar._format_history_block(turns, [])
    assert "(showed a video clip)" in out


def test_history_block_aligns_units_to_the_most_recent_turns():
    """Only the last N turns are in the window; unit lists align from the end."""
    turns = [
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "http://x/2.mp4"},
    ]
    per_turn = [
        [{"key": "k1", "unit_id": "u1", "text": "older answer"}],
        [{"key": "k2", "unit_id": "u2", "text": "newer answer"}],
    ]
    out = ar._format_history_block(turns, per_turn)
    assert "newer answer" in out
    assert "older answer" not in out


# ── proactive follow-up suggestion ──────────────────────────────────────────


def test_parse_follow_up_valid():
    raw = '{"unit_ids": ["u1"], "follow_up": {"question": "want more?", "unit_ids": ["u9"]}}'
    assert ar._parse_follow_up(raw) == {"question": "want more?", "unit_ids": ["u9"]}


def test_parse_follow_up_absent_or_null():
    assert ar._parse_follow_up('{"unit_ids": ["u1"]}') is None
    assert ar._parse_follow_up('{"unit_ids": ["u1"], "follow_up": null}') is None


def test_parse_follow_up_rejects_malformed():
    assert ar._parse_follow_up('{"follow_up": {"question": "", "unit_ids": ["u9"]}}') is None
    assert ar._parse_follow_up('{"follow_up": {"question": "q", "unit_ids": []}}') is None
    assert ar._parse_follow_up('{"follow_up": {"question": "q"}}') is None
    assert ar._parse_follow_up("not json") is None


def _fu_units():
    archive = [
        ar.ArchiveSegment(
            segment=_segment("seg-a", "Q1"),
            chunks=[_chunk_with("seg-a", 0, _paced_words(21, break_after=[6, 13]), "a")],
        ),
    ]
    return ar._build_units(archive)  # u1, u2, u3


def test_follow_up_kept_when_it_points_at_real_unseen_units():
    units = _fu_units()
    by_id = {u.unit_id: u for u in units}
    out = ar._validate_follow_up(
        {"question": "want to hear more?", "unit_ids": ["u3"]}, by_id, [units[0]], set()
    )
    assert out == {"question": "want to hear more?", "unit_ids": ["u3"]}


def test_follow_up_dropped_when_unit_unknown():
    """Never offer something the archive cannot actually show."""
    units = _fu_units()
    by_id = {u.unit_id: u for u in units}
    assert ar._validate_follow_up(
        {"question": "q", "unit_ids": ["u999"]}, by_id, [units[0]], set()
    ) is None


def test_follow_up_dropped_when_it_only_repeats_the_answer():
    units = _fu_units()
    by_id = {u.unit_id: u for u in units}
    assert ar._validate_follow_up(
        {"question": "q", "unit_ids": ["u1"]}, by_id, [units[0]], set()
    ) is None


def test_follow_up_dropped_when_material_already_shown():
    units = _fu_units()
    by_id = {u.unit_id: u for u in units}
    shown = {ar._unit_key(units[2].segment_id, units[2].start_sec)}
    assert ar._validate_follow_up(
        {"question": "q", "unit_ids": ["u3"]}, by_id, [units[0]], shown
    ) is None


async def test_assemble_v2_passes_follow_up_through(monkeypatch):
    clips = [ar.ExpandedClip(raw_segment_id="seg-a", start_sec=0.0, end_sec=1.2,
                             source_chunk_id="archive-read:seg-a")]
    monkeypatch.setattr(
        ar, "select_units",
        AsyncMock(return_value=ar.UnitSelection(
            clips=clips, selected_units=[],
            follow_up={"question": "want to hear how I knew?"})),
    )
    monkeypatch.setattr(ar.cache_service, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(ar.cache_service, "set", AsyncMock(return_value=True))
    monkeypatch.setattr(ar.cache_service, "add_visited", AsyncMock(return_value=True))
    monkeypatch.setattr(ar.video_clip_assembler, "_assemble_and_upload_clip",
                        AsyncMock(return_value="https://cdn/x.mp4"))

    result = await ar.assemble_video_clip_response_v2("q", "group", "he", "sess")
    assert result.follow_up == {"question": "want to hear how I knew?"}


# ── per-producer archive cache ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_archive_cache():
    """The cache is module-level state; leaking it between tests would make
    them order-dependent."""
    ar.invalidate_archive_cache()
    yield
    ar.invalidate_archive_cache()


async def test_archive_bundle_reuses_cache_when_version_unchanged(monkeypatch):
    archive = _resolvable_archive()
    loads = {"archive": 0, "entities": 0}

    async def fake_load(_gid):
        loads["archive"] += 1
        return archive

    async def fake_entities(_archive, _gid):
        loads["entities"] += 1
        return {"Nir": ["seg-a"]}

    monkeypatch.setattr(ar, "_archive_version", AsyncMock(return_value=(2, "t1", "t0")))
    monkeypatch.setattr(ar, "_load_archive", fake_load)
    monkeypatch.setattr(ar, "_build_entity_map", fake_entities)

    a1, e1, u1, t1 = await ar._archive_bundle("group-1")
    a2, e2, u2, t2 = await ar._archive_bundle("group-1")

    assert loads == {"archive": 1, "entities": 1}, "second call must not rebuild"
    assert a1 is a2 and e1 is e2 and u1 is u2
    assert u1, "units are cached alongside the archive"


async def test_archive_bundle_rebuilds_when_version_changes(monkeypatch):
    """A newly ingested recording changes the version, and the next question
    MUST see it — a stale cache would make a fresh story silently invisible."""
    archive = _resolvable_archive()
    loads = {"n": 0}

    async def fake_load(_gid):
        loads["n"] += 1
        return archive

    versions = iter([(2, "t1", "t0"), (3, "t2", "t0")])
    monkeypatch.setattr(ar, "_archive_version", AsyncMock(side_effect=lambda _g: next(versions)))
    monkeypatch.setattr(ar, "_load_archive", fake_load)
    monkeypatch.setattr(ar, "_build_entity_map", AsyncMock(return_value={}))

    await ar._archive_bundle("group-1")
    await ar._archive_bundle("group-1")

    assert loads["n"] == 2, "a version change must force a rebuild"


async def test_invalidate_archive_cache_forces_rebuild(monkeypatch):
    archive = _resolvable_archive()
    loads = {"n": 0}

    async def fake_load(_gid):
        loads["n"] += 1
        return archive

    monkeypatch.setattr(ar, "_archive_version", AsyncMock(return_value=(2, "t1", "t0")))
    monkeypatch.setattr(ar, "_load_archive", fake_load)
    monkeypatch.setattr(ar, "_build_entity_map", AsyncMock(return_value={}))

    await ar._archive_bundle("group-1")
    ar.invalidate_archive_cache("group-1")
    await ar._archive_bundle("group-1")

    assert loads["n"] == 2


async def test_archive_bundle_rebuilds_when_version_check_fails(monkeypatch):
    """If freshness can't be confirmed, rebuild rather than risk stale data."""
    archive = _resolvable_archive()
    loads = {"n": 0}

    async def fake_load(_gid):
        loads["n"] += 1
        return archive

    monkeypatch.setattr(ar, "_archive_version", AsyncMock(side_effect=RuntimeError("db down")))
    monkeypatch.setattr(ar, "_load_archive", fake_load)
    monkeypatch.setattr(ar, "_build_entity_map", AsyncMock(return_value={}))

    await ar._archive_bundle("group-1")
    await ar._archive_bundle("group-1")

    assert loads["n"] == 2, "unconfirmed freshness must not be cached"


async def test_empty_archive_is_not_cached(monkeypatch):
    monkeypatch.setattr(ar, "_archive_version", AsyncMock(return_value=(0, "t", "t")))
    monkeypatch.setattr(ar, "_load_archive", AsyncMock(return_value=[]))
    a, e, u, t = await ar._archive_bundle("group-empty")
    assert (a, e, u, t) == ([], {}, [], {})
    assert "group-empty" not in ar._ARCHIVE_CACHE


async def test_warm_archive_cache_populates_so_the_next_question_is_warm(monkeypatch):
    archive = _resolvable_archive()
    loads = {"n": 0}

    async def fake_load(_gid):
        loads["n"] += 1
        return archive

    monkeypatch.setattr(ar, "_archive_version", AsyncMock(return_value=(2, "t1", "t0")))
    monkeypatch.setattr(ar, "_load_archive", fake_load)
    monkeypatch.setattr(ar, "_build_entity_map", AsyncMock(return_value={}))

    assert await ar.warm_archive_cache("group-1") is True
    assert "group-1" in ar._ARCHIVE_CACHE

    await ar._archive_bundle("group-1")
    assert loads["n"] == 1, "the warm did the build; the question must reuse it"


async def test_warm_archive_cache_is_fail_soft(monkeypatch):
    """A failed warm must leave the old behaviour (next question rebuilds),
    never break ingestion or poison the cache."""
    monkeypatch.setattr(
        ar, "_archive_bundle", AsyncMock(side_effect=RuntimeError("neo4j unreachable"))
    )
    assert await ar.warm_archive_cache("group-1") is False
    assert "group-1" not in ar._ARCHIVE_CACHE


async def test_warm_archive_cache_bounded_by_timeout(monkeypatch):
    """A hung graph must not wedge the ingestion task forever."""
    async def never(_gid):
        await asyncio.sleep(30)

    monkeypatch.setattr(ar, "_archive_bundle", never)
    monkeypatch.setattr(ar, "_WARM_TIMEOUT_SECONDS", 0.05)

    assert await ar.warm_archive_cache("group-1") is False
    assert "group-1" not in ar._ARCHIVE_CACHE


# ── overlapping units ────────────────────────────────────────────────────────


def test_clamp_overlaps_trims_the_earlier_unit():
    """A word cannot still be sounding once the next has begun. Deepgram
    sometimes says otherwise (real case: "ורז" ended 14.64 while "להורים"
    started 13.82), and an overlap makes a stitched answer replay the same
    fraction of a second twice."""
    units = [
        ar.UtteranceUnit("u1", "seg-a", 1, 9.12, 14.64, "ניר חן עדי ורז"),
        ar.UtteranceUnit("u2", "seg-a", 2, 13.82, 16.23, "להורים שלי"),
    ]
    ar._clamp_overlaps(units)
    assert units[0].end_sec == 13.82, "earlier unit is trimmed"
    assert units[1].start_sec == 13.82, "later unit's start is never pushed out"


def test_clamp_overlaps_leaves_clean_units_alone():
    units = [
        ar.UtteranceUnit("u1", "seg-a", 1, 0.0, 2.0, "a"),
        ar.UtteranceUnit("u2", "seg-a", 2, 2.0, 4.0, "b"),
    ]
    ar._clamp_overlaps(units)
    assert (units[0].end_sec, units[1].start_sec) == (2.0, 2.0)


def test_clamp_overlaps_never_collapses_a_unit_to_nothing():
    """A total overlap would trim the earlier unit to zero length, which
    validation would then drop entirely — losing real speech is worse than
    keeping a small overlap."""
    units = [
        ar.UtteranceUnit("u1", "seg-a", 1, 5.0, 9.0, "a"),
        ar.UtteranceUnit("u2", "seg-a", 2, 4.0, 8.0, "b"),  # starts BEFORE u1
    ]
    ar._clamp_overlaps(units)
    assert units[0].end_sec == 9.0, "left untouched rather than zeroed"


def test_built_units_within_a_recording_never_overlap():
    archive = [
        ar.ArchiveSegment(
            segment=_segment("seg-a", "Q"),
            chunks=[_chunk_with("seg-a", 0, _paced_words(21, break_after=[6, 13]), "a")],
        ),
    ]
    units = ar._build_units(archive)
    for a, b in zip(units, units[1:]):
        assert a.end_sec <= b.start_sec, f"{a.unit_id} overlaps {b.unit_id}"


# ── siblings: several takes of one interview question ────────────────────────


def _take(seg_id: str, question: str, q_index: int) -> "ar.ArchiveSegment":
    return ar.ArchiveSegment(
        segment=_segment(seg_id, question, question_index=q_index),
        chunks=[_chunk_with(seg_id, 0, _paced_words(11, break_after=[5]), "text")],
    )


def test_single_take_question_prints_exactly_as_before():
    """No take marker when a question has one recording.

    Every archive recorded before siblings existed is entirely
    single-take, so this line must not change for them — the eval baseline
    is measured against it.
    """
    archive = [_take("seg-a", "Tell me about your family", 0)]
    out = ar._format_annotated_transcript(archive, ar._build_units(archive))
    assert "RECORDING 1 — interview question: Tell me about your family" in out
    assert "take" not in out


def test_takes_of_one_question_are_marked():
    archive = [
        _take("seg-a", "Tell me about the army", 3),
        _take("seg-b", "Tell me about the army", 3),
    ]
    out = ar._format_annotated_transcript(archive, ar._build_units(archive))
    assert "RECORDING 1 — interview question: Tell me about the army (take 1 of 2 of this question)" in out
    assert "RECORDING 2 — interview question: Tell me about the army (take 2 of 2 of this question)" in out


def test_take_count_ignores_a_sibling_with_nothing_to_print():
    """"take 1 of 2" would be a lie if the second take contributed no units
    the model can see — it counts what is actually printed, not what exists."""
    archive = [
        _take("seg-a", "Tell me about the army", 3),
        _take("seg-b", "Tell me about the army", 3),
    ]
    # Only seg-a's units survive (e.g. seg-b still transcribing).
    units = [u for u in ar._build_units(archive) if u.segment_id == "seg-a"]
    out = ar._format_annotated_transcript(archive, units)
    assert "take" not in out
    assert "RECORDING 2" not in out


def test_siblings_are_pulled_together_when_recorded_apart():
    """created_at order alone separates them: answer Q1, answer Q2, then go
    back and add a second take to Q1. Without grouping the model reads one
    interview question twice, in two places, with nothing tying them
    together."""
    archive = [
        _take("q1-take1", "About my childhood", 0),
        _take("q2-only", "About my career", 1),
        _take("q1-take2", "About my childhood", 0),
    ]
    grouped = ar._group_siblings(archive)
    assert [a.segment.id for a in grouped] == ["q1-take1", "q1-take2", "q2-only"]


def test_grouping_preserves_order_without_siblings():
    """Stable by design — an archive with one take per question comes out
    exactly as it went in, so this cannot silently re-cut existing
    archives."""
    archive = [
        _take("seg-a", "Q one", 0),
        _take("seg-b", "Q two", 1),
        _take("seg-c", "Q three", 2),
    ]
    assert [a.segment.id for a in ar._group_siblings(archive)] == ["seg-a", "seg-b", "seg-c"]


def test_units_keep_playback_order_within_a_question():
    """Grouping moves recordings, so unit numbering has to follow — u-ids
    are assigned over the grouped order, and a later take's units must come
    after the earlier take's, not interleave with them."""
    archive = ar._group_siblings([
        _take("q1-take1", "About my childhood", 0),
        _take("q2-only", "About my career", 1),
        _take("q1-take2", "About my childhood", 0),
    ])
    units = ar._build_units(archive)
    order = [u.segment_id for u in units]
    assert order.index("q1-take1") < order.index("q1-take2") < order.index("q2-only")


# ── Two people with one name (docs/ENTITY_DISAMBIGUATION.md step 3) ──────────


def _confusable(name, summary, segs):
    return entity_store.ConfusableEntity(
        entity_id=f"e-{name}", name=name, summary=summary, segment_ids=tuple(segs)
    )


_AMNON_GROUP = [
    _confusable("אמנון", "חבר של הדובר מהצבא", ["seg-friend"]),
    _confusable("אמנון נחום", "דוד של הדובר מצד אבא", ["seg-uncle"]),
]


def test_the_prompt_is_byte_identical_when_no_two_people_share_a_name():
    """THE CONTROL, and the strongest form it can take.

    The over-asking risk — a model taught to ask "which one?" starting to ask
    when the answer was obvious — is the likeliest way this feature makes
    things worse, and it would affect EVERY question rather than only the
    ambiguous ones. An archive with no repeated names cannot suffer it,
    because it is handed the same bytes it was handed before this existed.

    Asserted rather than reasoned about: the disambiguation text is inserted
    by string formatting, and "empty placeholder leaves an extra newline" is
    exactly the kind of difference that is invisible in review and would
    silently invalidate every measurement taken against this arm.
    """
    plain = ar._ARCHIVE_READER_SYSTEM_PROMPT_TEMPLATE.format(
        transcript_block="T", entity_map_block="E", disambiguation_block="",
        **ar._id_atoms(),
    )
    assert "TWO PEOPLE WITH THE SAME NAME" not in plain
    assert "clarify" not in plain
    # The exact shape the pre-feature template produced around this seam.
    assert "seem helpful.\n\nRules:" in plain

    tagged = ar._ARCHIVE_READER_SYSTEM_PROMPT_TEMPLATE.format(
        transcript_block="T", entity_map_block="E",
        disambiguation_block=ar._DISAMBIGUATION_BLOCK,
        **ar._id_atoms(),
    )
    assert "TWO PEOPLE WITH THE SAME NAME" in tagged
    # The output form must travel WITH the instruction, not as a trailing
    # rule. Placed at the end of the Rules section it sat straight after
    # "if nothing answers the question, output {"unit_ids": []}" — two
    # consecutive empty-answer examples, and `school` measured 8 units -> 0.
    assert '"clarify"' in tagged
    assert tagged.index('"clarify"') < tagged.index("Rules:")


def test_name_tags_attach_each_recording_to_the_person_it_is_actually_about():
    tags = ar._build_name_tags([_AMNON_GROUP])

    assert set(tags) == {"seg-friend", "seg-uncle"}
    assert "אמנון: חבר של הדובר מהצבא" == tags["seg-friend"][0].label
    assert "אמנון נחום: דוד של הדובר מצד אבא" == tags["seg-uncle"][0].label
    # Both surface forms are searched in BOTH recordings, longest first: the
    # uncle's transcript says the bare "אמנון" even though his row carries the
    # fuller name, which is the whole reason the producer had to give one.
    assert tags["seg-uncle"][0].surfaces == ("אמנון נחום", "אמנון")


def test_a_recording_about_both_people_is_left_untagged():
    """Nothing in the data says which occurrence is which.

    The mention links a RECORDING to a person, not a word to a person. When
    one recording links to both, a tag would be a guess — and a confident
    wrong label in front of the model is worse than the ambiguity it is
    meant to fix.
    """
    both = [
        _confusable("אמנון", "חבר", ["seg-both"]),
        _confusable("אמנון נחום", "דוד", ["seg-both"]),
    ]
    assert ar._build_name_tags([both]) == {}


def test_the_tag_is_written_next_to_the_name_including_hebrew_prefixes():
    tags = ar._build_name_tags([_AMNON_GROUP])["seg-uncle"]

    # Bare, and with the single-letter prefix Hebrew glues onto names.
    assert ar._annotate_names("יש לי דוד ושמו אמנון", tags) == (
        "יש לי דוד ושמו אמנון [אמנון נחום: דוד של הדובר מצד אבא]"
    )
    assert ar._annotate_names("הלכתי ואמנון בא", tags) == (
        "הלכתי ואמנון [אמנון נחום: דוד של הדובר מצד אבא] בא"
    )
    # A name that is not there is not invented.
    assert ar._annotate_names("שום שם כאן", tags) == "שום שם כאן"


def test_annotation_never_touches_the_stored_unit_text():
    """The tag exists for ONE LLM call and nowhere else.

    `unit.text` is what the answer's spoken text is assembled from and what
    gets persisted on the message. A tag reaching it would put a bracketed
    note in the chat beside a video that never says it — and the earlier
    name-correction work established that retrieval text is not ours to
    rewrite.
    """
    archive = [
        ar.ArchiveSegment(
            segment=_segment("seg-uncle", "ספר על המשפחה"),
            chunks=[_chunk("seg-uncle", 0, 0.0, 4.0, "הדוד שלי אמנון גר בתל אביב")],
        )
    ]
    units = ar._build_units(archive)
    before = [u.text for u in units]

    out = ar._format_annotated_transcript(
        archive, units, None, ar._build_name_tags([_AMNON_GROUP])
    )

    assert "[אמנון נחום: דוד של הדובר מצד אבא]" in out
    assert [u.text for u in units] == before
    assert all("[" not in t for t in before)


def test_clarify_is_ignored_when_the_prompt_never_offered_it():
    """A model inventing a key it was not told about must not be honoured.

    Without this, an archive with no same-named people could still be turned
    into a question-back by a model that decided to be helpful — the exact
    failure the byte-identical control exists to make impossible.
    """
    reply = '{"unit_ids": [], "clarify": {"question": "which?", "options": ["a", "b"]}}'
    assert ar._parse_clarify(reply) == {"question": "which?", "options": ["a", "b"]}
    # One option is not a choice.
    assert ar._parse_clarify('{"clarify": {"question": "q", "options": ["a"]}}') is None
    assert ar._parse_clarify('{"unit_ids": ["u1"]}') is None


async def test_a_clarification_is_not_a_no_story(monkeypatch):
    """The ordering that makes the feature legible instead of alarming.

    A clarification IS an empty selection, so falling through to the no-story
    branch would tell the listener the archive holds nothing about אמנון — at
    the exact moment it holds two of them and only needs to know which. The
    branch order is the whole fix, so it is pinned.
    """
    monkeypatch.setattr(
        ar,
        "select_units",
        AsyncMock(
            return_value=ar.UnitSelection(
                clips=[],
                selected_units=[],
                clarify={"question": "איזה אמנון?", "options": ["הדוד", "החבר"]},
            )
        ),
    )

    result = await ar.assemble_video_clip_response_v2("q", "group", "he", "sess")

    assert result.no_story is False
    assert result.fallback_text == ""
    assert result.video_url is None
    assert result.clarify == {"question": "איזה אמנון?", "options": ["הדוד", "החבר"]}


async def test_clarify_replaces_the_answer_rather_than_decorating_it(monkeypatch):
    """A guess plus "or did you mean the other one?" is still the conflation.

    The listener would receive one person's footage presented as the answer,
    with the question as a footnote. So units are dropped when a clarification
    is returned, even though the model sent both.
    """
    archive = _resolvable_archive()
    monkeypatch.setattr(ar, "_load_archive", AsyncMock(return_value=archive))
    monkeypatch.setattr(ar, "_build_entity_map", AsyncMock(return_value={}))
    monkeypatch.setattr(
        ar, "_build_name_tags_for", AsyncMock(return_value={"seg-a": []})
    )
    monkeypatch.setattr(retrieval_service, "_recent_turns", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ar,
        "_read_archive_for_ranges",
        AsyncMock(
            return_value=ar.ArchiveRead(
                unit_ids=["u1"], clarify={"question": "q?", "options": ["a", "b"]}
            )
        ),
    )
    ar.invalidate_archive_cache("group")

    selection = await ar.select_units("q", "group", "he", "sess")

    assert selection.clarify == {"question": "q?", "options": ["a", "b"]}
    assert selection.clips == []
    assert selection.selected_units == []


# ── "I don't have another story about אמנון" ─────────────────────────────────


def test_about_must_name_an_entity_the_archive_actually_holds():
    """A name the model produced is a CLAIM about the archive.

    "I don't have another story about X" asserts that X is someone this
    archive knows. A model answering "what pets did you have?" by naming a
    plausible-sounding pet would have us assert it — which is worse than the
    generic line, not better. So the name is checked against the real entity
    list, and the archive's OWN spelling is what gets shown.
    """
    entity_map = {"אמנון": ["seg-a", "seg-b"], "תל אביב": ["seg-c"]}

    assert ar._resolve_about("אמנון", entity_map) == ("אמנון", ["seg-a", "seg-b"])
    # Normalised match: a final-letter form or stray whitespace still resolves,
    # and the STORED spelling comes back, not the model's.
    assert ar._resolve_about("  אמנון ", entity_map)[0] == "אמנון"
    # Not in the archive -> generic line.
    assert ar._resolve_about("כלב", entity_map) is None
    assert ar._resolve_about(None, entity_map) is None
    assert ar._resolve_about("", entity_map) is None


def test_about_is_parsed_only_as_a_bare_name():
    assert ar._parse_about('{"unit_ids": [], "about": "אמנון"}') == "אמנון"
    assert ar._parse_about('{"unit_ids": [], "about": null}') is None
    assert ar._parse_about('{"unit_ids": []}') is None
    assert ar._parse_about("not json") is None


def test_no_story_wording_is_stable_for_a_given_question():
    """Same question, same sentence — always.

    The bank exists so repeats do not read robotically, but a RANDOM pick
    would make the retrieval eval flaky for a reason that has nothing to do
    with retrieval. `prompt_regression.py` compares runs of the same question.
    """
    archive = [
        ar.ArchiveSegment(
            segment=_segment("seg-a", "ספר על הצבא"),
            chunks=[_chunk("seg-a", 0, 0.0, 4.0, "הייתי עם אמנון בצבא")],
        )
    ]
    units = ar._build_units(archive)
    entity_map = {"אמנון": ["seg-a"]}
    lines = {
        ar._no_story_line("אמנון", entity_map, units, set(), "מה עוד עשיתם ביחד?")
        for _ in range(5)
    }
    assert len(lines) == 1


async def test_a_subject_named_alongside_a_real_answer_is_ignored(monkeypatch):
    """`about` accompanies an EMPTY selection and nothing else.

    Stored next to a real answer it would have no reader, and an unread field
    drifts until something starts trusting it.
    """
    archive = _resolvable_archive()
    monkeypatch.setattr(ar, "_load_archive", AsyncMock(return_value=archive))
    monkeypatch.setattr(ar, "_build_entity_map", AsyncMock(return_value={"Nir": ["seg-a"]}))
    monkeypatch.setattr(ar, "_build_name_tags_for", AsyncMock(return_value={}))
    monkeypatch.setattr(retrieval_service, "_recent_turns", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ar, "_read_archive_for_ranges",
        AsyncMock(return_value=ar.ArchiveRead(unit_ids=["u1"], about="Nir")),
    )
    ar.invalidate_archive_cache("group")

    selection = await ar.select_units("q", "group", "he", "sess")

    assert selection.clips, "this run has a real answer"
    assert selection.no_story_text is None


async def test_no_story_falls_back_to_the_generic_line_when_nobody_is_named(monkeypatch):
    """Questions about nobody in particular keep exactly today's behaviour.

    "What pets did you have?" has no subject. The generic line is the right
    answer for it, and this is the case a tailored one would make WORSE.
    """
    archive = _resolvable_archive()
    monkeypatch.setattr(ar, "_load_archive", AsyncMock(return_value=archive))
    monkeypatch.setattr(ar, "_build_entity_map", AsyncMock(return_value={"Nir": ["seg-a"]}))
    monkeypatch.setattr(ar, "_build_name_tags_for", AsyncMock(return_value={}))
    monkeypatch.setattr(retrieval_service, "_recent_turns", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ar, "_read_archive_for_ranges",
        AsyncMock(return_value=ar.ArchiveRead(unit_ids=[])),
    )
    ar.invalidate_archive_cache("group")

    result = await ar.assemble_video_clip_response_v2("q", "group", "he", "sess")

    assert result.no_story is True
    assert result.fallback_text == NO_STORY_FALLBACK


async def test_never_claims_nothing_more_while_units_are_unplayed():
    """THE REGRESSION THIS GUARDS. Said live, and it was false.

    Five of אמנון's twelve units had played; the model found nothing for
    "what else did you do together?" and the answer went out as
    "אין לי עוד סיפור על אמנון" — while u11-u13, the entire army story, sat
    unplayed. An empty selection means "nothing here answers THAT question",
    never "the archive is out of material about this person", and the
    difference is a fact we hold rather than a judgement the model makes.

    A specific falsehood is worse than a vague truth: it tells the listener to
    stop asking about someone the archive still has stories about.
    """
    archive = [
        ar.ArchiveSegment(
            segment=_segment("seg-a", "ספר על הצבא"),
            chunks=[_chunk_with("seg-a", 0, _paced_words(8, break_after=[3]), "text")],
        )
    ]
    units = ar._build_units(archive)
    assert len(units) > 1, "fixture must produce more than one unit"
    entity_map = {"אמנון": ["seg-a"]}

    # Some played, some not -> must NOT claim there is nothing more.
    partial = {ar._unit_key(units[0].segment_id, units[0].start_sec)}
    assert ar._no_story_line("אמנון", entity_map, units, partial, "q") is None

    # Everything played -> the tailored line is honest and is used.
    everything = {ar._unit_key(u.segment_id, u.start_sec) for u in units}
    line = ar._no_story_line("אמנון", entity_map, units, everything, "q")
    assert line and "אמנון" in line
    assert line in [t.format(entity="אמנון") for t in ra.NO_MORE_STORY_ABOUT_TEMPLATES]


async def test_a_follow_up_offer_survives_an_empty_answer(monkeypatch):
    """"Nothing for that, but want to hear about X?" is a different answer.

    This branch used to drop `follow_up` on the floor, so a turn that found no
    direct answer while KNOWING about related material still said only "I
    don't have a story about that" — which is how the system came to insist
    there was nothing more when there was.
    """
    monkeypatch.setattr(
        ar,
        "select_units",
        AsyncMock(
            return_value=ar.UnitSelection(
                clips=[],
                selected_units=[],
                follow_up={"question": "רוצה לשמוע על השירות הצבאי שלי?"},
            )
        ),
    )

    result = await ar.assemble_video_clip_response_v2("q", "group", "he", "sess")

    assert result.no_story is True
    assert result.follow_up == {"question": "רוצה לשמוע על השירות הצבאי שלי?"}


# ── An outage is not an answer ───────────────────────────────────────────────


async def test_a_failed_read_is_never_reported_as_an_empty_archive(monkeypatch):
    """THE DEFECT THIS CLOSES, and it produced a false statement about a life.

    The archive read is fail-soft on purpose — a family member should get a
    sentence, not a stack trace. But it returned the SAME empty result for
    "the model chose nothing" and "the API was down", so an outage came out as
    "אין לי סיפור על זה" about a person the archive has twelve units on.

    It also destroyed three measurements in one day, twice in evals and once
    in a live report that could only be explained by eliminating everything
    else. PROJECT_STATUS has warned about this shape since 2026-07-29.
    """
    archive = _resolvable_archive()
    monkeypatch.setattr(ar, "_load_archive", AsyncMock(return_value=archive))
    monkeypatch.setattr(ar, "_build_entity_map", AsyncMock(return_value={}))
    monkeypatch.setattr(ar, "_build_name_tags_for", AsyncMock(return_value={}))
    monkeypatch.setattr(retrieval_service, "_recent_turns", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ar.llm_service, "generate_response", AsyncMock(side_effect=RuntimeError("503"))
    )
    ar.invalidate_archive_cache("group")

    result = await ar.assemble_video_clip_response_v2("q", "group", "he", "sess")

    assert result.read_failed is True
    assert result.fallback_text == TRANSIENT_FAILURE_FALLBACK
    assert result.fallback_text != NO_STORY_FALLBACK
    assert result.video_url is None


async def test_a_failed_read_never_names_a_subject_or_offers_anything(monkeypatch):
    """Nothing below the read can say anything true once it did not happen.

    A failed read must not reach the subject-naming line, the follow-up
    carry-through, or the clarification branch — every one of them asserts
    something about an archive that was never consulted.
    """
    monkeypatch.setattr(
        ar,
        "_read_archive_for_ranges",
        AsyncMock(return_value=ar.ArchiveRead(unit_ids=[], failed=True)),
    )
    archive = _resolvable_archive()
    monkeypatch.setattr(ar, "_load_archive", AsyncMock(return_value=archive))
    monkeypatch.setattr(ar, "_build_entity_map", AsyncMock(return_value={"Nir": ["seg-a"]}))
    monkeypatch.setattr(ar, "_build_name_tags_for", AsyncMock(return_value={}))
    monkeypatch.setattr(retrieval_service, "_recent_turns", AsyncMock(return_value=[]))
    ar.invalidate_archive_cache("group")

    selection = await ar.select_units("q", "group", "he", "sess")

    assert selection.read_failed is True
    assert selection.no_story_text is None
    assert selection.follow_up is None
    assert selection.clarify is None


# ── _validate_follow_up exposes the validated unit_ids (2026-08-21) ─────────
# Server-side only: the WS layer strips them (tested in test_websocket.py).


def _fu_unit(uid, seg, index, start):
    return ar.UtteranceUnit(
        unit_id=uid, segment_id=seg, index=index,
        start_sec=start, end_sec=start + 1.0, text=f"טקסט {uid}",
    )


def test_validate_follow_up_exposes_surviving_unit_ids():
    u1, u2, u3 = _fu_unit("u1", "s", 1, 0.0), _fu_unit("u2", "s", 2, 2.0), _fu_unit("u3", "s", 3, 4.0)
    by_id = {"u1": u1, "u2": u2, "u3": u3}
    out = ar._validate_follow_up(
        {"question": "עוד?", "unit_ids": ["u2", "u3"]},
        by_id, answer_units=[u1], shown_keys=set(),
    )
    assert out == {"question": "עוד?", "unit_ids": ["u2", "u3"]}


def test_validate_follow_up_unit_ids_exclude_answer_and_shown():
    u1, u2, u3 = _fu_unit("u1", "s", 1, 0.0), _fu_unit("u2", "s", 2, 2.0), _fu_unit("u3", "s", 3, 4.0)
    by_id = {"u1": u1, "u2": u2, "u3": u3}
    shown = {ar._unit_key("s", 2.0)}  # u2 already played
    out = ar._validate_follow_up(
        {"question": "עוד?", "unit_ids": ["u1", "u2", "u3"]},
        by_id, answer_units=[u1], shown_keys=shown,
    )
    # u1 is the answer, u2 is shown — only u3 survives, and the exposed
    # ids say exactly that.
    assert out == {"question": "עוד?", "unit_ids": ["u3"]}


def test_validate_follow_up_all_covered_still_drops_entirely():
    u1 = _fu_unit("u1", "s", 1, 0.0)
    out = ar._validate_follow_up(
        {"question": "עוד?", "unit_ids": ["u1"]},
        {"u1": u1}, answer_units=[u1], shown_keys=set(),
    )
    assert out is None


# ── the scoped unit-id scheme (UNIT_ID_STABILITY_PLAN, 2026-08-22) ──────────
# Under the default global scheme every rendered byte is unchanged (the
# byte-identity control above now formats via _id_atoms()); these tests pin
# the scoped side of the toggle.

from types import SimpleNamespace


def _seg_item(rec_no):
    return SimpleNamespace(segment=SimpleNamespace(id="seg-x", recording_no=rec_no))


def test_unit_id_scheme_global_is_default_and_unchanged():
    from app.config import settings

    assert settings.UNIT_ID_SCHEME == "global"
    assert ar._unit_id_for(_seg_item(7), global_index=42, local_index=3) == "u42"


def test_unit_id_scheme_scoped_anchors_to_recording_no(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "UNIT_ID_SCHEME", "scoped")
    assert ar._unit_id_for(_seg_item(7), global_index=42, local_index=3) == "r7u3"


def test_unit_id_scheme_scoped_refuses_missing_recording_no(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "UNIT_ID_SCHEME", "scoped")
    with pytest.raises(RuntimeError, match="recording_no"):
        ar._unit_id_for(_seg_item(None), global_index=1, local_index=1)


def test_scoped_parse_counts_and_drops_malformed_ids(monkeypatch):
    """The §4 copy-reliability instrument: bare numbers and bare u-ids are
    AMBIGUOUS under scoped ids — counted, logged, dropped, never guessed."""
    from app.config import settings

    monkeypatch.setattr(settings, "UNIT_ID_SCHEME", "scoped")
    ids, malformed = ar._parse_unit_selection(
        '{"unit_ids": ["r2u3", "u7", 9, "x1", "r12u40"]}'
    )
    assert ids == ["r2u3", "r12u40"]
    assert malformed == 3


def test_scoped_ordinals_print_recording_no(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "UNIT_ID_SCHEME", "scoped")
    seg_a = SimpleNamespace(segment=SimpleNamespace(id="sa", recording_no=4))
    seg_b = SimpleNamespace(segment=SimpleNamespace(id="sb", recording_no=17))
    units = [
        ar.UtteranceUnit(unit_id="r4u1", segment_id="sa", index=1,
                         start_sec=0.0, end_sec=1.0, text="a"),
        ar.UtteranceUnit(unit_id="r17u1", segment_id="sb", index=2,
                         start_sec=0.0, end_sec=1.0, text="b"),
    ]
    # Gaps allowed: deletion leaves the numbering honest.
    assert ar._recording_ordinals([seg_a, seg_b], units) == {"sa": 4, "sb": 17}


def test_scoped_prompt_atoms_change_only_the_id_form(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "UNIT_ID_SCHEME", "scoped")
    atoms = ar._id_atoms()
    assert atoms["id_ex1"] == "r2u3"
    assert "r<recording>u<number>" in atoms["id_form"]


def test_history_block_resolves_ids_from_stable_keys():
    """Persisted unit_ids go stale on any renumbering; the renderer resolves
    the CURRENT id from the stable segment:start key, falling back to the
    stored id only when the key is unknown (pre-key rows)."""
    turns = [
        {"role": "assistant", "content": "http://x/clip.mp4"},
        {"role": "user", "content": "ומה עוד?"},
    ]
    per_turn = [[
        {"key": "seg-a:1.00", "unit_id": "u84", "text": "אנחנו חמישה משפחה"},
        {"key": "unknown:9.99", "unit_id": "u85", "text": "אני טל"},
    ]]
    out = ar._format_history_block(turns, per_turn, {"seg-a:1.00": "r11u1"})
    assert 'r11u1: "אנחנו חמישה משפחה"' in out  # resolved from the key
    assert 'u85: "אני טל"' in out  # unknown key -> stored id fallback


# ── shown-state placement (GEMINI_CONTEXT_CACHING_PLAN Phase A) ─────────────
# The system prompt template is deliberately untouched by the toggle: under
# `message` only the transcript loses its per-turn marks (becoming the
# stable cacheable prefix) and the shown facts move to the user message.
# These tests pin the blast-radius boundary: with nothing shown, BOTH modes
# render byte-for-byte what production has always sent.


def _shown_unit(uid, seg, idx, start):
    return ar.UtteranceUnit(
        unit_id=uid, segment_id=seg, index=idx,
        start_sec=start, end_sec=start + 1.0, text="t",
    )


def test_shown_state_placement_default_is_inline():
    from app.config import settings

    assert settings.SHOWN_STATE_PLACEMENT == "inline"


def test_user_message_bytes_unchanged_when_nothing_shown():
    # Exactly the string the pre-Phase-A inline code assembled.
    assert ar._build_user_message("שאלה", "") == "Question:\nשאלה"
    assert (
        ar._build_user_message("שאלה", "TURNS")
        == "Recent conversation:\nTURNS\n\nQuestion:\nשאלה"
    )
    # Empty shown_block explicitly - the empty-shown identity invariant.
    assert ar._build_user_message("שאלה", "TURNS", "") == (
        "Recent conversation:\nTURNS\n\nQuestion:\nשאלה"
    )


def test_user_message_places_shown_block_between_history_and_question():
    out = ar._build_user_message("שאלה", "TURNS", "ALREADY SHOWN: u5")
    assert out == (
        "Recent conversation:\nTURNS\n\nALREADY SHOWN: u5\n\nQuestion:\nשאלה"
    )


def test_shown_block_empty_state_renders_nothing():
    units = [_shown_unit("u1", "sa", 1, 0.0)]
    assert ar._format_shown_block(units, set()) == ""
    # Keys that resolve to no current unit (deleted recording) also render
    # nothing rather than inventing ids.
    assert ar._format_shown_block(units, {"gone:9.99"}) == ""


def test_shown_block_resolves_current_ids_in_archive_order():
    units = [
        _shown_unit("u1", "sa", 1, 0.0),
        _shown_unit("u2", "sa", 2, 5.0),
        _shown_unit("u3", "sb", 3, 0.0),
    ]
    keys = {ar._unit_key("sb", 0.0), ar._unit_key("sa", 0.0)}
    out = ar._format_shown_block(units, keys)
    assert out == (
        "ALREADY SHOWN: u1, u3 (these units were played earlier in this "
        "conversation — treat them exactly as if marked [ALREADY SHOWN] "
        "in the transcript)"
    )


def test_message_mode_transcript_carries_no_marks(monkeypatch):
    """Under `message` the transcript builder receives an EMPTY shown set at
    the call site; a mark-free transcript is what makes the prefix stable.
    Pinned at the formatter level: no shown keys, no marks, ever."""
    archive = [
        ar.ArchiveSegment(
            segment=_segment("sa", "ש"),
            chunks=[_chunk_with("sa", 0, _paced_words(11, break_after=[5]), "t")],
        ),
    ]
    units = ar._build_units(archive)
    shown = {ar._unit_key(units[0].segment_id, units[0].start_sec)}
    # Inline path: the mark appears. Message path passes set() instead —
    # and with an empty shown set no mark can ever render.
    assert "[ALREADY SHOWN]" in ar._format_annotated_transcript(
        archive, units, shown, {}
    )
    assert "[ALREADY SHOWN]" not in ar._format_annotated_transcript(
        archive, units, set(), {}
    )
