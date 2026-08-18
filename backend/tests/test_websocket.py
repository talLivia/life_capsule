"""
Tests for the real-time WebSocket pipeline's concurrency contract.

These exercise `ConnectionManager` directly with a fake socket so we don't
need a live DB, LLM, or GPU. The properties under test are the ones that
make barge-in actually work:

  * `handle_text_input` dispatches without blocking (returns before the
    turn finishes) — otherwise the WS receive loop can never observe an
    interrupt.
  * A second input cancels the first in-flight turn.
  * An explicit interrupt cancels the turn and notifies the client.
  * Input validation rejects empty / oversized messages up front.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.websocket import (
    _CLAUSE_RE,
    _MAX_CHUNK_CHARS,
    _MIN_FIRST_CHUNK_LEN,
    _MIN_SENTENCE_LEN,
    _SENTENCE_RE,
    MAX_TEXT_INPUT_LEN,
    ConnectionManager,
    _drain_chunks,
    _match_pending_prompt,
)

# ── chunker (first-frame latency) ───────────────────────────────────────────


def test_drain_chunks_emits_at_clause_for_first_fragment():
    """A clause boundary ships the opening fragment before the sentence ends."""
    buf = "Sure thing, let me look that up for you right now."
    chunks, rest = _drain_chunks(buf, _CLAUSE_RE, _MIN_FIRST_CHUNK_LEN, _MAX_CHUNK_CHARS)
    # "Sure thing," is >= 12 chars → emitted at the comma, not the period.
    assert chunks
    assert chunks[0].startswith("Sure thing,")


def test_drain_chunks_never_drops_text():
    """Short leading fragments merge forward rather than being discarded."""
    buf = "Hi, the answer is 42 and that is final."
    chunks, rest = _drain_chunks(buf, _CLAUSE_RE, _MIN_FIRST_CHUNK_LEN, _MAX_CHUNK_CHARS)
    reassembled = " ".join(chunks)
    if rest.strip():
        reassembled = (reassembled + " " + rest).strip()
    for word in ["Hi,", "answer", "42", "final."]:
        assert word in reassembled


def test_drain_chunks_force_flush_runon():
    """A long run-on with no punctuation is cut at a space, not held forever."""
    buf = "word " * 60  # 300 chars, no sentence punctuation
    chunks, rest = _drain_chunks(buf, _SENTENCE_RE, _MIN_SENTENCE_LEN, _MAX_CHUNK_CHARS)
    assert chunks  # something was force-flushed
    assert all(len(c) <= _MAX_CHUNK_CHARS for c in chunks)


def test_drain_chunks_holds_incomplete_buffer():
    """With no boundary and under the cap, nothing is emitted yet."""
    chunks, rest = _drain_chunks(
        "partial thought with no end", _SENTENCE_RE, _MIN_SENTENCE_LEN, _MAX_CHUNK_CHARS
    )
    assert chunks == []
    assert rest == "partial thought with no end"


class FakeWebSocket:
    """Minimal stand-in that records everything sent to the client."""

    def __init__(self):
        self.sent: list[dict] = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        self.sent.append(message)


def _wire_session(manager: ConnectionManager, session_id: str = "s1", user_id: str = "u1"):
    """Attach a fake connected session without going through the DB-backed connect()."""
    ws = FakeWebSocket()
    manager.active_connections[session_id] = ws
    manager._send_locks[session_id] = asyncio.Lock()
    manager.session_data[session_id] = {
        "messages": [],
        "user_id": user_id,
        "language": "en",
        "system_prompt": None,
        "voice_wav": None,
        "avatar_image_local": None,
    }
    return ws


@pytest.mark.asyncio
async def test_handle_text_input_is_non_blocking():
    """Dispatch must return immediately, before the turn completes."""
    m = ConnectionManager()
    _wire_session(m)

    started = asyncio.Event()

    async def slow_turn(session_id, text):
        started.set()
        await asyncio.sleep(5)  # simulate a long LLM+TTS+animation turn

    m._handle_text_input_inner = slow_turn  # type: ignore[assignment]

    # If dispatch blocked on the turn, this would take ~5s and time out.
    await asyncio.wait_for(m.handle_text_input("s1", "hello there"), timeout=0.5)

    assert "s1" in m._active_turns
    await asyncio.wait_for(started.wait(), timeout=1)  # the turn really started

    # cleanup
    await m.interrupt_active_turn("s1")


@pytest.mark.asyncio
async def test_second_input_interrupts_first():
    """A new turn cancels the previous in-flight turn (barge-in)."""
    m = ConnectionManager()
    _wire_session(m)

    async def slow_turn(session_id, text):
        await asyncio.sleep(5)

    m._handle_text_input_inner = slow_turn  # type: ignore[assignment]

    await m.handle_text_input("s1", "first message")
    first_task = m._active_turns["s1"]

    await m.handle_text_input("s1", "second message")
    await asyncio.sleep(0.05)  # let the cancellation settle

    assert first_task.cancelled() or first_task.done()
    # The new turn is now the active one.
    assert m._active_turns["s1"] is not first_task

    await m.interrupt_active_turn("s1")


@pytest.mark.asyncio
async def test_explicit_interrupt_notifies_client():
    """interrupt_active_turn cancels and emits an `interrupted` event."""
    m = ConnectionManager()
    ws = _wire_session(m)

    async def slow_turn(session_id, text):
        await asyncio.sleep(5)

    m._handle_text_input_inner = slow_turn  # type: ignore[assignment]
    await m.handle_text_input("s1", "talk to me")

    interrupted = await m.interrupt_active_turn("s1")
    assert interrupted is True
    assert "s1" not in m._active_turns
    assert any(msg["type"] == "interrupted" for msg in ws.sent)


@pytest.mark.asyncio
async def test_interrupt_with_no_active_turn_is_noop():
    m = ConnectionManager()
    _wire_session(m)
    assert await m.interrupt_active_turn("s1") is False


@pytest.mark.asyncio
async def test_empty_text_rejected():
    m = ConnectionManager()
    ws = _wire_session(m)
    await m.handle_text_input("s1", "   ")
    assert any(msg["type"] == "error" for msg in ws.sent)
    assert "s1" not in m._active_turns  # no turn spawned


@pytest.mark.asyncio
async def test_oversized_text_rejected():
    m = ConnectionManager()
    ws = _wire_session(m)
    await m.handle_text_input("s1", "x" * (MAX_TEXT_INPUT_LEN + 1))
    assert any(
        "too long" in msg.get("message", "").lower() for msg in ws.sent if msg["type"] == "error"
    )
    assert "s1" not in m._active_turns


@pytest.mark.asyncio
async def test_set_language_coerces_unknown():
    m = ConnectionManager()
    _wire_session(m)
    await m.set_language("s1", "klingon")
    assert m.session_data["s1"]["language"] == "en"
    await m.set_language("s1", "fr")
    assert m.session_data["s1"]["language"] == "fr"


@pytest.mark.asyncio
async def test_set_voice_rejects_cross_tenant(monkeypatch):
    """A voice owned by another user must not attach to this session."""
    m = ConnectionManager()
    _wire_session(m, user_id="owner-A")

    async def fake_entry(voice_id):
        return {"id": voice_id, "user_id": "owner-B", "wav_path": "/tmp/x.wav"}

    monkeypatch.setattr(m, "_get_voice_entry", fake_entry)
    ok = await m.set_voice_by_id("s1", "some-voice")
    assert ok is False
    assert m.session_data["s1"]["voice_wav"] is None


@pytest.mark.asyncio
async def test_audio_transcribed_in_session_language(monkeypatch):
    """STT must use the session's selected language, not always English."""
    import base64

    from app.services import stt as stt_module

    m = ConnectionManager()
    _wire_session(m)
    m.session_data["s1"]["language"] = "fr"

    captured = {}

    async def fake_transcribe(audio, language="en"):
        captured["language"] = language
        return ""  # empty → handler returns before spawning a turn

    # _handle_audio_inner calls gpu_client.transcribe(), which (with
    # GPU_SERVICE_URL unset, the default) calls this exact shared
    # stt_service singleton — patching it here affects that call too.
    monkeypatch.setattr(stt_module.stt_service, "transcribe", fake_transcribe)

    await m._handle_audio_inner("s1", base64.b64encode(b"x" * 2000).decode())
    assert captured.get("language") == "fr"


