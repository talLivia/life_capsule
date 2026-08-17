"""
The SPOKEN renderer for the shared answer engine (avatar mode).

docs/AVATAR_SHARED_ENGINE_PLAN.md: one engine, two renderers. The engine is
full_archive_retrieval.select_units — retrieval, coreference-via-history,
disambiguation, unit selection, the never-invent guarantee — and this module
is the avatar-mode counterpart of assemble_video_clip_response_v2: it takes
the SAME UnitSelection and renders narrated text instead of a video. The
text is the selected units' VERBATIM content, with fixed bridge phrases
between non-contiguous runs so a spoken answer doesn't jump-cut; it then
goes to TTS → MuseTalk unchanged (websocket._response_producer).

NEVER-INVENT, structurally: the output text is exactly (unit texts) +
(phrases from the banks below) + at most one engine-validated follow-up
question, nothing else. The banks follow the two precedents in
response_assembler (BRIDGE_PHRASE_TEMPLATES, NO_MORE_STORY_ABOUT_TEMPLATES):
the phrase never varies with what happened in any recording, and the only
injected value is an entity name the archive really holds, fetched from
entity_store — never model output. Phrase choice is a stable function of
position, so identical selections always speak identical words. The ONE
scoped exception is the spoken follow-up offer (see the note above
FOLLOW_UP_DECLINE_ACK): by producer decision it speaks the engine's
generated question verbatim — meta-conversation, never story content. A
constrained-LLM bridge upgrade is a recorded follow-up (plan §3),
deliberately not built first.

Branch ordering mirrors the video renderer exactly: read-failed before
clarify before no-story before an answer — an outage is not an answer, and
a clarification replaces one.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from app.database import AsyncSessionLocal
from app.services import entity_store
from app.services.full_archive_retrieval import (
    UnitSelection,
    UtteranceUnit,
    _group_selected_runs,
    _unit_key,
)
from app.services.response_assembler import (
    NO_STORY_FALLBACK,
    TRANSIENT_FAILURE_FALLBACK,
)

logger = logging.getLogger(__name__)

# Between two runs from the SAME recording — the speaker is continuing their
# own story past a skipped stretch.
SAME_RECORDING_BRIDGES = [
    "ועוד משהו מאותו סיפור...",
    "ובהמשך לזה...",
    "וגם...",
]

# Between runs from DIFFERENT recordings, when the archive can name what the
# next stretch is about. {entity} is validated against entity_store before
# injection — the same rule as every template bank in this codebase.
CROSS_RECORDING_BRIDGES_ENTITY = [
    "אגב, יש לי עוד סיפור על {entity}...",
    "וזה מזכיר לי גם את {entity}...",
    "ויש עוד משהו שקשור ל{entity}...",
]

# Different recordings, nothing validated to name — still a real transition,
# just an unnamed one. Never a placeholder.
CROSS_RECORDING_BRIDGES_GENERIC = [
    "ויש לי עוד סיפור שקשור לזה...",
    "אגב, זה מזכיר לי עוד משהו...",
    "ויש עוד סיפור אחד...",
]

# Spoken when the engine asks WHICH person was meant. Fixed template with
# nothing injected: the model's own clarify question is generated prose and
# generated prose never reaches TTS. The OPTIONS go out as chat text (the
# WS layer's job), where generated text is allowed — same rule v2 applies.
CLARIFY_SPOKEN_LINE = "יש לי סיפורים על כמה אנשים בשם הזה — למי התכוונת?"

# The follow-up OFFER is spoken using the engine's own generated question,
# verbatim — the same text the chat card shows (DECIDED by the producer,
# 2026-08-17, replacing a fixed generic offer line). This is a deliberate,
# narrowly-scoped exception to "generated prose never reaches TTS": it
# applies ONLY to the offer question, which is meta-conversation about the
# archive rather than story content, and only after _validate_follow_up has
# tied it to real, un-shown units. Story text itself remains verbatim-only.

# Spoken when the listener declines the offer by VOICE. A click dismisses
# silently (v2 parity — the click is its own feedback), but a spoken "לא"
# answered by dead air is a broken voice interaction.
FOLLOW_UP_DECLINE_ACK = "בסדר."


@dataclass
class SpokenAnswer:
    """What the avatar should say, plus the bookkeeping the WS layer persists
    — the spoken twin of VideoClipResult, field for field where they share
    meaning (shown_units carries the same shape the v2 handler persists, so
    both modes feed the same history/already-shown machinery)."""

    text: str
    shown_units: List[dict] = field(default_factory=list)
    follow_up: Optional[dict] = None
    clarify: Optional[dict] = None
    no_story: bool = False
    read_failed: bool = False


def _bridge_for(
    boundary_index: int,
    prev_run: List[UtteranceUnit],
    next_run: List[UtteranceUnit],
    names_by_segment: Dict[str, Set[str]],
) -> str:
    """The fixed phrase spoken between two non-contiguous runs.

    Same recording → continuation bank. Different recording → the entity
    bank when the TARGET segment has a validated name (deterministically the
    alphabetically first, so repeats are word-identical), else the generic
    bank. `boundary_index` rotates within whichever bank fires — stable, so
    nothing about the wording depends on runtime state."""
    if prev_run[-1].segment_id == next_run[0].segment_id:
        bank = SAME_RECORDING_BRIDGES
        return bank[boundary_index % len(bank)]

    names = names_by_segment.get(next_run[0].segment_id) or set()
    if names:
        bank = CROSS_RECORDING_BRIDGES_ENTITY
        return bank[boundary_index % len(bank)].format(entity=min(names))
    bank = CROSS_RECORDING_BRIDGES_GENERIC
    return bank[boundary_index % len(bank)]


async def _entity_names_by_segment(
    segment_ids: List[str], group_id: str
) -> Dict[str, Set[str]]:
    """Bulk entity names for bridge wording. Fail-soft on purpose: these
    names only pick WHICH fixed phrase is spoken, never which units are —
    losing them costs a generic bridge, not an answer."""
    if not segment_ids:
        return {}
    try:
        async with AsyncSessionLocal() as db:
            by_segment = await entity_store.get_entity_names_for_segments(
                db, segment_ids, group_id
            )
    except Exception as e:
        logger.warning(f"Bridge entity lookup failed for {len(segment_ids)} segments: {e}")
        return {}
    return {sid: set(names) for sid, names in by_segment.items()}


async def render_spoken_answer(
    selection: UnitSelection, group_id: str
) -> SpokenAnswer:
    """UnitSelection → what the avatar says. The engine decided WHAT; this
    only decides how it reads aloud."""
    if selection.read_failed:
        # An outage is not an answer — never presented as "no story".
        return SpokenAnswer(text=TRANSIENT_FAILURE_FALLBACK, read_failed=True)

    if selection.clarify:
        # A clarification REPLACES the answer (engine guarantees clips are
        # empty here). Spoken as a fixed line; the options travel as chat
        # text alongside.
        return SpokenAnswer(text=CLARIFY_SPOKEN_LINE, clarify=selection.clarify)

    if not selection.selected_units:
        no_story_line = selection.no_story_text or NO_STORY_FALLBACK
        return SpokenAnswer(
            text=(
                f"{no_story_line} {selection.follow_up['question']}"
                if selection.follow_up
                else no_story_line
            ),
            no_story=True,
            # The offer survives an empty answer — same rule as the video
            # renderer, same reason.
            follow_up=selection.follow_up,
        )

    runs = _group_selected_runs(selection.selected_units)

    # Names only matter where a bridge might name something: the head
    # segments of every run after the first.
    bridge_targets = list({run[0].segment_id for run in runs[1:]})
    names_by_segment = await _entity_names_by_segment(bridge_targets, group_id)

    parts: List[str] = []
    for i, run in enumerate(runs):
        if i:
            parts.append(_bridge_for(i - 1, runs[i - 1], run, names_by_segment))
        parts.append(" ".join(u.text for u in run))
    if selection.follow_up:
        # The offer rides the SAME utterance as the answer, so it plays
        # inside the same mic-suppression window and the mic reopens right
        # after it — the sequencing that makes a spoken yes/no answerable.
        # The spoken words are the generated question itself, matching the
        # chat card exactly (see the scoped exception noted above).
        parts.append(selection.follow_up["question"])

    return SpokenAnswer(
        text=" ".join(parts),
        follow_up=selection.follow_up,
        # Identical shape to the video renderer's payload, so both modes
        # feed the same shown-units history machinery.
        shown_units=[
            {
                "key": _unit_key(u.segment_id, u.start_sec),
                "unit_id": u.unit_id,
                "text": u.text,
            }
            for u in selection.selected_units
        ],
    )
