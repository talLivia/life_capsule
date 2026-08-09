import asyncio
import base64
import json
import logging
import os
import re
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from fastapi import WebSocket

from app.services import gpu_client
from app.services.storage import storage_service
from app.telemetry import span

logger = logging.getLogger(__name__)
TMPDIR = Path(tempfile.gettempdir())

_RAW_TRACE_PATH = TMPDIR / "avatar_raw_trace.log"


def _raw_trace(msg: str) -> None:
    """Logging-module-independent trace — bypasses configure_logging()
    entirely, for the current investigation into why request-level log
    lines weren't reaching the configured file handler."""
    try:
        with open(_RAW_TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except OSError:
        pass

# Owner-only file/dir modes — keep another user on a shared host from
# eavesdropping on raw audio inputs or in-flight video chunks.
_OWNER_ONLY_FILE = stat.S_IRUSR | stat.S_IWUSR  # 0o600
_OWNER_ONLY_DIR = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR  # 0o700


def _private_session_dir(session_id: str) -> Path:
    """
    Return (creating if needed) a per-session subdirectory of TMPDIR with
    mode 0o700. Anything written inside is invisible to other UNIX users —
    cheaper than chmod'ing each tmp file after creation.
    """
    d = TMPDIR / f"avatar-session-{session_id}"
    d.mkdir(mode=_OWNER_ONLY_DIR, exist_ok=True)
    # If the dir already existed with a looser mode, tighten it now.
    try:
        os.chmod(str(d), _OWNER_ONLY_DIR)
    except OSError:
        pass
    return d


def _write_private_bytes(path: Path, data: bytes) -> None:
    """Atomically write bytes to `path` with file mode 0o600 (owner-only)."""
    # os.open lets us set the mode at create time so there's no readable window
    # between create and chmod. O_CREAT|O_TRUNC|O_WRONLY is the standard "open
    # for writing, truncate, create if missing" combination.
    #
    # O_BINARY is critical on Windows: without it, os.open()/os.write() can
    # open the fd in TEXT mode, silently translating every 0x0A byte in the
    # data to 0x0D 0x0A on write. For binary audio (WebM/Opus), 0x0A occurs
    # by chance roughly every 256 bytes, and each occurrence corrupts the
    # container's byte-precise structure — confirmed directly: a real
    # recording written this way decoded via PyAV as 0.06s of audio instead
    # of its actual ~2-6s, which faster-whisper then (correctly, per the
    # corrupted input) transcribed as empty. os.O_BINARY doesn't exist on
    # POSIX, hence the getattr fallback to 0 (no-op there).
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
        _OWNER_ONLY_FILE,
    )
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(str(path), _OWNER_ONLY_FILE)
    except OSError:
        pass


# ── chunking thresholds (first-frame latency vs prosody trade-off) ──────────
# Used by `_response_producer` to pace response_assembler.py's fully-known
# assembled text (Prompts 6-9) through the same TTS/lip-sync consumer a
# live-streamed LLM reply would use.
# The opening fragment ships at the first CLAUSE boundary (comma/semicolon/
# colon/dash) once it's long enough, so audio+video start as early as
# possible. Every chunk after that uses SENTENCE boundaries — fewer TTS
# calls and smoother prosody for the bulk of the reply.
_MIN_SENTENCE_LEN = 8
_MIN_FIRST_CHUNK_LEN = 10  # ship the opening clause fast (~2 words)
# Force-flush a run-on with no usable punctuation so we never stall waiting
# for a boundary that may never come.
_MAX_CHUNK_CHARS = 200

# Boundary regexes. Lookbehind keeps the punctuation attached to the chunk
# (better TTS prosody than trailing a bare clause).
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_RE = re.compile(r"(?<=[.!?,;:—])\s+")


def _drain_chunks(buf: str, sep_re: "re.Pattern[str]", min_len: int, max_len: int):
    """
    Pull speakable chunks out of an in-progress LLM buffer without ever
    dropping text. Returns (chunks_ready_to_speak, remaining_buffer).

    A chunk is emitted at a punctuation boundary only once the text up to
    that boundary is at least `min_len` chars — short leading fragments
    (e.g. "Hi,") stay in the buffer and merge forward instead of being
    spoken as their own tiny clip. A run-on longer than `max_len` with no
    usable boundary is force-flushed at the last space.
    """
    chunks: list[str] = []

    # Emit complete punctuation-bounded segments that meet the length bar.
    while True:
        emitted = False
        for m in sep_re.finditer(buf):
            head = buf[: m.end()].strip()
            if len(head) >= min_len:
                chunks.append(head)
                buf = buf[m.end() :]
                emitted = True
                break
        if not emitted:
            break

    # Backstop: no punctuation but the buffer is getting long — cut at a space.
    while len(buf) >= max_len:
        cut = buf.rfind(" ", 0, max_len)
        if cut <= 0:
            break
        chunks.append(buf[:cut].strip())
        buf = buf[cut:].lstrip()

    return chunks, buf


# Per-message input cap. Long inputs waste LLM tokens and create DoS surface.
MAX_TEXT_INPUT_LEN = 4000

# Conversation memory cap — keep the most recent N user/assistant pairs.
# System prompt is stored separately so it survives trimming.
MAX_CONTEXT_MESSAGES = 60

# Soft TTL for an idle (disconnected/abandoned) session in seconds.
STALE_SESSION_TTL_SECS = 60 * 60 * 2  # 2 hours


