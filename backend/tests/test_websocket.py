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
async def test_audio_routes_to_video_clip_pipeline_when_producer_opted_in(monkeypatch):
    """When the linked producer's chat_mode is "video_clips", transcribed
    audio must feed the video-clip turn instead of the avatar turn — same
    STT step, different destination, per the producer's own Settings
    toggle (never the family viewer's own preference)."""
    import base64

    from app.services import stt as stt_module

    m = ConnectionManager()
    _wire_session(m)
    m.session_data["s1"]["producer_chat_mode"] = "video_clips"

    async def fake_transcribe(audio, language="en"):
        return "tell me about your brothers"

    monkeypatch.setattr(stt_module.stt_service, "transcribe", fake_transcribe)

    calls = {"text": 0, "video_clip": 0}

    async def fake_text_turn(session_id, text):
        calls["text"] += 1

    async def fake_video_clip_turn(session_id, text):
        calls["video_clip"] += 1
        assert text == "tell me about your brothers"

    m._handle_text_input_inner = fake_text_turn  # type: ignore[assignment]
    m._handle_video_clip_question_inner = fake_video_clip_turn  # type: ignore[assignment]

    await m._handle_audio_inner("s1", base64.b64encode(b"x" * 2000).decode())

    assert calls == {"text": 0, "video_clip": 1}


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
async def test_video_clip_inner_selects_v2_assembler_for_v2_mode(monkeypatch):
    """The shared video-clip handler must dispatch to full_archive_retrieval's v2
    assembler when producer_chat_mode is "video_clips_v2", and to the v1
    assembler otherwise — both have the identical response contract."""
    from app.services import full_archive_retrieval, video_clip_assembler
    from app.services.video_clip_assembler import VideoClipResult

    called = {"v1": 0, "v2": 0}

    async def fake_v1(question, group_id, recording_language, session_id):
        called["v1"] += 1
        return VideoClipResult(video_url="http://x/v1.mp4")

    async def fake_v2(question, group_id, recording_language, session_id):
        called["v2"] += 1
        return VideoClipResult(video_url="http://x/v2.mp4")

    monkeypatch.setattr(video_clip_assembler, "assemble_video_clip_response", fake_v1)
    monkeypatch.setattr(full_archive_retrieval, "assemble_video_clip_response_v2", fake_v2)

    m = ConnectionManager()
    ws = _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"
    m.session_data["s1"]["producer_chat_mode"] = "video_clips_v2"

    await m._handle_video_clip_question_inner("s1", "who is Ilana")

    assert called == {"v1": 0, "v2": 1}
    urls = [msg.get("video_url") for msg in ws.sent if msg["type"] == "video_clip_response"]
    assert urls == ["http://x/v2.mp4"]


@pytest.mark.asyncio
async def test_video_clip_inner_selects_v1_assembler_for_default_video_clips_mode(monkeypatch):
    from app.services import full_archive_retrieval, video_clip_assembler
    from app.services.video_clip_assembler import VideoClipResult

    called = {"v1": 0, "v2": 0}

    async def fake_v1(question, group_id, recording_language, session_id):
        called["v1"] += 1
        return VideoClipResult(video_url="http://x/v1.mp4")

    async def fake_v2(question, group_id, recording_language, session_id):
        called["v2"] += 1
        return VideoClipResult(video_url="http://x/v2.mp4")

    monkeypatch.setattr(video_clip_assembler, "assemble_video_clip_response", fake_v1)
    monkeypatch.setattr(full_archive_retrieval, "assemble_video_clip_response_v2", fake_v2)

    m = ConnectionManager()
    _wire_session(m)
    m.session_data["s1"]["producer_id"] = "producer-1"
    m.session_data["s1"]["producer_chat_mode"] = "video_clips"

    await m._handle_video_clip_question_inner("s1", "tell me about your family")

    assert called == {"v1": 1, "v2": 0}


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
        id="sess-lang", user_id=producer.id, avatar_id=avatar.id, status="active"
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
