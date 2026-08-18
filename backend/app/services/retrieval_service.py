"""
Conversation-history helpers shared by the retrieval engine.

⚠️ HISTORY: this module WAS the avatar mode's multi-step retrieval
pipeline (Prompt 6: topic classification → primary_match → expand_graph,
plus an LLM coreference-rewrite pass). All of that was retired on
2026-08-19 per docs/AVATAR_SHARED_ENGINE_PLAN.md §5 — the shared engine
(full_archive_retrieval.select_units) does those jobs inside its single
whole-archive read, for BOTH modes, and its history block replaced the
separate coreference call. See the `pre-step5-retirement` history of this
file for the deleted implementation.

What remains is exactly the §5 KEEP list, names frozen:

  * `_recent_turns` / `COREFERENCE_HISTORY_TURNS` — the engine's
    coreference-via-history window (full_archive_retrieval.py imports
    both; eval scripts monkeypatch `_recent_turns` by name).
  * `_render_turn_for_history` — history rows can hold a raw clip URL as
    content; the engine's prompt must see a placeholder, not a URL.
  * `_parse_json_array` — general JSON-array-from-LLM-text parsing,
    still generally useful.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import Message

logger = logging.getLogger(__name__)

# How many of the most recent Message rows (both roles) the engine's
# history block looks at — the last user+assistant pair. Recency, not
# breadth, is what resolving a reference needs; websocket.py's much larger
# MAX_CONTEXT_MESSAGES window exists for UI continuity, a different job.
COREFERENCE_HISTORY_TURNS = 2


def _parse_json_array(text: str) -> List[str]:
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


def _render_turn_for_history(role: str, content: str) -> str:
    """video_clip_assembler's assistant turns persist a raw video URL as
    `content` (there's no text caption of what the clip actually said) —
    feeding that to an LLM prompt as if it were narration would be
    actively misleading rather than merely unhelpful. Rendered as a
    neutral placeholder instead; the antecedent we actually need ("Gila")
    almost always comes from the family member's OWN prior question anyway,
    not the assistant's reply."""
    if content.startswith("http://") or content.startswith("https://"):
        return f"{role}: (showed a video clip)"
    return f"{role}: {content}"


async def _recent_turns(session_id: str, limit: int) -> List[Dict[str, str]]:
    """Last `limit` Message rows for this session (both roles), oldest
    first — the same rehydration query websocket.py's _load_session_data
    already runs for UI continuity on reconnect, just windowed much
    smaller. Works identically for both the avatar and video-clip paths,
    since both persist every turn via ConnectionManager._persist_message."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Message.role, Message.content)
            .where(Message.session_id == session_id)
            .where(Message.role.in_(("user", "assistant")))
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        rows = list(result.all())[::-1]
    return [{"role": row.role, "content": row.content} for row in rows]
