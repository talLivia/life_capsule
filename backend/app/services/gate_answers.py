"""
Reading and writing interview gate answers.

Step 3 of docs/INTERVIEW_RESTRUCTURE.md.

`interview_gate_answers.value` deliberately carries no FK or CHECK — a gate's
options live in interview_questions.json, and duplicating that vocabulary into
the database would mean a migration every time a screening question gains an
option. This module is where that constraint actually lives instead, so every
write goes through one validation against the same file the questions come
from. A caller reaching past it and inserting directly is the bug this module
exists to prevent.
"""

from __future__ import annotations

import logging
from typing import Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import interview_config
from app.models import InterviewGateAnswer

logger = logging.getLogger(__name__)


class InvalidGateAnswer(ValueError):
    """A gate id that is not a gate, or a value that gate does not offer.

    Raised rather than logged-and-swallowed: unlike a missing entity summary,
    a bad gate answer changes which questions a producer is asked, and
    silently dropping it would leave the flow stuck on a step whose answer
    appeared to have been recorded.
    """


async def get_answers(db: AsyncSession, interview_session_id: str) -> Dict[str, str]:
    """Every gate answer for a session, as `{gate_id: value}`.

    One query and one dict: this is what `resolve_steps` takes, and the
    accordion resolves EVERY category against the same set, so fetching per
    category would be a round trip per category for no benefit.
    """
    rows = (
        await db.execute(
            select(InterviewGateAnswer).where(
                InterviewGateAnswer.interview_session_id == interview_session_id
            )
        )
    ).scalars().all()
    return {row.gate_id: row.value for row in rows}


async def set_answer(
    db: AsyncSession, interview_session_id: str, gate_id: str, value: str
) -> InterviewGateAnswer:
    """Record (or change) one gate answer. Upsert on (session, gate).

    Changing an existing answer is allowed and does nothing else: recordings
    already made under the previous branch are NOT deleted, per
    INTERVIEW_RESTRUCTURE §8.3. Footage is never destroyed by a navigation
    action, and the case is rare enough that guessing at cleanup semantics
    would be worse than leaving it visible.
    """
    if not interview_config.is_valid_gate_id(gate_id):
        raise InvalidGateAnswer(f"{gate_id!r} is not a gate in the question set")

    allowed = interview_config.gate_option_values(gate_id)
    if value not in allowed:
        # Names the options rather than just rejecting, so a client bug is
        # diagnosable from the response.
        raise InvalidGateAnswer(
            f"{value!r} is not an option for {gate_id!r} (expected one of {allowed})"
        )

    existing = (
        await db.execute(
            select(InterviewGateAnswer).where(
                InterviewGateAnswer.interview_session_id == interview_session_id,
                InterviewGateAnswer.gate_id == gate_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.value != value:
            logger.info(
                f"Gate {gate_id} changed {existing.value!r} -> {value!r} "
                f"for session {interview_session_id}; recordings under the "
                f"previous branch are kept"
            )
        existing.value = value
        return existing

    answer = InterviewGateAnswer(
        interview_session_id=interview_session_id, gate_id=gate_id, value=value
    )
    db.add(answer)
    await db.flush()
    return answer


async def clear_answer(
    db: AsyncSession, interview_session_id: str, gate_id: str
) -> bool:
    """Forget a gate answer, returning it to unanswered.

    Not currently reachable from the UI — the flow only ever sets answers —
    but it is the honest inverse of `set_answer` and the natural way to reset
    a session for testing. Returns whether a row was removed.
    """
    existing = (
        await db.execute(
            select(InterviewGateAnswer).where(
                InterviewGateAnswer.interview_session_id == interview_session_id,
                InterviewGateAnswer.gate_id == gate_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return False
    await db.delete(existing)
    return True


async def unresolvable_answers(
    db: AsyncSession, interview_session_id: str
) -> List[str]:
    """Stored answers whose gate or value no longer exists in the question set.

    The question set is editable and sessions outlive edits, so this is a real
    state rather than a defensive one. `resolve_steps` already treats these as
    unanswered — the producer is simply asked again — so this is for reporting,
    not repair: nothing is deleted on the strength of a file having changed.
    """
    stale = []
    for gate_id, value in (await get_answers(db, interview_session_id)).items():
        if not interview_config.is_valid_gate_id(gate_id):
            stale.append(gate_id)
        elif value not in interview_config.gate_option_values(gate_id):
            stale.append(gate_id)
    return stale
