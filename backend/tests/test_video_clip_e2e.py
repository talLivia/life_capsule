"""
End-to-end WebSocket turn for the video-clip chat mode (video_clips_v2),
mirroring test_ws_e2e.py's conventions: a file-backed SQLite DB (NullPool,
reopenable from the TestClient's portal thread), the real ASGI
`/ws/session/{id}` endpoint, and only the already-independently-tested
internals stubbed — here that's full_archive_retrieval.select_units (the
unit selection has its own extensive suite in test_full_archive_retrieval).
ffmpeg trim/upload run FOR REAL against a real synthetic multi-topic
recording, so this test actually proves the WS routing + v2 orchestration +
assembly pipeline produces a clip whose time range plausibly matches the
selected units — not just that the right mocks were called.

(Rewritten from the v1 chunk-pipeline e2e when the video_clips mode was
removed — see docs/V1_REMOVAL_PLAN.md. The proof this file carries — a WS
question becomes a genuinely trimmed clip — is mode-independent and kept.)
"""

import subprocess
import tempfile

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

import main
from app.api.v1.users import create_access_token, get_password_hash
from app.database import Base
from app.models import Avatar, InterviewSession, RawSegment, Session, TranscriptChunk, User

pytestmark = pytest.mark.asyncio


def _make_url() -> str:
    return f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}"


def _word_timestamps(text: str, start_sec: float) -> list:
    words = text.split(" ")
    out = []
    t = start_sec
    for w in words:
        out.append({"word": w, "start_sec": round(t, 3), "end_sec": round(t + 0.4, 3)})
        t += 0.4
    return out


async def _seed(url: str, video_key: str) -> str:
    """A producer with one long, multi-topic recording (career / Paris /
    grandchildren, 10s each) and a linked family viewer. Returns the
    family viewer's WS session id."""
    eng = create_async_engine(url, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with sm() as db:
        producer = User(
            id="producer-1",
            email="producer@example.com",
            username="producer1",
            hashed_password=get_password_hash("pw12345678"),
            role="producer",
            chat_mode="video_clips_v2",
        )
        family = User(
            id="family-1",
            email="family@example.com",
            username="family1",
            hashed_password=get_password_hash("pw12345678"),
            role="family",
            producer_id="producer-1",
        )
        db.add_all([producer, family])

        avatar = Avatar(
            id="avatar-1",
            user_id="producer-1",
            name="A",
            image_url="http://x/i.jpg",
            s3_key="avatars/a/image.jpg",
            status="ready",
        )
        db.add(avatar)

        interview = InterviewSession(id="interview-1", user_id="producer-1", status="active")
        db.add(interview)
        await db.flush()

        segment = RawSegment(
            id="segment-1",
            interview_session_id="interview-1",
            question_asked="Tell me about your life",
            question_index=0,
            transcript="career story, then Paris story, then grandchildren story",
            video_key=video_key,
            importance_score=5.0,
            status="ready",
        )
        db.add(segment)
        await db.flush()

        career_text = "I spent thirty years working as an engineer at a factory"
        paris_text = "The year I lived in Paris was the happiest time of my life"
        grandkids_text = "My grandchildren visit every summer and we bake together"

        db.add_all(
            [
                TranscriptChunk(
                    raw_segment_id="segment-1",
                    start_sec=0.0,
                    end_sec=10.0,
                    text=career_text,
                    sequence_index=0,
                    word_timestamps=_word_timestamps(career_text, 0.0),
                    topic_tags=["career"],
                ),
                TranscriptChunk(
                    raw_segment_id="segment-1",
                    start_sec=10.0,
                    end_sec=20.0,
                    text=paris_text,
                    sequence_index=1,
                    word_timestamps=_word_timestamps(paris_text, 10.0),
                    topic_tags=["travel"],
                ),
                TranscriptChunk(
                    raw_segment_id="segment-1",
                    start_sec=20.0,
                    end_sec=30.0,
                    text=grandkids_text,
                    sequence_index=2,
                    word_timestamps=_word_timestamps(grandkids_text, 20.0),
                    topic_tags=["family"],
                ),
            ]
        )

        session = Session(
            id="sess-video-clip-e2e",
            user_id="family-1",
            producer_id="producer-1",
            avatar_id="avatar-1",
            status="active",
        )
        db.add(session)
        await db.commit()
    await eng.dispose()
    return "sess-video-clip-e2e"


def _patch_environment(monkeypatch, sm, tmp_path):
    """Every module-level AsyncSessionLocal in the turn's path pinned to the
    test DB, storage pointed at tmp_path, and the avatar-image preview
    stubbed (a real download failure there would raise inside
    _load_session_data before the producer_id lookup, masking every
    assertion behind a false no-story)."""
    monkeypatch.setattr("app.database.AsyncSessionLocal", sm)
    monkeypatch.setattr(main, "AsyncSessionLocal", sm)

    from app.services import full_archive_retrieval, retrieval_service, video_clip_assembler

    monkeypatch.setattr(retrieval_service, "AsyncSessionLocal", sm)
    monkeypatch.setattr(video_clip_assembler, "AsyncSessionLocal", sm)
    monkeypatch.setattr(full_archive_retrieval, "AsyncSessionLocal", sm)

    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "LOCAL_STORAGE_PATH", str(tmp_path))

    from app import websocket as wsmod

    async def fake_resolve_image(avatar):
        return str(tmp_path / "fake-avatar.jpg")

    monkeypatch.setattr(wsmod.websocket_manager, "_resolve_local_image", fake_resolve_image)


