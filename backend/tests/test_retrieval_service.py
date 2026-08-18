"""
Tests for retrieval_service.py — now only the conversation-history helpers
that survived the 2026-08-19 step-5 retirement (docs/
AVATAR_SHARED_ENGINE_PLAN.md §5): `_recent_turns`,
`_render_turn_for_history`, `COREFERENCE_HISTORY_TURNS`,
`_parse_json_array`. The multi-step retrieval pipeline these once sat
beside (topic classification, primary_match, expand_graph, the LLM
coreference call) was deleted with its tests; the shared engine's own
suite covers those jobs now.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Avatar, Message
from app.models import Session as SessionModel
from app.services import retrieval_service as rsvc


@pytest.fixture
async def retrieval_session_factory(test_engine, monkeypatch):
    """Retarget retrieval_service's DB access at the same SQLite engine the
    `client`/`db_session` fixtures use (it opens its own sessions via the
    module-level AsyncSessionLocal, bypassing FastAPI's DI)."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(rsvc, "AsyncSessionLocal", factory)
    return factory


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


# ── _recent_turns ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recent_turns_returns_empty_for_session_with_no_messages(retrieval_session_factory):
    turns = await rsvc._recent_turns("no-such-session", rsvc.COREFERENCE_HISTORY_TURNS)
    assert turns == []


@pytest.mark.asyncio
async def test_recent_turns_returns_last_n_in_chronological_order(chat_session_with_messages):
    add_message = chat_session_with_messages["add_message"]
    await add_message("user", "who is Gila?", 0)
    await add_message("assistant", "Gila was my neighbor growing up.", 1)
    await add_message("user", "did you love her?", 2)

    turns = await rsvc._recent_turns(chat_session_with_messages["session_id"], 2)

    assert [t["content"] for t in turns] == ["Gila was my neighbor growing up.", "did you love her?"]
    assert [t["role"] for t in turns] == ["assistant", "user"]


@pytest.mark.asyncio
async def test_recent_turns_respects_limit_even_with_more_history(chat_session_with_messages):
    add_message = chat_session_with_messages["add_message"]
    for i in range(5):
        await add_message("user", f"question {i}", i)

    turns = await rsvc._recent_turns(chat_session_with_messages["session_id"], 2)
    assert [t["content"] for t in turns] == ["question 3", "question 4"]


# ── _render_turn_for_history ─────────────────────────────────────────────────


def test_render_turn_for_history_passes_through_plain_text():
    assert rsvc._render_turn_for_history("user", "who is Gila?") == "user: who is Gila?"


def test_render_turn_for_history_masks_video_clip_url():
    """video_clip_assembler persists a raw video URL as the assistant's
    Message.content — must never be fed to an LLM prompt as if it were
    narration."""
    rendered = rsvc._render_turn_for_history(
        "assistant", "http://localhost:8000/uploads/video-clips/abc123.mp4"
    )
    assert rendered == "assistant: (showed a video clip)"


# ── _parse_json_array ────────────────────────────────────────────────────────


def test_parse_json_array_extracts_and_strips_items():
    assert rsvc._parse_json_array('noise ["גילה", " אמנון "] noise') == ["גילה", "אמנון"]


def test_parse_json_array_returns_empty_on_garbage():
    assert rsvc._parse_json_array("no array here") == []
    assert rsvc._parse_json_array('{"not": "a list"}') == []