@pytest.mark.asyncio
async def test_audio_routes_to_avatar_pipeline_by_default(monkeypatch):
    """Prompt 14's audio follow-up: with no producer_chat_mode set (or set
    to the "avatar" default), transcribed speech must still reach the
    existing avatar turn — unchanged behavior for every session that
    hasn't opted into video-clip mode."""
    import base64

    from app.services import stt as stt_module

    m = ConnectionManager()
    _wire_session(m)

    async def fake_transcribe(audio, language="en"):
        return "hello there"

    monkeypatch.setattr(stt_module.stt_service, "transcribe", fake_transcribe)

    calls = {"text": 0, "video_clip": 0}

    async def fake_text_turn(session_id, text):
        calls["text"] += 1

    async def fake_video_clip_turn(session_id, text):
        calls["video_clip"] += 1

    m._handle_text_input_inner = fake_text_turn  # type: ignore[assignment]
    m._handle_video_clip_question_inner = fake_video_clip_turn  # type: ignore[assignment]

    await m._handle_audio_inner("s1", base64.b64encode(b"x" * 2000).decode())

    assert calls == {"text": 1, "video_clip": 0}


@pytest.mark.asyncio
async def test_audio_routes_unknown_modes_to_the_avatar_path(monkeypatch):
    """Only "video_clips_v2" reaches the clip turn since the v1 mode was
    removed. A mode string the router doesn't know — including the retired
    "video_clips", which validation no longer accepts — fails toward the
    avatar text path, exactly as any unknown value always has."""
    import base64

    from app.services import stt as stt_module

    m = ConnectionManager()
    _wire_session(m)
    m.session_data["s1"]["producer_chat_mode"] = "video_clips"  # retired value

    async def fake_transcribe(audio, language="en"):
        return "tell me about your brothers"

    monkeypatch.setattr(stt_module.stt_service, "transcribe", fake_transcribe)

    calls = {"text": 0, "video_clip": 0}

    async def fake_text_turn(session_id, text):
        calls["text"] += 1

    async def fake_video_clip_turn(session_id, text):
        calls["video_clip"] += 1

    m._handle_text_input_inner = fake_text_turn  # type: ignore[assignment]
    m._handle_video_clip_question_inner = fake_video_clip_turn  # type: ignore[assignment]

    await m._handle_audio_inner("s1", base64.b64encode(b"x" * 2000).decode())

    assert calls == {"text": 1, "video_clip": 0}


@pytest.mark.asyncio
async def test_audio_routes_to_video_clip_pipeline_for_v2_mode(monkeypatch):
    """Prompt 15: "video_clips_v2" is a video-clip mode too — transcribed
    audio must feed the video-clip turn (which then picks the v2 assembler
    internally), NOT the avatar turn."""
    import base64

    from app.services import stt as stt_module

    m = ConnectionManager()
    _wire_session(m)
    m.session_data["s1"]["producer_chat_mode"] = "video_clips_v2"

    async def fake_transcribe(audio, language="en"):
        return "who is Ilana"

    monkeypatch.setattr(stt_module.stt_service, "transcribe", fake_transcribe)

    calls = {"text": 0, "video_clip": 0}

    async def fake_text_turn(session_id, text):
        calls["text"] += 1

    async def fake_video_clip_turn(session_id, text):
        calls["video_clip"] += 1

    m._handle_text_input_inner = fake_text_turn  # type: ignore[assignment]
    m._handle_video_clip_question_inner = fake_video_clip_turn  # type: ignore[assignment]

    await m._handle_audio_inner("s1", base64.b64encode(b"x" * 2000).decode())

    assert calls == {"text": 0, "video_clip": 1}