async def test_ws_video_clip_question_returns_plausible_clip(monkeypatch, tmp_path):
    # A real 30s synthetic recording — three 10s "topics" back to back — so
    # ffmpeg trim/upload run for real, not mocked.
    video_key = "segments/producer-1/interview-1/0/sample.mp4"
    video_path = tmp_path / video_key
    video_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=30",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "30", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
            "-shortest", str(video_path),
        ],
        check=True,
    )

    url = _make_url()
    session_id = await _seed(url, video_key)

    sm = async_sessionmaker(
        create_async_engine(url, poolclass=NullPool), class_=AsyncSession, expire_on_commit=False
    )
    _patch_environment(monkeypatch, sm, tmp_path)

    # Bypass the archive-read selection — it has its own extensive suite.
    # Here it "chose" the Paris span (10s-20s of the recording), and the
    # REAL assembly downstream must produce a clip of that span, not of the
    # whole 30s recording.
    from app.services import full_archive_retrieval
    from app.services.video_clip_assembler import ExpandedClip

    async def fake_select_units(question, group_id, recording_language, session_id):
        return full_archive_retrieval.UnitSelection(
            clips=[
                ExpandedClip(
                    raw_segment_id="segment-1",
                    start_sec=10.0,
                    end_sec=20.0,
                    source_chunk_id="stubbed",
                )
            ],
            selected_units=[],
        )

    monkeypatch.setattr(full_archive_retrieval, "select_units", fake_select_units)

    token = create_access_token(data={"sub": "family-1"})

    with TestClient(main.app) as tc:
        with tc.websocket_connect(f"/ws/session/{session_id}?token={token}") as ws:
            ws.send_json({"type": "video_clip_question", "text": "tell me about the time you lived in Paris"})

            types: list[str] = []
            messages: list[dict] = []
            for _ in range(30):
                msg = ws.receive_json()
                types.append(msg["type"])
                messages.append(msg)
                if msg["type"] in ("video_clip_response", "video_clip_no_story", "error"):
                    break

    assert "status" in types
    assert "video_clip_response" in types, f"got {types}, messages={messages}"

    final = next(m for m in messages if m["type"] == "video_clip_response")
    assert final["video_url"]
    # The Phase 8 lookup ran for real against the seeded segment: no
    # question_id means no category, never a guess.
    assert final["photo_categories"] == []

    # The clip must plausibly contain "the right moment" (the Paris span,
    # 10s-20s) and NOT the whole 30s recording.
    served_key = final["video_url"].split("/uploads/", 1)[1]
    clip_path = tmp_path / served_key
    assert clip_path.exists()

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(clip_path),
        ],
        capture_output=True, text=True, check=True,
    )
    duration = float(probe.stdout.strip())
    assert 5.0 < duration < 15.0, f"expected ~10s Paris-only clip, got {duration}s"


async def test_ws_video_clip_no_story_when_nothing_is_selected(monkeypatch, tmp_path):
    """An empty selection -> the no-story signal, no video assembled at all."""
    video_key = "segments/producer-1/interview-1/0/sample.mp4"
    video_path = tmp_path / video_key
    video_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=5",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", str(video_path),
        ],
        check=True,
    )

    url = _make_url()
    session_id = await _seed(url, video_key)

    sm = async_sessionmaker(
        create_async_engine(url, poolclass=NullPool), class_=AsyncSession, expire_on_commit=False
    )
    _patch_environment(monkeypatch, sm, tmp_path)

    from app.services import full_archive_retrieval

    async def fake_select_units(question, group_id, recording_language, session_id):
        return full_archive_retrieval.UnitSelection(clips=[], selected_units=[])

    monkeypatch.setattr(full_archive_retrieval, "select_units", fake_select_units)

    token = create_access_token(data={"sub": "family-1"})

    with TestClient(main.app) as tc:
        with tc.websocket_connect(f"/ws/session/{session_id}?token={token}") as ws:
            ws.send_json({"type": "video_clip_question", "text": "what was your favorite vacation to the moon"})

            types: list[str] = []
            messages: list[dict] = []
            for _ in range(30):
                msg = ws.receive_json()
                types.append(msg["type"])
                messages.append(msg)
                if msg["type"] in ("video_clip_response", "video_clip_no_story", "error"):
                    break

    assert "video_clip_no_story" in types, f"got {types}"
    final = next(m for m in messages if m["type"] == "video_clip_no_story")
    assert final["message"]
