"""
End-to-end WebSocket turn through the real ASGI `/ws/session/{id}` endpoint.

This covers the one seam the ConnectionManager unit tests skip: the actual
`websocket_endpoint` receive loop + auth handshake + message dispatch, wired
through Starlette's TestClient. The DB is a file-backed SQLite the portal
thread can reopen (NullPool → a fresh connection per op, no event-loop
binding), and the heavy services (LLM/TTS/animation/storage/image) are
stubbed so a full turn runs without GPU or network.
"""

import tempfile

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

import main
from app.api.v1.users import create_access_token, get_password_hash
from app.database import Base
from app.models import Avatar, Session, User
from app.services.tts import SynthResult


def _make_url() -> str:
    return f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}"


async def _seed(url: str) -> str:
    """Create schema + a ready avatar/session owned by user 'u1'. Returns session id."""
    eng = create_async_engine(url, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with sm() as db:
        db.add(
            User(
                id="u1",
                email="u1@example.com",
                username="u1",
                hashed_password=get_password_hash("pw12345678"),
            )
        )
        avatar = Avatar(
            id="11111111-1111-1111-1111-111111111111",
            user_id="u1",
            name="A",
            image_url="http://x/i.jpg",
            s3_key="avatars/a/image.jpg",
            status="ready",
        )
        db.add(avatar)
        await db.flush()
        session = Session(id="sess-e2e", user_id="u1", avatar_id=avatar.id, status="active")
        db.add(session)
        await db.commit()
    await eng.dispose()
    return "sess-e2e"


@pytest.mark.asyncio
async def test_ws_full_turn_streams_events(monkeypatch):
    url = _make_url()
    session_id = await _seed(url)

    # Point the app's DB sessionmaker at the seeded file DB.
    sm = async_sessionmaker(
        create_async_engine(url, poolclass=NullPool), class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr("app.database.AsyncSessionLocal", sm)
    monkeypatch.setattr(main, "AsyncSessionLocal", sm)

    from app import websocket as wsmod

    # Stub the heavy pipeline so a turn completes instantly, deterministically.
    # `_response_producer` now calls response_assembler.assemble_response()
    # (Prompts 6-9's retrieval + relevance scoring + bridge-phrase assembly
    # against the storyteller's own archive) — mocked here at that single
    # entry point, since its own internals (topic classification, graph
    # traversal, scoring) are covered by test_response_assembler.py and
    # friends, not this WS-plumbing test.
    TEST_REPLY = "This is the assembled, retrieval-based reply."

    async def fake_resolve_image(avatar):
        return "/tmp/fake-avatar.jpg"

    async def fake_tts(text, output_path, speaker_wav=None, language="en"):
        from pathlib import Path

        Path(output_path).write_bytes(b"wav")
        return SynthResult(
            output_path=output_path, engine="chatterbox", fallback=False, voice_cloned=False
        )

    async def fake_animate(avatar_image_path, audio_path, output_path):
        from pathlib import Path

        Path(output_path).write_bytes(b"mp4")

    async def fake_upload(data, key, content_type="video/mp4", metadata=None):
        return f"http://test/{key}"

    async def fake_assemble_response(question, group_id, recording_language, session_id):
        return TEST_REPLY

    # Poison pill: if a text turn ever reaches the LLM service DIRECTLY (not
    # through response_assembler, which is mocked above) — a regression
    # reintroducing free-form chat — fail loudly instead of silently
    # passing. This is the actual safety property Prompt 1 exists to
    # guarantee: only response_assembler's constrained, retrieval-based
    # text may ever reach TTS/lip-sync.
    def _fail_if_llm_called(*args, **kwargs):
        raise AssertionError(
            "llm_service was invoked directly during a text turn, bypassing "
            "response_assembler.py — the free-form chat path must stay "
            "disconnected."
        )

    from app.services.llm import llm_service

    monkeypatch.setattr(llm_service, "generate_response", _fail_if_llm_called)
    monkeypatch.setattr(llm_service, "stream_response", _fail_if_llm_called)

    from app.services import response_assembler

    monkeypatch.setattr(response_assembler, "assemble_response", fake_assemble_response)

    # _animate_from_queue calls gpu_client.synthesize()/.animate(), which
    # (with GPU_SERVICE_URL unset, the default) call these exact shared
    # singletons — patching them here affects those calls too.
    from app.services.animator import avatar_animator
    from app.services.tts import tts_service

    monkeypatch.setattr(wsmod.websocket_manager, "_resolve_local_image", fake_resolve_image)
    monkeypatch.setattr(tts_service, "synthesize", fake_tts)
    monkeypatch.setattr(avatar_animator, "animate", fake_animate)
    monkeypatch.setattr(wsmod.storage_service, "upload_file", fake_upload)

    token = create_access_token(data={"sub": "u1"})

    # TestClient drives the ASGI app (lifespan + portal) on a worker thread.
    with TestClient(main.app) as tc:
        with tc.websocket_connect(f"/ws/session/{session_id}?token={token}") as ws:
            ws.send_json({"type": "text", "text": "hi"})

            types: list[str] = []
            messages: list[dict] = []
            # Collect until the turn ends or we hit a safety cap.
            for _ in range(60):
                msg = ws.receive_json()
                types.append(msg["type"])
                messages.append(msg)
                if msg["type"] == "video_chunk_end":
                    break

    # The full pipeline emitted the expected event sequence.
    assert "token" in types  # full reply text sent for live UI display
    assert "message" in types  # assembled assistant message
    assert "video_chunk" in types  # at least one lip-sync chunk
    assert "video_chunk_end" in types  # stream terminated cleanly

    # The reply is exactly what response_assembler.assemble_response()
    # returned — nothing else reached TTS/lip-sync.
    final = next(m for m in messages if m["type"] == "message")
    assert final["content"] == TEST_REPLY


@pytest.mark.asyncio
async def test_ws_rejects_bad_token(monkeypatch):
    url = _make_url()
    session_id = await _seed(url)
    sm = async_sessionmaker(
        create_async_engine(url, poolclass=NullPool), class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr("app.database.AsyncSessionLocal", sm)
    monkeypatch.setattr(main, "AsyncSessionLocal", sm)
    monkeypatch.setattr(main.settings, "DEBUG", False)

    from starlette.websockets import WebSocketDisconnect

    with TestClient(main.app) as tc:
        # A garbage token must be rejected at the handshake (close 4401).
        with pytest.raises(WebSocketDisconnect) as exc:
            with tc.websocket_connect(f"/ws/session/{session_id}?token=garbage") as ws:
                ws.receive_json()
        assert exc.value.code == 4401