@pytest.mark.asyncio
async def test_video_clip_inner_always_dispatches_to_the_v2_assembler(monkeypatch):
    """Since the v1 (video_clips) mode was removed there is ONE clip
    assembler: full_archive_retrieval's v2. The handler must use it
    regardless of what producer_chat_mode says — reaching this handler at
    all means the producer is on a clip mode, and only one exists."""
    from app.services import full_archive_retrieval
    from app.services.video_clip_assembler import VideoClipResult

    called = {"v2": 0}

    async def fake_v2(question, group_id, recording_language, session_id):
        called["v2"] += 1
        return VideoClipResult(video_url="http://x/v2.mp4")

    monkeypatch.setattr(full_archive_retrieval, "assemble_video_clip_response_v2", fake_v2)

    m = ConnectionManager()
    ws = _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"
    m.session_data["s1"]["producer_chat_mode"] = "video_clips_v2"

    await m._handle_video_clip_question_inner("s1", "who is Ilana")

    assert called == {"v2": 1}
    urls = [msg.get("video_url") for msg in ws.sent if msg["type"] == "video_clip_response"]
    assert urls == ["http://x/v2.mp4"]


@pytest.mark.asyncio
async def test_handle_video_clip_question_inner_sends_error_on_early_failure(monkeypatch):
    """Confirmed live bug: _persist_message/_ensure_conversation_title/the
    initial "status" send used to sit OUTSIDE the try/except — an
    unexpected failure there would propagate out of the task uncaught,
    with no "error" message ever sent, leaving the frontend stuck on
    "Finding a clip…" forever with nothing in the browser console. The
    try must wrap the ENTIRE chain, not just assemble_video_clip_response."""
    m = ConnectionManager()
    ws = _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"

    async def fake_persist_message(session_id, role, content, latency=None):
        raise RuntimeError("DB connection lost")

    m._persist_message = fake_persist_message  # type: ignore[assignment]

    await m._handle_video_clip_question_inner("s1", "tell me about your family")

    types = [msg["type"] for msg in ws.sent]
    assert "error" in types, f"expected an error message, got {types}"


@pytest.mark.asyncio
async def test_animate_from_queue_tolerates_locked_temp_file_cleanup(monkeypatch):
    """A PermissionError deleting tmp_audio/tmp_video (Windows can briefly
    hold a file lock after the ffmpeg subprocess behind synthesize/animate
    exits) must not crash the whole turn. That unlink call lives in
    `finally`, which propagates an exception regardless of the `except`
    clause right above it unless handled explicitly there too — a real
    incident where this bubbled all the way up to "Processing failed",
    even though the chunk itself had already succeeded."""
    import pathlib

    from app import websocket as wsmod
    from app.services.tts import SynthResult

    m = ConnectionManager()
    ws = _wire_session(m)
    m.session_data["s1"]["avatar_image_local"] = "/fake/avatar.jpg"

    async def fake_synthesize(text, output_path, speaker_wav=None, language="en"):
        pathlib.Path(output_path).write_bytes(b"wav")
        return SynthResult(
            output_path=output_path, engine="chatterbox", fallback=False, voice_cloned=False
        )

    async def fake_animate(avatar_image_path, audio_path, output_path):
        pathlib.Path(output_path).write_bytes(b"mp4")

    async def fake_upload(data, key, content_type="video/mp4", metadata=None):
        return f"http://test/{key}"

    async def fake_serving_url(key, ttl_seconds=3600):
        return f"http://test/{key}"

    monkeypatch.setattr(wsmod.gpu_client, "synthesize", fake_synthesize)
    monkeypatch.setattr(wsmod.gpu_client, "animate", fake_animate)
    monkeypatch.setattr(wsmod.storage_service, "upload_file", fake_upload)
    monkeypatch.setattr(wsmod.storage_service, "serving_url", fake_serving_url)

    real_unlink = pathlib.Path.unlink

    def flaky_unlink(self, missing_ok=False):
        if self.name.endswith("_audio.wav") or self.name.endswith("_video.mp4"):
            raise PermissionError(f"[WinError 32] simulated lock on {self}")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", flaky_unlink)

    queue: "asyncio.Queue" = asyncio.Queue()
    await queue.put("Hello there.")
    await queue.put(None)

    await m._animate_from_queue("s1", queue)

    types = [msg["type"] for msg in ws.sent]
    assert "video_chunk" in types  # the chunk itself still succeeded
    assert "video_chunk_end" in types
    assert not any(
        msg["type"] == "error" and "failed for all sentences" in msg.get("message", "")
        for msg in ws.sent
    )


@pytest.mark.asyncio
async def test_load_session_data_defaults_language_to_producer_recording_language(
    db_session, test_engine, monkeypatch
):
    """session_data["language"] (used by BOTH STT transcription and TTS
    synthesis) defaulted to "en" in connect() and was never updated
    afterward unless the client sent an explicit set_language message.
    Confirmed live: for a family member talking to a Hebrew-recorded
    archive, this meant Edge TTS tried to speak verbatim Hebrew transcript
    text with an English voice — which can't vocalize Hebrew at all, only
    a literal embedded digit came through audible. The default must match
    the STORYTELLER's own recording language instead."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import app.database as database_module
    from app import websocket as wsmod
    from app.models import Avatar, User
    from app.models import Session as SessionModel

    producer = User(
        email="producer@example.com",
        username="producer",
        hashed_password="x",
        recording_language="he",
    )
    db_session.add(producer)
    await db_session.flush()
    avatar = Avatar(
        user_id=producer.id,
        name="A",
        image_url="http://x/i.jpg",
        s3_key="avatars/a/image.jpg",
        status="ready",
    )
    db_session.add(avatar)
    await db_session.flush()
    session = SessionModel(
        id="sess-lang",
        user_id=producer.id,
        producer_id=producer.id,
        avatar_id=avatar.id,
        status="active",
    )
    db_session.add(session)
    await db_session.commit()

    # _load_session_data opens its own AsyncSessionLocal (bypasses FastAPI's
    # DI) — retarget it at this test's engine, matching the pattern used
    # elsewhere for modules that do the same.
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", factory)

    manager = wsmod.ConnectionManager()

    async def fake_resolve_image(avatar):
        return "/fake/avatar.jpg"

    monkeypatch.setattr(manager, "_resolve_local_image", fake_resolve_image)
    manager.session_data["sess-lang"] = {
        "messages": [],
        "avatar_id": None,
        "avatar_image_key": None,
        "avatar_image_local": None,
        "voice_wav": None,
        "language": "en",
        "user_id": producer.id,
        "producer_id": None,
        "producer_recording_language": "en",
        "connected_at": None,
        "last_activity": None,
    }

    await manager._load_session_data("sess-lang")

    assert manager.session_data["sess-lang"]["producer_recording_language"] == "he"
    assert manager.session_data["sess-lang"]["language"] == "he"


@pytest.mark.asyncio
async def test_load_session_data_resolves_producer_without_any_avatar(
    db_session, test_engine, monkeypatch
):
    """A v2 session carries no avatar at all (avatar_id NULL), and the
    producer must still resolve — from the session's own producer_id, not
    through an avatar row (docs/V2_PRIMARY_AVATAR_DORMANT_PLAN.md §3.3).
    Before that change this exact state left producer_id None, and every
    clip question answered the no-pipeline fallback."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import app.database as database_module
    from app import websocket as wsmod
    from app.models import User
    from app.models import Session as SessionModel

    producer = User(
        email="clipsonly@example.com",
        username="clipsonly",
        hashed_password="x",
        recording_language="he",
        chat_mode="video_clips_v2",
    )
    db_session.add(producer)
    await db_session.flush()
    session = SessionModel(
        id="sess-no-avatar",
        user_id=producer.id,
        producer_id=producer.id,
        avatar_id=None,
        status="active",
    )
    db_session.add(session)
    await db_session.commit()

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", factory)

    manager = wsmod.ConnectionManager()

    async def fail_resolve_image(avatar):  # pragma: no cover - must not run
        raise AssertionError("no avatar exists; nothing should resolve an image")

    monkeypatch.setattr(manager, "_resolve_local_image", fail_resolve_image)
    manager.session_data["sess-no-avatar"] = {
        "messages": [],
        "avatar_id": None,
        "avatar_image_key": None,
        "avatar_image_local": None,
        "voice_wav": None,
        "language": "en",
        "user_id": producer.id,
        "producer_id": None,
        "producer_recording_language": "en",
        "producer_chat_mode": "avatar",
        "connected_at": None,
        "last_activity": None,
    }

    await manager._load_session_data("sess-no-avatar")

    data = manager.session_data["sess-no-avatar"]
    assert data["producer_id"] == producer.id
    assert data["producer_chat_mode"] == "video_clips_v2"
    assert data["producer_recording_language"] == "he"
    assert data["avatar_image_local"] is None