class ConnectionManager:
    """Manage WebSocket connections and the real-time avatar pipeline."""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.session_data: Dict[str, dict] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        # Serializes connect/disconnect/cleanup-snapshot so the stale-session
        # reaper can't race a fresh connection for the same session id.
        self._mutation_lock = asyncio.Lock()
        # Per-session handle to the currently-running turn task, used for
        # barge-in: when a fresh user input arrives we cancel the in-flight
        # task instead of queueing.
        self._active_turns: Dict[str, asyncio.Task] = {}
        # Per-session send lock. The turn task streams chunks while the WS
        # receive loop may also send (pong/error) — without this, two
        # coroutines could interleave mid-frame and corrupt the connection.
        self._send_locks: Dict[str, asyncio.Lock] = {}

    # ── connection lifecycle ──────────────────────────────────────────────────

    async def connect(self, session_id: str, websocket: WebSocket, user_id: Optional[str] = None):
        await websocket.accept()
        self.active_connections[session_id] = websocket
        self._send_locks[session_id] = asyncio.Lock()
        self.session_data[session_id] = {
            "messages": [],
            "avatar_id": None,
            "avatar_image_key": None,
            "avatar_image_local": None,
            "voice_wav": None,
            "language": "en",
            "user_id": user_id,
            # Populated in _load_session_data from the avatar's owner — the
            # storyteller whose archive response_assembler.py (Prompts 6-9)
            # searches, which may differ from `user_id` (a family member
            # chatting with someone else's avatar).
            "producer_id": None,
            "producer_recording_language": "en",
            # Prompt 14's follow-up: which pipeline a transcribed "audio"
            # message should feed into — the avatar path (default) or the
            # video-clip path, per the PRODUCER's own Settings toggle (see
            # _handle_audio_inner below). Never the family viewer's own
            # setting; there isn't one (see User.chat_mode's docstring).
            "producer_chat_mode": "avatar",
            "connected_at": datetime.now(timezone.utc),
            "last_activity": datetime.now(timezone.utc),
        }
        await self._load_session_data(session_id)
        logger.info(f"WebSocket connected: {session_id} (user={user_id})")

    async def _load_session_data(self, session_id: str):
        try:
            from sqlalchemy import select
            from sqlalchemy.orm import joinedload

            from app.database import AsyncSessionLocal
            from app.models import Message, User
            from app.models import Session as SessionModel

            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(SessionModel)
                    .options(joinedload(SessionModel.avatar))
                    .where(SessionModel.id == session_id)
                )
                session = result.scalar_one_or_none()
                if not session:
                    return

                self.session_data[session_id]["avatar_id"] = session.avatar_id
                # Trust DB owner over caller-supplied claim
                if session.user_id:
                    self.session_data[session_id]["user_id"] = session.user_id

                # Rehydrate the LLM context window from persisted messages so
                # a reconnect (refresh, network blip, etc.) resumes the same
                # conversation instead of starting fresh. We pull the most
                # recent MAX_CONTEXT_MESSAGES rows to bound memory. Order by
                # (created_at, id) so ties (when several rows share a
                # sub-millisecond timestamp on bulk insert) are still stable
                # — message IDs are monotonic UUIDs assigned in insertion order
                # per session, so they make a reliable secondary key.
                hist_result = await db.execute(
                    select(Message.role, Message.content)
                    .where(Message.session_id == session_id)
                    .where(Message.role.in_(("user", "assistant")))
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(MAX_CONTEXT_MESSAGES)
                )
                # Reverse to chronological order for the LLM
                hist_rows = list(hist_result.all())[::-1]
                if hist_rows:
                    self.session_data[session_id]["messages"] = [
                        {"role": row.role, "content": row.content} for row in hist_rows
                    ]
                    logger.info(f"Rehydrated {len(hist_rows)} message(s) for session {session_id}")

                avatar = session.avatar
                if avatar:
                    self.session_data[session_id]["avatar_image_key"] = avatar.s3_key
                    local = await self._resolve_local_image(avatar)
                    self.session_data[session_id]["avatar_image_local"] = local

                    if avatar.voice_id:
                        wav = await self._get_voice_wav_path(avatar.voice_id)
                        if wav:
                            self.session_data[session_id]["voice_wav"] = wav
                            logger.info(
                                f"Auto-loaded voice {avatar.voice_id} for session {session_id}"
                            )
                    logger.info(f"Loaded avatar {avatar.id} for session {session_id}")

                    # response_assembler.py needs the STORYTELLER's archive to
                    # search (Prompt 6's group_id) and recording language —
                    # the avatar's owner, not necessarily the chatting user
                    # (a family member's session belongs to them, but the
                    # avatar belongs to the producer whose stories they're
                    # asking about; see sessions.py's create_session for the
                    # access check that authorized this in the first place).
                    producer_result = await db.execute(
                        select(User).where(User.id == avatar.user_id)
                    )
                    producer = producer_result.scalar_one_or_none()
                    if producer:
                        self.session_data[session_id]["producer_id"] = producer.id
                        self.session_data[session_id][
                            "producer_recording_language"
                        ] = producer.recording_language
                        self.session_data[session_id]["producer_chat_mode"] = producer.chat_mode
                        # "language" (used by both STT transcription and TTS
                        # synthesis, see _handle_audio_inner/_animate_from_
                        # queue) defaulted to "en" in connect() — but every
                        # word this pipeline ever speaks or needs to
                        # transcribe is in the STORYTELLER's own recording
                        # language, never a generic English default. Confirmed
                        # live: leaving this at "en" made Edge TTS try to
                        # speak a Hebrew archive's verbatim text with an
                        # English voice, which can't vocalize the Hebrew
                        # words at all — only a literal digit like "14"
                        # embedded in the sentence came through audible.
                        # Still overridable via an explicit "set_language" WS
                        # message (e.g. a family member who wants to ask
                        # questions in a different language than the archive).
                        self.session_data[session_id]["language"] = producer.recording_language

        except Exception as e:
            logger.error(f"Failed to load session data for {session_id}: {e}")

    async def _get_voice_wav_path(self, voice_id: str) -> Optional[str]:
        """Return the WAV filesystem path for a voice profile, or None if not found."""
        voice_index = Path("voice_profiles") / "index.json"
        if not voice_index.exists():
            return None
        try:
            raw = await asyncio.to_thread(voice_index.read_text)
            for entry in json.loads(raw):
                if entry["id"] == voice_id:
                    return entry.get("wav_path")
        except Exception as e:
            logger.warning(f"Could not read voice index: {e}")
        return None

    async def _get_voice_entry(self, voice_id: str) -> Optional[dict]:
        """Return the full voice-index entry (including `user_id`) for ownership checks."""
        voice_index = Path("voice_profiles") / "index.json"
        if not voice_index.exists():
            return None
        try:
            raw = await asyncio.to_thread(voice_index.read_text)
            for entry in json.loads(raw):
                if entry["id"] == voice_id:
                    return entry
        except Exception as e:
            logger.warning(f"Could not read voice index: {e}")
        return None

    async def _resolve_local_image(self, avatar) -> str:
        """Return a local FS path to the avatar image, downloading from S3 if needed."""
        cache_path = TMPDIR / "avatars" / f"{avatar.id}.jpg"
        if cache_path.exists():
            return str(cache_path)

        # Local storage: use get_local_path directly
        try:
            local = storage_service.get_local_path(avatar.s3_key)
            if Path(local).exists():
                return local
        except (NotImplementedError, AttributeError):
            pass

        # S3 fallback: download and cache locally for the animator
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = await storage_service.download_file(avatar.s3_key)
        cache_path.write_bytes(data)
        return str(cache_path)

    async def disconnect(self, session_id: str):
        # Cancel any in-flight LLM/TTS/animation task for this session so it
        # doesn't keep churning after the client is gone (wasted tokens + GPU).
        task = self._active_turns.pop(session_id, None)
        if task and not task.done():
            task.cancel()

        ws = self.active_connections.pop(session_id, None)
        self.session_data.pop(session_id, None)
        self._send_locks.pop(session_id, None)

        # Actually close the socket. Disconnect is also reached when the
        # stale-session reaper or a REST "end session" tears a session down —
        # without this, the client's connection stayed open with all its
        # server-side state already gone (a zombie that ignored every input).
        if ws is not None:
            close = getattr(ws, "close", None)
            if close is not None:
                try:
                    await close(code=1000)
                except Exception:
                    pass  # already closed by the peer
        # Best-effort wipe of the per-session temp dir. We use shutil.rmtree
        # via to_thread because rmtree on a large dir can briefly block.
        session_dir = TMPDIR / f"avatar-session-{session_id}"
        if session_dir.exists():
            try:
                import shutil

                await asyncio.to_thread(shutil.rmtree, str(session_dir), True)
            except Exception as e:
                logger.warning(f"Could not clean session tmp dir for {session_id}: {e}")
        logger.info(f"WebSocket disconnected: {session_id}")

    async def interrupt_active_turn(self, session_id: str) -> bool:
        """
        Cancel any in-flight turn for this session ("barge-in"). Returns
        True if a turn was actually interrupted. Used when fresh user audio
        arrives mid-response — modern voice-AI UX expects sub-100 ms cutoff
        so the user doesn't keep hearing the previous response while talking.
        """
        task = self._active_turns.pop(session_id, None)
        if task and not task.done():
            task.cancel()
            # Tell the client to stop playing the queued video chunks too —
            # otherwise they'd keep arriving from the buffer.
            await self.send_message(
                session_id,
                {
                    "type": "interrupted",
                    "message": "Previous response interrupted",
                },
            )
            return True
        return False

    async def send_message(self, session_id: str, message: dict):
        ws = self.active_connections.get(session_id)
        if not ws:
            return
        lock = self._send_locks.get(session_id)
        try:
            if lock is not None:
                async with lock:
                    await ws.send_json(message)
            else:
                await ws.send_json(message)
        except Exception as e:
            logger.error(f"Send failed [{session_id}]: {e}")
            await self.disconnect(session_id)

    # ── DB persistence helpers ────────────────────────────────────────────────

    async def _persist_message(
        self,
        session_id: str,
        role: str,
        content: str,
        latency: Optional[float] = None,
        metadata: Optional[dict] = None,
        video_url: Optional[str] = None,
    ) -> None:
        """Best-effort persist a message; failure must not break the chat pipeline.

        `content` is always the READABLE text of the turn. A video answer puts
        its URL in `video_url` (the column that exists for it) rather than
        stuffing it into `content` — otherwise the stored conversation is a
        list of links that nobody, including the archive-read call's history
        block, can read."""
        try:
            from app.database import AsyncSessionLocal
            from app.models import Message

            async with AsyncSessionLocal() as db:
                db.add(
                    Message(
                        session_id=session_id,
                        role=role,
                        content=content,
                        content_type="video" if video_url else "text",
                        video_url=video_url,
                        latency=latency,
                        message_metadata=metadata,
                    )
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"Could not persist {role} message for {session_id}: {e}")

    async def _ensure_conversation_title(self, session_id: str, first_user_text: str) -> None:
        """
        Lazily create a Conversation row for the session and seed its title from
        the first user turn. Idempotent — safe to call on every text input.

        Title heuristic: first 60 chars of the user's message, trimmed at a
        word boundary. Cheap, no extra LLM call.
        """
        try:
            from sqlalchemy import func, select

            from app.database import AsyncSessionLocal
            from app.models import Conversation, Message

            async with AsyncSessionLocal() as db:
                exists = await db.execute(
                    select(Conversation.id).where(Conversation.session_id == session_id).limit(1)
                )
                if exists.scalar_one_or_none():
                    return

                snippet = first_user_text.strip().replace("\n", " ")
                if len(snippet) > 60:
                    cutoff = snippet.rfind(" ", 0, 60)
                    snippet = snippet[: cutoff if cutoff > 30 else 60].rstrip(",.!?;:") + "…"

                count_res = await db.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.session_id == session_id)
                )
                msg_count = int(count_res.scalar() or 0)

                db.add(
                    Conversation(
                        session_id=session_id,
                        title=snippet or "New Conversation",
                        message_count=msg_count,
                    )
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"Could not auto-title conversation for {session_id}: {e}")

    # ── handlers ──────────────────────────────────────────────────────────────

    def _spawn_turn(self, session_id: str, coro) -> None:
        """
        Register `coro` as the session's active turn and schedule it WITHOUT
        awaiting. This is the heart of barge-in: the WebSocket receive loop
        must return to `receive_json()` immediately so it can observe the next
        client message (a new turn or an explicit stop) while this one streams.

        A done-callback clears the slot and logs unhandled errors. We never
        await the task here, so an exception inside it can't crash the WS loop.
        """
        task = asyncio.create_task(coro, name=f"turn-{session_id}")
        self._active_turns[session_id] = task

        def _done(t: asyncio.Task) -> None:
            if self._active_turns.get(session_id) is t:
                self._active_turns.pop(session_id, None)
            if t.cancelled():
                _raw_trace(f"TURN {session_id} CANCELLED (barge-in or disconnect)")
                logger.info(f"Turn for {session_id} cancelled (barge-in or disconnect)")
                return
            exc = t.exception()
            if exc is not None:
                _raw_trace(f"TURN {session_id} FAILED: {exc!r}")
                logger.error(f"Turn task for {session_id} failed: {exc!r}")
            else:
                _raw_trace(f"TURN {session_id} completed normally")

        task.add_done_callback(_done)

    async def handle_audio_input(self, session_id: str, audio_data: str):
        """
        Non-blocking dispatcher: interrupt any prior turn, then run STT +
        the full chat turn inside a tracked task so the WS loop stays free to
        receive the next message (enabling barge-in even mid-transcription).
        """
        await self.interrupt_active_turn(session_id)
        self._spawn_turn(session_id, self._handle_audio_inner(session_id, audio_data))

    async def _handle_audio_inner(self, session_id: str, audio_data: str) -> None:
        _raw_trace(f"_handle_audio_inner ENTER {session_id} audio_len={len(audio_data or '')}")
        # Unique per call — continuous listening can fire a new audio message
        # every few seconds, and interrupt_active_turn cancelling the OLD
        # turn's asyncio Task does NOT stop its transcribe() call already
        # running inside asyncio.to_thread (cancellation doesn't kill the
        # underlying thread). With a fixed "input.webm" name shared by every
        # message in the session, the OLD turn's `finally: tmp_audio.unlink()`
        # could delete the file out from under a NEW turn's in-flight read
        # (or the new write could land mid-read of the old one) — exactly
        # the kind of race that silently yields empty transcriptions.
        tmp_audio = _private_session_dir(session_id) / f"input-{uuid.uuid4().hex}.webm"
        try:
            await self.send_message(
                session_id,
                {"type": "status", "message": "Transcribing audio…", "stage": "transcription"},
            )
            _raw_trace(f"_handle_audio_inner sent status {session_id}")

            try:
                raw = base64.b64decode(audio_data, validate=False)
            except Exception:
                await self.send_message(
                    session_id, {"type": "error", "message": "Invalid audio data"}
                )
                return

            # 50 MB hard cap so a malicious client cannot OOM the server
            if len(raw) > 50 * 1024 * 1024:
                await self.send_message(
                    session_id, {"type": "error", "message": "Audio payload too large"}
                )
                return

            await asyncio.to_thread(_write_private_bytes, tmp_audio, raw)
            # Transcribe in the session's selected language — otherwise Whisper
            # assumes English and garbles non-English speech.
            language = self.session_data.get(session_id, {}).get("language", "en")
            logger.info(f"Transcribing audio [{session_id}]: {len(raw)} bytes, lang={language}")
            _raw_trace(f"_handle_audio_inner about to transcribe {session_id} bytes={len(raw)} lang={language}")
            text = await gpu_client.transcribe(str(tmp_audio), language=language)
            _raw_trace(f"_handle_audio_inner transcribe returned {session_id} text={text!r}")

            if not text:
                # Keep a copy for offline debugging — the finally block below
                # unlinks tmp_audio regardless, so without this an empty
                # transcription can never be inspected after the fact.
                debug_path = TMPDIR / f"debug-failed-transcribe-{session_id}-{int(datetime.now(timezone.utc).timestamp())}.webm"
                try:
                    debug_path.write_bytes(raw)
                    logger.warning(f"Empty transcription [{session_id}]: saved audio to {debug_path}")
                except OSError:
                    pass
                await self.send_message(
                    session_id, {"type": "error", "message": "Could not transcribe audio"}
                )
                return

            await self.send_message(session_id, {"type": "transcription", "text": text})
            # Route the transcribed text to whichever pipeline the PRODUCER's
            # own Settings toggle selects (Prompt 14) — same STT step either
            # way, just a different destination. Run directly (we're already
            # inside the tracked task).
            chat_mode = self.session_data.get(session_id, {}).get("producer_chat_mode", "avatar")
            if chat_mode in ("video_clips", "video_clips_v2"):
                await self._handle_video_clip_question_inner(session_id, text)
            else:
                await self._handle_text_input_inner(session_id, text)

        except asyncio.CancelledError:
            _raw_trace(f"_handle_audio_inner CANCELLED {session_id}")
            raise  # propagate barge-in cancellation cleanly
        except Exception as e:
            _raw_trace(f"_handle_audio_inner EXCEPTION {session_id} {type(e).__name__}: {e}")
            logger.error(f"Audio error [{session_id}]: {type(e).__name__}: {e}", exc_info=True)
            await self.send_message(
                session_id, {"type": "error", "message": "Audio processing failed"}
            )
        finally:
            tmp_audio.unlink(missing_ok=True)
            _raw_trace(f"_handle_audio_inner EXIT {session_id}")

    async def handle_text_input(self, session_id: str, text: str):
        """
        Non-blocking dispatcher for a text turn. Validates inline (so the
        client gets immediate feedback on empty/oversized input), interrupts
        any in-flight turn, then spawns the streaming pipeline as a tracked
        task and returns immediately.

        Pipeline (inside `_handle_text_input_inner`):
          1. Produce the reply text → `token` event(s) for live UI display
          2. Detect sentence boundaries → enqueue complete sentences
          3. Consumer runs TTS + animation per sentence, streaming
             `video_chunk` events as each completes

        Step 1 is `response_assembler.assemble_response()` (Prompts 6-9) —
        retrieval + relevance scoring + bridge-phrase assembly against the
        storyteller's own archive, never the general-purpose LLM (removed
        in Prompt 1). It's the only thing allowed to reach TTS/lip-sync.
        """
        text = (text or "").strip()
        if not text:
            await self.send_message(session_id, {"type": "error", "message": "Empty message"})
            return
        if len(text) > MAX_TEXT_INPUT_LEN:
            await self.send_message(
                session_id,
                {
                    "type": "error",
                    "message": f"Message too long ({len(text)} chars). Limit is {MAX_TEXT_INPUT_LEN}.",
                },
            )
            return

        await self.interrupt_active_turn(session_id)
        self._spawn_turn(session_id, self._handle_text_input_inner(session_id, text))

    async def _handle_text_input_inner(self, session_id: str, text: str):
        started_at = datetime.now(timezone.utc)

        try:
            data = self.session_data.get(session_id, {})
            data["last_activity"] = started_at
            messages: list[dict] = data.get("messages", [])
            messages.append({"role": "user", "content": text})

            # Cap the conversation window. The system prompt is passed
            # separately to the LLM so we don't need to keep it in `messages`.
            if len(messages) > MAX_CONTEXT_MESSAGES:
                messages = messages[-MAX_CONTEXT_MESSAGES:]

            # Persist the user turn before kicking off generation so it's
            # durable even if the model fails partway through.
            await self._persist_message(session_id, "user", text)
            # Auto-title the conversation from the first user turn (idempotent)
            await self._ensure_conversation_title(session_id, text)

            await self.send_message(
                session_id, {"type": "status", "message": "Thinking…", "stage": "llm"}
            )

            # Bounded queue prevents the response producer from racing too far ahead
            sentence_queue: asyncio.Queue[Optional[str]] = asyncio.Queue(maxsize=4)

            # Parent span for the whole turn — child spans (response.produce,
            # tts.synthesize, avatar.animate, storage.upload) nest under it
            # so a trace shows exactly where a slow turn spent its time.
            with span("chat.turn", **{"input_chars": len(text)}):
                results = await asyncio.gather(
                    self._response_producer(session_id, sentence_queue, text),
                    self._animate_from_queue(session_id, sentence_queue),
                    return_exceptions=True,
                )

            # Check for errors
            for r in results:
                if isinstance(r, Exception):
                    raise r

            response_text = results[0] if isinstance(results[0], str) else ""
            if response_text:
                messages.append({"role": "assistant", "content": response_text})
                data["messages"] = messages
                latency = (datetime.now(timezone.utc) - started_at).total_seconds()
                await self._persist_message(session_id, "assistant", response_text, latency=latency)

        except Exception as e:
            logger.error(f"Text error [{session_id}]: {type(e).__name__}: {e}", exc_info=True)
            await self.send_message(
                session_id,
                {"type": "error", "message": f"Processing failed: {type(e).__name__}: {e}"},
            )

    # ── original-video-clip chat mode (Prompt 13) ───────────────────────────────
    #
    # A SEPARATE mode from handle_text_input/_handle_text_input_inner above —
    # returns a real, verbatim video clip assembled from the storyteller's own
    # recordings instead of an avatar's synthesized TTS+lip-sync reply. Built
    # alongside the avatar path, not replacing it (see docs/poc-claude-code-
    # prompts.md's "Shared context" hard constraint) — this is why it gets its
    # own message types ("video_clip_question" in, "video_clip_response"/
    # "video_clip_no_story" out) rather than repurposing "text"/"token"/
    # "video_chunk", whose contract belongs entirely to the avatar pipeline.

    async def handle_video_clip_question(self, session_id: str, text: str):
        """Non-blocking dispatcher, mirroring handle_text_input's shape:
        validates inline, interrupts any in-flight turn (avatar or video-clip
        — one active turn per session either way), then spawns the work as a
        tracked task and returns immediately."""
        text = (text or "").strip()
        if not text:
            await self.send_message(session_id, {"type": "error", "message": "Empty message"})
            return
        if len(text) > MAX_TEXT_INPUT_LEN:
            await self.send_message(
                session_id,
                {
                    "type": "error",
                    "message": f"Message too long ({len(text)} chars). Limit is {MAX_TEXT_INPUT_LEN}.",
                },
            )
            return

        await self.interrupt_active_turn(session_id)
        self._spawn_turn(session_id, self._handle_video_clip_question_inner(session_id, text))

    async def _handle_video_clip_question_inner(self, session_id: str, text: str):
        from app.services import full_archive_retrieval, video_clip_assembler

        started_at = datetime.now(timezone.utc)
        data = self.session_data.get(session_id, {})
        data["last_activity"] = started_at
        group_id = data.get("producer_id")
        recording_language = data.get("producer_recording_language", "en")
        # Both video-clip modes share this handler and an identical response
        # contract (a clip URL or the no-story fallback); only the range-
        # decision backend differs. Prompt 15's experimental full-archive
        # reader ("video_clips_v2") is selected here, otherwise the Prompt
        # 11-14 chunk-retrieval assembler.
        chat_mode = data.get("producer_chat_mode", "avatar")
        assembler = (
            full_archive_retrieval.assemble_video_clip_response_v2
            if chat_mode == "video_clips_v2"
            else video_clip_assembler.assemble_video_clip_response
        )

        # The try below must wrap EVERYTHING from here, not just the
        # assemble_video_clip_response call — _persist_message/
        # _ensure_conversation_title used to sit outside it (mirrored from
        # an earlier draft), meaning an unexpected failure in either would
        # propagate uncaught out of this task with no "error" message ever
        # sent, leaving the frontend stuck on "Finding a clip…" forever with
        # nothing in the browser console. Matches _handle_text_input_inner's
        # own convention (its try already wraps its persist/title calls too).
        try:
            await self._persist_message(session_id, "user", text)
            await self._ensure_conversation_title(session_id, text)
            await self.send_message(
                session_id, {"type": "status", "message": "Finding a clip…", "stage": "video_clip"}
            )

            if not group_id:
                await self.send_message(
                    session_id,
                    {"type": "video_clip_no_story", "message": self._NO_PIPELINE_MSG},
                )
                return

            with span("video_clip.assemble"):
                result = await assembler(
                    question=text,
                    group_id=group_id,
                    recording_language=recording_language,
                    session_id=session_id,
                )

            # Two people share a name and the question did not say which.
            # Sent BEFORE the no-story check, because a clarification is an
            # empty selection: falling through would say the archive holds
            # nothing about אמנון when it holds two of them.
            if result.clarify:
                await self._persist_message(
                    session_id, "assistant", result.clarify["question"]
                )
                await self.send_message(
                    session_id,
                    {
                        "type": "video_clip_clarify",
                        "question": result.clarify["question"],
                        "options": result.clarify["options"],
                    },
                )
                return

            # An outage, not an answer. A SEPARATE message type so the client
            # can never render it the way it renders "the archive has nothing
            # about that" — the two were the same value until 2026-08-09, and
            # that is how a failed API call came to tell a family member their
            # relative had no story about someone.
            if result.read_failed:
                await self.send_message(
                    session_id,
                    {
                        "type": "video_clip_failed",
                        "message": result.fallback_text,
                        "question": text,
                    },
                )
                return

            if result.no_story or not result.video_url:
                await self.send_message(
                    session_id,
                    {
                        "type": "video_clip_no_story",
                        "message": result.fallback_text,
                        # Carried on a no-story turn too: "I have nothing for
                        # that, but do you want to hear about X?" is a real
                        # answer, and dropping it is how the system came to
                        # say there was nothing more when there was.
                        "follow_up": result.follow_up,
                    },
                )
                return

            latency = (datetime.now(timezone.utc) - started_at).total_seconds()
            # v2 reports which utterance units it played. ONE source of truth:
            # the spoken text stored as the message body, the text sent to the
            # client, and the per-unit records the next turn reads are all
            # derived from this same list — they cannot drift apart.
            # v1 has no unit concept, so it falls back to the URL as before.
            spoken_text = " ".join(
                u.get("text", "").strip() for u in result.shown_units if u.get("text")
            ).strip()
            await self._persist_message(
                session_id,
                "assistant",
                spoken_text or result.video_url,
                latency=latency,
                metadata={"shown_units": result.shown_units} if result.shown_units else None,
                video_url=result.video_url,
            )
            await self.send_message(
                session_id,
                {
                    "type": "video_clip_response",
                    "video_url": result.video_url,
                    # What the clip actually says, so the chat shows the words
                    # alongside the video instead of a bare player.
                    "text": spoken_text,
                    "uncovered_clauses": result.uncovered_clauses,
                    # v2's optional "want to hear more about X?" offer. Chat
                    # text only — the client renders it as a message with
                    # Yes/No; Yes re-asks it through this same handler so the
                    # answer goes through the normal validation/assembly path.
                    "follow_up": result.follow_up,
                },
            )

        except Exception as e:
            logger.error(
                f"Video clip error [{session_id}]: {type(e).__name__}: {e}", exc_info=True
            )
            await self.send_message(
                session_id,
                {"type": "error", "message": f"Video clip assembly failed: {type(e).__name__}: {e}"},
            )

    # ── streaming pipeline ────────────────────────────────────────────────────

    # Only used when a session has no producer_id yet (shouldn't normally
    # happen — sessions.py's create_session already requires a valid
    # avatar — but a missing/deleted avatar owner is a real enough edge case
    # to have a safe, non-generated fallback for). Never LLM-generated.
    _NO_PIPELINE_MSG = (
        "I'm not connected to any recorded stories yet — this avatar's "
        "answer pipeline is still being built."
    )

    async def _response_producer(
        self,
        session_id: str,
        queue: "asyncio.Queue[Optional[str]]",
        text: str,
    ) -> str:
        """
        The retrieval-constrained response pipeline (Prompts 6-9):
        response_assembler.assemble_response() runs retrieval (Prompt 6) +
        relevance scoring (Prompt 7) + bridge-phrase assembly (Prompt 8)
        against the STORYTELLER's archive (session's producer_id, the
        avatar owner — see _load_session_data), then the assembled text
        (verbatim transcripts + fixed bridge phrases only, never generated)
        is chunked into this queue exactly the way a live-streamed LLM
        reply would be, so the TTS/lip-sync consumer downstream
        (`_animate_from_queue`) doesn't need to know the difference.

        Deliberately never calls the general-purpose LLM service for the
        reply itself — this project's rule is that only ingested, confirmed
        story content may ever reach TTS/lip-sync.
        """
        from app.services import response_assembler

        data = self.session_data.get(session_id, {})
        group_id = data.get("producer_id")
        recording_language = data.get("producer_recording_language", "en")

        with span("response.produce"):
            if not group_id:
                full_text = self._NO_PIPELINE_MSG
            else:
                try:
                    full_text = await response_assembler.assemble_response(
                        question=text,
                        group_id=group_id,
                        recording_language=recording_language,
                        session_id=session_id,
                    )
                except Exception as e:
                    logger.error(f"response_assembler failed [{session_id}]: {e}")
                    full_text = response_assembler.NO_STORY_FALLBACK

            if session_id in self.active_connections:
                # The whole reply is already known (retrieval, not live
                # generation) — show it in full immediately rather than
                # faking an incremental reveal of text we already have.
                await self.send_message(session_id, {"type": "token", "token": full_text})

            # Chunk the complete text for TTS/animation using the same
            # first-clause-then-sentences pacing a live-streamed LLM reply
            # goes through: the opening fragment ships at the first CLAUSE
            # boundary once it's long enough (fastest first video frame),
            # everything after that chunks at SENTENCE boundaries.
            buf = full_text
            chunks: list[str] = []
            first_clause_end = next(
                (
                    m.end()
                    for m in _CLAUSE_RE.finditer(buf)
                    if len(buf[: m.end()].strip()) >= _MIN_FIRST_CHUNK_LEN
                ),
                None,
            )
            if first_clause_end is not None:
                chunks.append(buf[:first_clause_end].strip())
                buf = buf[first_clause_end:]

            sentence_chunks, buf = _drain_chunks(buf, _SENTENCE_RE, _MIN_SENTENCE_LEN, _MAX_CHUNK_CHARS)
            chunks.extend(sentence_chunks)

            for chunk in chunks:
                await queue.put(chunk)
            if buf.strip():
                await queue.put(buf.strip())

        await queue.put(None)

        await self.send_message(
            session_id, {"type": "message", "role": "assistant", "content": full_text}
        )
        return full_text

    async def _animate_from_queue(
        self,
        session_id: str,
        queue: "asyncio.Queue[Optional[str]]",
    ) -> None:
        """
        Consume sentences from the queue and run TTS + animation for each,
        streaming video_chunk events to the frontend as they complete.
        """
        data = self.session_data.get(session_id, {})
        avatar_image = data.get("avatar_image_local")
        speaker_wav: Optional[str] = data.get("voice_wav")
        language: str = data.get("language", "en")

        # If no avatar image, drain queue silently
        if not avatar_image:
            logger.warning(f"No avatar image for session {session_id}")
            while True:
                item = await queue.get()
                if item is None:
                    break
            return

        chunk_index: int = 0
        sent_any = False
        # Only warn about TTS fallback once per turn — repeated warnings on
        # every sentence would be noisy. We reset this in the enclosing turn.
        fallback_announced = False
        # Surfaced to the client in the final "failed for all sentences"
        # message if every chunk fails, so the real cause is visible without
        # needing to tail server logs — captures only the FIRST failure
        # (later ones are usually the same root cause repeating).
        first_failure_detail: Optional[str] = None

        await self.send_message(
            session_id,
            {
                "type": "video_chunk_start",
                "total_chunks": -1,  # streaming mode — total unknown up front
            },
        )

        while True:
            sentence = await queue.get()
            if sentence is None:
                break

            if session_id not in self.active_connections:
                break  # client disconnected mid-stream

            job_id = uuid.uuid4().hex[:12]
            session_dir = _private_session_dir(session_id)
            tmp_audio = session_dir / f"{job_id}_audio.wav"
            tmp_video = session_dir / f"{job_id}_video.mp4"

            stage = "setup"
            try:
                await self.send_message(
                    session_id,
                    {
                        "type": "status",
                        "message": "Animating…",
                        "stage": "animation",
                    },
                )

                stage = "tts.synthesize"
                with span(
                    "tts.synthesize",
                    **{"chars": len(sentence), "lang": language, "cloned": bool(speaker_wav)},
                ):
                    synth = await gpu_client.synthesize(
                        text=sentence,
                        output_path=str(tmp_audio),
                        speaker_wav=speaker_wav,
                        language=language,
                    )

                # Notify the client exactly once if Chatterbox bailed and
                # we ended up serving the un-cloned gTTS voice instead.
                if synth.fallback and not fallback_announced:
                    fallback_announced = True
                    await self.send_message(
                        session_id,
                        {
                            "type": "tts_fallback",
                            "engine": synth.engine,
                            "voice_cloned": synth.voice_cloned,
                            "message": (
                                "Cloned voice unavailable — using default voice for this reply."
                                if speaker_wav
                                else f"Voice engine fell back to {synth.engine} for this reply."
                            ),
                        },
                    )

                stage = "avatar.animate"
                with span("avatar.animate", **{"chunk": chunk_index}):
                    await gpu_client.animate(
                        avatar_image_path=avatar_image,
                        audio_path=str(tmp_audio),
                        output_path=str(tmp_video),
                    )

                stage = "storage.upload"
                ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                video_key = f"videos/{session_id}/{ts}_c{chunk_index}.mp4"
                with span("storage.upload", **{"chunk": chunk_index}):
                    await storage_service.upload_file(
                        tmp_video.read_bytes(), video_key, content_type="video/mp4"
                    )
                    # S3 objects are private — hand the client a URL it can
                    # actually fetch (presigned on S3, /uploads/ locally).
                    video_url = await storage_service.serving_url(video_key)

                stage = "send_video_chunk"
                await self.send_message(
                    session_id,
                    {
                        "type": "video_chunk",
                        "chunk_index": chunk_index,
                        "total_chunks": -1,
                        "video_url": video_url,
                        "text": sentence,
                    },
                )
                chunk_index = chunk_index + 1
                sent_any = True
                logger.info(f"Chunk {chunk_index} ready [{session_id}]")

            except Exception as e:
                # exc_info=True so the FULL traceback lands in server logs,
                # not just str(e) — a bare exception message (e.g. an empty
                # string from some subprocess failures) is often useless
                # without the traceback showing exactly where it came from.
                # `stage` pinpoints which of the four steps above failed.
                logger.error(
                    f"Chunk {chunk_index} failed at stage={stage!r} [{session_id}]: "
                    f"{type(e).__name__}: {e}",
                    exc_info=True,
                )
                if first_failure_detail is None:
                    first_failure_detail = f"{stage}: {type(e).__name__}: {e}"

            finally:
                # missing_ok=True only suppresses FileNotFoundError, not a
                # locked file — Windows can briefly hold a handle open on
                # tmp_audio/tmp_video after the ffmpeg subprocess behind
                # gpu_client.synthesize/animate exits (pydub's mp3->wav
                # export and the "simple" animator both shell out to it),
                # raising PermissionError here. An exception raised inside
                # `finally` propagates regardless of the `except` above,
                # which is exactly how this crashed the WHOLE turn
                # (bypassing the per-chunk error handling entirely) instead
                # of just leaving one leftover temp file for the existing
                # daily cleanup task (celery_app.py) to sweep later.
                for tmp_path in (tmp_audio, tmp_video):
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError as cleanup_err:
                        logger.warning(f"Could not delete temp file {tmp_path}: {cleanup_err}")

        await self.send_message(
            session_id,
            {
                "type": "video_chunk_end",
                "sent_chunks": chunk_index,
            },
        )

        if not sent_any:
            detail = f" ({first_failure_detail})" if first_failure_detail else ""
            await self.send_message(
                session_id,
                {"type": "error", "message": f"Avatar animation failed for all sentences.{detail}"},
            )

    # ── helpers ───────────────────────────────────────────────────────────────

    async def set_avatar(self, session_id: str, avatar_id: str):
        if session_id in self.session_data:
            self.session_data[session_id]["avatar_id"] = avatar_id

    async def set_voice_by_id(self, session_id: str, voice_id: str) -> bool:
        """
        Resolve a voice ID to its on-disk WAV and attach it to the session.
        Returns True if the voice was found, owned by the requester, and
        assigned. Accepts voice IDs only — raw filesystem paths are NEVER
        accepted from WebSocket clients (path-disclosure / arbitrary-read).
        """
        if session_id not in self.session_data:
            return False
        entry = await self._get_voice_entry(voice_id)
        if not entry:
            return False
        # Cross-tenant guard: only the voice's owner can attach it to their
        # session. Otherwise user A could guess user B's voice UUID and
        # surreptitiously use their cloned voice.
        session_user = self.session_data[session_id].get("user_id")
        voice_user = entry.get("user_id", "demo-user")
        if session_user and voice_user != session_user:
            logger.warning(
                f"WS set_voice rejected: voice {voice_id} owned by "
                f"{voice_user!r} but session belongs to {session_user!r}"
            )
            return False
        wav = entry.get("wav_path")
        if not wav:
            return False
        self.session_data[session_id]["voice_wav"] = wav
        logger.info(f"Voice set [{session_id}]: voice_id={voice_id}")
        return True

    async def set_language(self, session_id: str, language: str):
        """Set TTS language for the session. Falls back to 'en' on unknown codes."""
        # Match voices.py allowed list
        allowed = {
            "ar",
            "da",
            "de",
            "el",
            "en",
            "es",
            "fi",
            "fr",
            "he",
            "hi",
            "it",
            "ja",
            "ko",
            "ms",
            "nl",
            "no",
            "pl",
            "pt",
            "ru",
            "sv",
            "sw",
            "tr",
            "zh",
        }
        lang = (language or "en").lower()
        if lang not in allowed:
            lang = "en"
        if session_id in self.session_data:
            self.session_data[session_id]["language"] = lang
            logger.info(f"Language set [{session_id}]: {lang}")

    # ── stale session cleanup ─────────────────────────────────────────────────

    async def cleanup_stale(self) -> int:
        """
        Reap sessions whose websocket is gone or that have been idle too long.

        We snapshot the candidate list under a lock, then drop the lock while
        calling `disconnect()` for each (disconnect involves async file I/O
        and shouldn't be serialized). The lock prevents the snapshot from
        racing with a fresh `connect()` for the same session id — without it,
        the cleanup loop could observe a half-built session and rip it down
        right after the new connection finished setting up.
        """
        now = datetime.now(timezone.utc)
        async with self._mutation_lock:
            stale: list[str] = []
            for sid, data in self.session_data.items():
                last = data.get("last_activity") or data.get("connected_at") or now
                if sid not in self.active_connections:
                    stale.append(sid)
                    continue
                if (now - last).total_seconds() > STALE_SESSION_TTL_SECS:
                    stale.append(sid)
        for sid in stale:
            await self.disconnect(sid)
        return len(stale)

    def start_cleanup_task(self) -> None:
        if self._cleanup_task is not None:
            return

        async def _loop():
            while True:
                try:
                    await asyncio.sleep(300)
                    reaped = await self.cleanup_stale()
                    if reaped:
                        logger.info(f"Reaped {reaped} stale WS session(s)")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"WS cleanup task error: {e}")

        self._cleanup_task = asyncio.create_task(_loop(), name="ws-cleanup")

    async def stop_cleanup_task(self) -> None:
        if self._cleanup_task is None:
            return
        self._cleanup_task.cancel()
        try:
            await self._cleanup_task
        except (asyncio.CancelledError, Exception):
            pass
        self._cleanup_task = None


websocket_manager = ConnectionManager()