@pytest.mark.asyncio
async def test_an_avatar_turn_runs_the_shared_engine_and_persists_shown_units(monkeypatch):
    """AVATAR_SHARED_ENGINE_PLAN step 3: the avatar text path runs
    select_units + the spoken renderer, speaks the verbatim unit text, and
    persists the SAME shown_units metadata record the v2 handler writes —
    the record the next turn's history block and already-shown marks read."""
    from app.services import full_archive_retrieval as far
    from app.services import spoken_answer as sa_mod

    unit = far.UtteranceUnit(
        unit_id="u1", segment_id="seg-a", index=0,
        start_sec=0.0, end_sec=2.0, text="נולדתי בטבריה",
    )
    selection = far.UnitSelection(clips=[], selected_units=[unit])

    async def fake_select(question, group_id, recording_language, session_id):
        return selection

    async def no_names(segment_ids, group_id):
        return {}

    monkeypatch.setattr(far, "select_units", fake_select)
    monkeypatch.setattr(sa_mod, "_entity_names_by_segment", no_names)

    m = ConnectionManager()
    ws = _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"

    persisted = []

    async def record_persist(session_id, role, content, latency=None, metadata=None, video_url=None):
        persisted.append({"role": role, "content": content, "metadata": metadata})

    monkeypatch.setattr(m, "_persist_message", record_persist)

    await m._handle_text_input_inner("s1", "איפה נולדת?")

    assistant = [p for p in persisted if p["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["content"] == "נולדתי בטבריה"
    assert assistant[0]["metadata"] == {
        "shown_units": [{"key": "seg-a:0.00", "unit_id": "u1", "text": "נולדתי בטבריה"}]
    }


@pytest.mark.asyncio
async def test_an_avatar_clarify_speaks_the_fixed_line_and_sends_options(monkeypatch):
    """Which-אמנון in avatar mode: a fixed spoken line (generated prose
    never reaches TTS) plus a `clarify` chat event carrying the options and
    the original question, so the UI can re-ask on a click."""
    from app.services import full_archive_retrieval as far
    from app.services import spoken_answer as sa_mod

    clarify = {"question": "לאיזה אמנון התכוונת?", "options": ["אמנון", "אמנון נחום"]}
    selection = far.UnitSelection(clips=[], selected_units=[], clarify=clarify)

    async def fake_select(question, group_id, recording_language, session_id):
        return selection

    monkeypatch.setattr(far, "select_units", fake_select)

    m = ConnectionManager()
    ws = _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"

    async def swallow_persist(*a, **k):
        return None

    monkeypatch.setattr(m, "_persist_message", swallow_persist)

    await m._handle_text_input_inner("s1", "ספר לי על אמנון")

    spoken = [msg for msg in ws.sent if msg.get("type") == "message"]
    assert spoken and spoken[-1]["content"] == sa_mod.CLARIFY_SPOKEN_LINE

    clarify_events = [msg for msg in ws.sent if msg.get("type") == "clarify"]
    assert clarify_events == [{
        "type": "clarify",
        "question": "לאיזה אמנון התכוונת?",
        "options": ["אמנון", "אמנון נחום"],
        "for_question": "ספר לי על אמנון",
    }]


@pytest.mark.asyncio
async def test_an_engine_outage_speaks_the_transient_line_not_no_story(monkeypatch):
    """The documented outage-as-no-story conflation, now fixed in avatar
    mode too: a failed engine call must never claim the archive is empty."""
    from app.services import full_archive_retrieval as far
    from app.services.response_assembler import TRANSIENT_FAILURE_FALLBACK

    async def boom(question, group_id, recording_language, session_id):
        raise RuntimeError("engine down")

    monkeypatch.setattr(far, "select_units", boom)

    m = ConnectionManager()
    ws = _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"

    async def swallow_persist(*a, **k):
        return None

    monkeypatch.setattr(m, "_persist_message", swallow_persist)

    await m._handle_text_input_inner("s1", "מי האחים שלך?")

    spoken = [msg for msg in ws.sent if msg.get("type") == "message"]
    assert spoken and spoken[-1]["content"] == TRANSIENT_FAILURE_FALLBACK


@pytest.mark.asyncio
async def test_a_no_story_reply_is_persisted_like_any_other_turn(monkeypatch):
    """It was not, and the gap silently broke follow-up resolution.

    `_recent_turns` takes the last 2 MESSAGE ROWS and runs AFTER the question
    is stored, so one missing assistant reply leaves the window holding two
    user questions and no antecedent at all. Two vague follow-ups in a row
    ("and what else?") and there is no name anywhere in view — which is the
    exact conversation shape the subject-naming feature exists for.
    """
    from app.services import full_archive_retrieval
    from app.services.video_clip_assembler import VideoClipResult

    async def fake_v2(question, group_id, recording_language, session_id):
        return VideoClipResult(
            video_url=None, no_story=True, fallback_text="אין לי סיפור על זה"
        )

    monkeypatch.setattr(
        full_archive_retrieval, "assemble_video_clip_response_v2", fake_v2
    )

    m = ConnectionManager()
    ws = _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"
    m.session_data["s1"]["producer_chat_mode"] = "video_clips_v2"

    persisted: list = []

    async def record(session_id, role, content, **kw):
        persisted.append((role, content))

    m._persist_message = record  # type: ignore[assignment]

    await m._handle_video_clip_question_inner("s1", "מה עוד?")

    assert ("assistant", "אין לי סיפור על זה") in persisted
    assert [msg["type"] for msg in ws.sent if msg["type"].startswith("video_clip")] == [
        "video_clip_no_story"
    ]


@pytest.mark.asyncio
async def test_a_failed_read_is_sent_as_its_own_message_type(monkeypatch):
    """Never `video_clip_no_story`, which asserts the archive has nothing.

    An outage cannot support that claim, and sending it under the same type
    would let the client render the two identically — which is how a 503 came
    to tell a listener their relative had no story about someone.
    """
    from app.services import full_archive_retrieval
    from app.services.response_assembler import TRANSIENT_FAILURE_FALLBACK
    from app.services.video_clip_assembler import VideoClipResult

    async def fake_v2(question, group_id, recording_language, session_id):
        return VideoClipResult(
            video_url=None,
            no_story=True,
            read_failed=True,
            fallback_text=TRANSIENT_FAILURE_FALLBACK,
        )

    monkeypatch.setattr(
        full_archive_retrieval, "assemble_video_clip_response_v2", fake_v2
    )

    m = ConnectionManager()
    ws = _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"
    m.session_data["s1"]["producer_chat_mode"] = "video_clips_v2"

    await m._handle_video_clip_question_inner("s1", "מה עוד?")

    failed = [msg for msg in ws.sent if msg["type"] == "video_clip_failed"]
    assert len(failed) == 1
    assert failed[0]["message"] == TRANSIENT_FAILURE_FALLBACK
    # Carries the question so the client can retry it verbatim.
    assert failed[0]["question"] == "מה עוד?"
    assert not [msg for msg in ws.sent if msg["type"] == "video_clip_no_story"]


# ── pending-prompt voice answering (AVATAR_SHARED_ENGINE_PLAN §7) ───────────
#
# A spoken "כן" must produce the byte-identical outgoing question a button
# click sends, via deterministic string matching only — and anything that
# doesn't match must fall through as a fresh question, clearing the prompt.


_FOLLOW_UP_PENDING = {"kind": "follow_up", "question": "רוצה לשמוע על הצבא?"}
_CLARIFY_PENDING = {
    "kind": "clarify",
    "options": ["אמנון", "אמנון נחום"],
    "original": "ספר לי על אמנון",
}


def test_a_bare_yes_is_the_zero_latency_fast_path():
    for spoken in ("כן", "כן!", "בטח.", "כן בבקשה", "okay"):
        assert _match_pending_prompt(spoken, _FOLLOW_UP_PENDING) == {
            "action": "ask",
            "text": "רוצה לשמוע על הצבא?",
        }, spoken


def test_a_bare_no_is_the_zero_latency_fast_path():
    for spoken in ("לא", "לא תודה.", "לא עכשיו", "no thanks"):
        assert _match_pending_prompt(spoken, _FOLLOW_UP_PENDING) == {
            "action": "dismiss"
        }, spoken


def test_longer_utterances_leave_the_fast_path_undecided():
    """The deterministic layer must never guess on natural phrasing — a
    reply CONTAINING לא or כן returns None so the caller can consult the
    bounded classifier instead."""
    for spoken in (
        "לא סיפרת לי על הצבא",
        "כן אבל קודם ספר לי על אמא שלך",
        "מה זאת אומרת לא?",
        "לא, תספר לי עוד על המשפחה שלך",
    ):
        assert _match_pending_prompt(spoken, _FOLLOW_UP_PENDING) is None, spoken


def test_a_spoken_clarify_option_wins_longest_first():
    """Naming the fuller option must not be swallowed by its prefix, and the
    outgoing text is exactly the button's re-ask shape."""
    match = _match_pending_prompt("על אמנון נחום בבקשה", _CLARIFY_PENDING)
    assert match == {"action": "ask", "text": "ספר לי על אמנון — אמנון נחום"}
    # The bare shorter option still matches on its own.
    assert _match_pending_prompt("אמנון", _CLARIFY_PENDING) == {
        "action": "ask",
        "text": "ספר לי על אמנון — אמנון",
    }


def test_clarify_options_match_whole_words_only():
    """'דן' must not fire inside 'ירדן'."""
    pending = {"kind": "clarify", "options": ["דן"], "original": "ספר לי על דן"}
    assert _match_pending_prompt("ספר לי על ירדן", pending) is None


def test_yes_and_no_do_not_apply_to_a_clarify_prompt():
    """A clarify asks WHICH — a bare כן answers nothing and routes fresh."""
    assert _match_pending_prompt("כן", _CLARIFY_PENDING) is None


@pytest.mark.asyncio
async def test_a_follow_up_turn_arms_the_prompt_and_sends_the_chat_event(monkeypatch):
    """A completed turn with a follow-up speaks the generated question
    itself at the end (the same text the chat card shows), sends it as a
    `follow_up` chat event, and arms pending_prompt for the next
    utterance."""
    from app.services import full_archive_retrieval as far
    from app.services import spoken_answer as sa_mod

    unit = far.UtteranceUnit(
        unit_id="u1", segment_id="seg-a", index=0,
        start_sec=0.0, end_sec=2.0, text="נולדתי בטבריה",
    )
    selection = far.UnitSelection(
        clips=[], selected_units=[unit],
        follow_up={"question": "רוצה לשמוע על הצבא?", "unit_ids": ["u9"]},
    )

    async def fake_select(question, group_id, recording_language, session_id):
        return selection

    async def no_names(segment_ids, group_id):
        return {}

    monkeypatch.setattr(far, "select_units", fake_select)
    monkeypatch.setattr(sa_mod, "_entity_names_by_segment", no_names)

    m = ConnectionManager()
    ws = _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"

    async def swallow_persist(*a, **k):
        return None

    monkeypatch.setattr(m, "_persist_message", swallow_persist)

    await m._handle_text_input_inner("s1", "איפה נולדת?")

    spoken = [msg for msg in ws.sent if msg.get("type") == "message"]
    assert spoken and spoken[-1]["content"] == "נולדתי בטבריה רוצה לשמוע על הצבא?"
    events = [msg for msg in ws.sent if msg.get("type") == "follow_up"]
    assert events == [{"type": "follow_up", "question": "רוצה לשמוע על הצבא?"}]
    assert m.session_data["s1"]["pending_prompt"] == {
        "kind": "follow_up",
        "question": "רוצה לשמוע על הצבא?",
    }


@pytest.mark.asyncio
async def test_a_spoken_yes_asks_the_follow_up_question_itself(monkeypatch):
    """With the prompt armed, 'כן' reaches the engine as the OFFERED
    question — and that question is what persists as the user turn, so
    history and coreference read the real question, not 'כן'."""
    from app.services import full_archive_retrieval as far

    asked = []

    async def fake_select(question, group_id, recording_language, session_id):
        asked.append(question)
        return far.UnitSelection(clips=[], selected_units=[])

    monkeypatch.setattr(far, "select_units", fake_select)

    m = ConnectionManager()
    _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"
    m.session_data["s1"]["pending_prompt"] = dict(_FOLLOW_UP_PENDING)

    persisted = []

    async def record_persist(session_id, role, content, latency=None, metadata=None, video_url=None):
        persisted.append({"role": role, "content": content})

    monkeypatch.setattr(m, "_persist_message", record_persist)

    await m._handle_text_input_inner("s1", "כן")

    assert asked == ["רוצה לשמוע על הצבא?"]
    user_rows = [p for p in persisted if p["role"] == "user"]
    assert user_rows == [{"role": "user", "content": "רוצה לשמוע על הצבא?"}]
    assert "pending_prompt" not in m.session_data["s1"]


@pytest.mark.asyncio
async def test_a_spoken_no_speaks_the_fixed_ack_without_the_engine(monkeypatch):
    """'לא' answers the OFFER, not the archive: the fixed ack is spoken and
    the engine is never called."""
    from app.services import full_archive_retrieval as far
    from app.services import spoken_answer as sa_mod

    async def must_not_run(question, group_id, recording_language, session_id):
        raise AssertionError("a declined offer must never reach the engine")

    monkeypatch.setattr(far, "select_units", must_not_run)

    m = ConnectionManager()
    ws = _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"
    m.session_data["s1"]["pending_prompt"] = dict(_FOLLOW_UP_PENDING)

    async def swallow_persist(*a, **k):
        return None

    monkeypatch.setattr(m, "_persist_message", swallow_persist)

    await m._handle_text_input_inner("s1", "לא תודה")

    spoken = [msg for msg in ws.sent if msg.get("type") == "message"]
    assert spoken and spoken[-1]["content"] == sa_mod.FOLLOW_UP_DECLINE_ACK
    assert "pending_prompt" not in m.session_data["s1"]


@pytest.mark.asyncio
async def test_an_unrelated_reply_clears_the_prompt_and_routes_fresh(monkeypatch):
    """A reply the classifier labels unrelated is a fresh question — same
    rule as v2, where typing a new question abandons the clarify buttons."""
    from app import websocket as wsmod
    from app.services import full_archive_retrieval as far

    asked = []

    async def fake_select(question, group_id, recording_language, session_id):
        asked.append(question)
        return far.UnitSelection(clips=[], selected_units=[])

    async def fake_classify(offered_question, utterance):
        assert offered_question == "רוצה לשמוע על הצבא?"
        assert utterance == "מי האחים שלך?"
        return "unrelated"

    monkeypatch.setattr(far, "select_units", fake_select)
    monkeypatch.setattr(wsmod, "_classify_prompt_reply", fake_classify)

    m = ConnectionManager()
    _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"
    m.session_data["s1"]["pending_prompt"] = dict(_FOLLOW_UP_PENDING)

    async def swallow_persist(*a, **k):
        return None

    monkeypatch.setattr(m, "_persist_message", swallow_persist)

    await m._handle_text_input_inner("s1", "מי האחים שלך?")

    assert asked == ["מי האחים שלך?"]
    assert "pending_prompt" not in m.session_data["s1"]


@pytest.mark.asyncio
async def test_a_spoken_clarify_name_reasks_like_the_button(monkeypatch):
    """Saying the person's name after a clarify sends the button's exact
    re-ask: original question — option."""
    from app.services import full_archive_retrieval as far

    asked = []

    async def fake_select(question, group_id, recording_language, session_id):
        asked.append(question)
        return far.UnitSelection(clips=[], selected_units=[])

    monkeypatch.setattr(far, "select_units", fake_select)

    m = ConnectionManager()
    _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"
    m.session_data["s1"]["pending_prompt"] = dict(_CLARIFY_PENDING)

    async def swallow_persist(*a, **k):
        return None

    monkeypatch.setattr(m, "_persist_message", swallow_persist)

    await m._handle_text_input_inner("s1", "אמנון נחום")

    assert asked == ["ספר לי על אמנון — אמנון נחום"]


# ── the hybrid classifier layer (follow-up offers only) ─────────────────────


@pytest.mark.asyncio
async def test_classifier_accept_asks_the_offered_question(monkeypatch):
    """A phrased acceptance the word-list can't know ('אה כן, למה לא בעצם')
    resolves to the byte-identical offered question — the classifier only
    picked the label, never wrote the text."""
    from app import websocket as wsmod
    from app.services import full_archive_retrieval as far

    asked = []

    async def fake_select(question, group_id, recording_language, session_id):
        asked.append(question)
        return far.UnitSelection(clips=[], selected_units=[])

    async def fake_classify(offered_question, utterance):
        return "accept"

    monkeypatch.setattr(far, "select_units", fake_select)
    monkeypatch.setattr(wsmod, "_classify_prompt_reply", fake_classify)

    m = ConnectionManager()
    _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"
    m.session_data["s1"]["pending_prompt"] = dict(_FOLLOW_UP_PENDING)

    persisted = []

    async def record_persist(session_id, role, content, latency=None, metadata=None, video_url=None):
        persisted.append({"role": role, "content": content})

    monkeypatch.setattr(m, "_persist_message", record_persist)

    await m._handle_text_input_inner("s1", "אה כן, למה לא בעצם")

    assert asked == ["רוצה לשמוע על הצבא?"]
    assert [p for p in persisted if p["role"] == "user"] == [
        {"role": "user", "content": "רוצה לשמוע על הצבא?"}
    ]


@pytest.mark.asyncio
async def test_classifier_decline_speaks_the_ack_without_the_engine(monkeypatch):
    from app import websocket as wsmod
    from app.services import full_archive_retrieval as far
    from app.services import spoken_answer as sa_mod

    async def must_not_run(question, group_id, recording_language, session_id):
        raise AssertionError("a declined offer must never reach the engine")

    async def fake_classify(offered_question, utterance):
        return "decline"

    monkeypatch.setattr(far, "select_units", must_not_run)
    monkeypatch.setattr(wsmod, "_classify_prompt_reply", fake_classify)

    m = ConnectionManager()
    ws = _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"
    m.session_data["s1"]["pending_prompt"] = dict(_FOLLOW_UP_PENDING)

    async def swallow_persist(*a, **k):
        return None

    monkeypatch.setattr(m, "_persist_message", swallow_persist)

    await m._handle_text_input_inner("s1", "לא בא לי כרגע, תודה רבה")

    spoken = [msg for msg in ws.sent if msg.get("type") == "message"]
    assert spoken and spoken[-1]["content"] == sa_mod.FOLLOW_UP_DECLINE_ACK


@pytest.mark.asyncio
async def test_a_bare_yes_never_pays_for_the_classifier(monkeypatch):
    """The fast path must resolve without the LLM round-trip."""
    from app import websocket as wsmod
    from app.services import full_archive_retrieval as far

    asked = []

    async def fake_select(question, group_id, recording_language, session_id):
        asked.append(question)
        return far.UnitSelection(clips=[], selected_units=[])

    async def must_not_classify(offered_question, utterance):
        raise AssertionError("bare 'כן' must not reach the classifier")

    monkeypatch.setattr(far, "select_units", fake_select)
    monkeypatch.setattr(wsmod, "_classify_prompt_reply", must_not_classify)

    m = ConnectionManager()
    _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"
    m.session_data["s1"]["pending_prompt"] = dict(_FOLLOW_UP_PENDING)

    async def swallow_persist(*a, **k):
        return None

    monkeypatch.setattr(m, "_persist_message", swallow_persist)

    await m._handle_text_input_inner("s1", "כן")

    assert asked == ["רוצה לשמוע על הצבא?"]


@pytest.mark.asyncio
async def test_clarify_prompts_never_consult_the_classifier(monkeypatch):
    """The classifier is scoped to follow-up offers; an unmatched reply to
    a clarify routes fresh with zero LLM involvement."""
    from app import websocket as wsmod
    from app.services import full_archive_retrieval as far

    asked = []

    async def fake_select(question, group_id, recording_language, session_id):
        asked.append(question)
        return far.UnitSelection(clips=[], selected_units=[])

    async def must_not_classify(offered_question, utterance):
        raise AssertionError("clarify must never reach the classifier")

    monkeypatch.setattr(far, "select_units", fake_select)
    monkeypatch.setattr(wsmod, "_classify_prompt_reply", must_not_classify)

    m = ConnectionManager()
    _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"
    m.session_data["s1"]["pending_prompt"] = dict(_CLARIFY_PENDING)

    async def swallow_persist(*a, **k):
        return None

    monkeypatch.setattr(m, "_persist_message", swallow_persist)

    await m._handle_text_input_inner("s1", "מה השעה עכשיו?")

    assert asked == ["מה השעה עכשיו?"]


@pytest.mark.asyncio
async def test_classify_prompt_reply_normalizes_valid_labels(monkeypatch):
    from app import websocket as wsmod
    from app.services.llm import llm_service

    replies = iter([' "Accept" ', "decline.", "UNRELATED"])

    async def fake_generate(messages, system_prompt=None, temperature=None, **kw):
        assert temperature == 0
        assert "OFFER: רוצה לשמוע על הצבא?" in messages[0]["content"]
        return next(replies)

    monkeypatch.setattr(llm_service, "generate_response", fake_generate)

    assert await wsmod._classify_prompt_reply("רוצה לשמוע על הצבא?", "טוב") == "accept"
    assert await wsmod._classify_prompt_reply("רוצה לשמוע על הצבא?", "טוב") == "decline"
    assert await wsmod._classify_prompt_reply("רוצה לשמוע על הצבא?", "טוב") == "unrelated"


@pytest.mark.asyncio
async def test_classify_prompt_reply_fails_open_on_error_and_garbage(monkeypatch):
    """Any failure means the status-quo action (fresh question), never a
    wrong accept/decline."""
    from app import websocket as wsmod
    from app.services.llm import llm_service

    async def boom(*a, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(llm_service, "generate_response", boom)
    assert await wsmod._classify_prompt_reply("שאלה?", "משהו") == "unrelated"

    async def garbage(*a, **kw):
        return "maybe yes? hard to say"

    monkeypatch.setattr(llm_service, "generate_response", garbage)
    assert await wsmod._classify_prompt_reply("שאלה?", "משהו") == "unrelated"


# ── v2 pending-prompt wiring (v2-voice-prompts step 2) ──────────────────────
#
# The v2 handler shares the SAME pending_prompt core the avatar uses. The
# load-bearing test is the inertness one: with no prompt armed, the new
# code is a provable no-op for every v2 turn.


def _v2_result(**kw):
    from app.services.video_clip_assembler import VideoClipResult

    return VideoClipResult(video_url=kw.pop("video_url", "http://test/clip.mp4"), **kw)


def _wire_v2(m):
    ws = _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"
    m.session_data["s1"]["producer_chat_mode"] = "video_clips_v2"
    return ws


@pytest.mark.asyncio
async def test_v2_without_pending_is_inert(monkeypatch):
    """No prompt armed → neither the matcher path nor the classifier runs,
    and the assembler receives the utterance untouched."""
    from app import websocket as wsmod
    from app.services import full_archive_retrieval as far

    asked = []

    async def fake_v2(question, group_id, recording_language, session_id):
        asked.append(question)
        return _v2_result(shown_units=[{"key": "k", "unit_id": "u1", "text": "טקסט"}])

    async def must_not_classify(offered_question, utterance):
        raise AssertionError("no pending — the classifier must never run")

    monkeypatch.setattr(far, "assemble_video_clip_response_v2", fake_v2)
    monkeypatch.setattr(wsmod, "_classify_prompt_reply", must_not_classify)

    m = ConnectionManager()
    _wire_v2(m)

    async def swallow_persist(*a, **k):
        return None

    monkeypatch.setattr(m, "_persist_message", swallow_persist)
    monkeypatch.setattr(m, "_ensure_conversation_title", swallow_persist)

    await m._handle_video_clip_question_inner("s1", "ספר לי על אבא שלך")

    assert asked == ["ספר לי על אבא שלך"]
    assert "pending_prompt" not in m.session_data["s1"]


@pytest.mark.asyncio
async def test_v2_follow_up_arms_and_spoken_yes_asks_it(monkeypatch):
    """A v2 response carrying follow_up arms the prompt; the next spoken
    'כן' reaches the assembler as the OFFERED question — and persists as
    the user turn, exactly like the avatar handler."""
    from app import websocket as wsmod
    from app.services import full_archive_retrieval as far

    asked = []
    results = iter(
        [
            _v2_result(
                shown_units=[{"key": "k", "unit_id": "u1", "text": "טקסט"}],
                follow_up={"question": "רוצה לשמוע על הצבא?", "unit_ids": ["u9"]},
            ),
            _v2_result(shown_units=[{"key": "k2", "unit_id": "u9", "text": "צבא"}]),
        ]
    )

    async def fake_v2(question, group_id, recording_language, session_id):
        asked.append(question)
        return next(results)

    monkeypatch.setattr(far, "assemble_video_clip_response_v2", fake_v2)

    m = ConnectionManager()
    _wire_v2(m)

    persisted = []

    async def record_persist(session_id, role, content, latency=None, metadata=None, video_url=None):
        persisted.append({"role": role, "content": content})

    monkeypatch.setattr(m, "_persist_message", record_persist)
    monkeypatch.setattr(m, "_ensure_conversation_title", AsyncMock())

    await m._handle_video_clip_question_inner("s1", "ספר לי על אבא שלך")
    assert m.session_data["s1"]["pending_prompt"] == {
        "kind": "follow_up",
        "question": "רוצה לשמוע על הצבא?",
    }

    await m._handle_video_clip_question_inner("s1", "כן")

    assert asked == ["ספר לי על אבא שלך", "רוצה לשמוע על הצבא?"]
    assert {"role": "user", "content": "רוצה לשמוע על הצבא?"} in persisted
    assert "pending_prompt" not in m.session_data["s1"]


@pytest.mark.asyncio
async def test_v2_spoken_no_sends_text_ack_without_the_engine(monkeypatch):
    from app import websocket as wsmod
    from app.services import full_archive_retrieval as far
    from app.services.spoken_answer import FOLLOW_UP_DECLINE_ACK

    async def must_not_run(question, group_id, recording_language, session_id):
        raise AssertionError("a declined offer must never reach the engine")

    monkeypatch.setattr(far, "assemble_video_clip_response_v2", must_not_run)

    m = ConnectionManager()
    ws = _wire_v2(m)
    m.session_data["s1"]["pending_prompt"] = {
        "kind": "follow_up",
        "question": "רוצה לשמוע על הצבא?",
    }

    persisted = []

    async def record_persist(session_id, role, content, latency=None, metadata=None, video_url=None):
        persisted.append({"role": role, "content": content})

    monkeypatch.setattr(m, "_persist_message", record_persist)

    await m._handle_video_clip_question_inner("s1", "לא תודה")

    acks = [msg for msg in ws.sent if msg.get("type") == "follow_up_ack"]
    assert acks == [{"type": "follow_up_ack", "message": FOLLOW_UP_DECLINE_ACK}]
    assert {"role": "assistant", "content": FOLLOW_UP_DECLINE_ACK} in persisted
    assert "pending_prompt" not in m.session_data["s1"]


@pytest.mark.asyncio
async def test_v2_clarify_arms_and_spoken_name_reasks(monkeypatch):
    from app.services import full_archive_retrieval as far

    asked = []
    results = iter(
        [
            _v2_result(
                video_url=None,
                clarify={"question": "לאיזה אמנון?", "options": ["אמנון", "אמנון נחום"]},
            ),
            _v2_result(shown_units=[{"key": "k", "unit_id": "u1", "text": "טקסט"}]),
        ]
    )

    async def fake_v2(question, group_id, recording_language, session_id):
        asked.append(question)
        return next(results)

    monkeypatch.setattr(far, "assemble_video_clip_response_v2", fake_v2)

    m = ConnectionManager()
    _wire_v2(m)

    monkeypatch.setattr(m, "_persist_message", AsyncMock())
    monkeypatch.setattr(m, "_ensure_conversation_title", AsyncMock())

    await m._handle_video_clip_question_inner("s1", "ספר לי על אמנון")
    assert m.session_data["s1"]["pending_prompt"] == {
        "kind": "clarify",
        "options": ["אמנון", "אמנון נחום"],
        "original": "ספר לי על אמנון",
    }

    await m._handle_video_clip_question_inner("s1", "אמנון נחום")
    assert asked == ["ספר לי על אמנון", "ספר לי על אמנון — אמנון נחום"]
