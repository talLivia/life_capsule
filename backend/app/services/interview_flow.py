"""
Where the producer is in the interview, and what they can reach.

Step 4 of docs/INTERVIEW_RESTRUCTURE.md. This is the single place that answers
"where am I, what is done, what is open" — the accordion renders it and does
not recompute any of it.

## The position is DERIVED, never stored

`InterviewSession.current_question_index` is a stored cursor the client sets,
and stored cursors drift: edit the question set and it points somewhere else,
lose a write and it points backwards, and nothing detects either. It also
cannot address a step inside a branch.

Position here is instead computed from three things that are already true —
the question set, the gate answers, and which questions have recordings. The
current step is simply the first reachable step that is not yet done. There is
no cursor to get out of sync, resume-after-refresh is correct by construction,
and a question-set edit relocates the producer honestly rather than stranding
them.

`current_question_index` is left alone: the pre-accordion `/record` screen
still uses it, and it stays meaningful as "which question the old flow was
on". Nothing here reads or writes it.

## Progress, and what is honestly knowable

A category's total is only known once no reachable gate is still unanswered
(`category_is_settled`). Before that the count genuinely depends on an answer
the producer has not given — the relationships category is 5 steps, or 14, or
17 — so `total` is None and the UI must not invent one. See §8.4.

Counts include gate steps, because the producer experiences answering one as a
step in the category.

## No category, gate or option value is named anywhere here

Same rule as interview_config. Everything comes from the question set.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import interview_config
from app.models import InterviewSession, RawSegment, User
from app.services import gate_answers


async def _recordings_by_question(
    db: AsyncSession, interview_session_id: str
) -> Dict[str, int]:
    """How many takes exist per question in this session.

    Counted rather than boolean because a question legitimately holds several
    takes, and the panel shows that.
    """
    rows = (
        await db.execute(
            select(RawSegment.question_id).where(
                RawSegment.interview_session_id == interview_session_id,
                RawSegment.question_id.isnot(None),
            )
        )
    ).scalars().all()
    counts: Dict[str, int] = {}
    for qid in rows:
        counts[qid] = counts.get(qid, 0) + 1
    return counts


def _step_view(
    step: Dict[str, Any], answers: Dict[str, str], takes: Dict[str, int]
) -> Dict[str, Any]:
    """One resolved step, plus whether it is done and how."""
    view: Dict[str, Any] = {
        "kind": step["kind"],
        "id": step["id"],
        "text": step["text"],
    }
    if step["kind"] == interview_config.GATE:
        # Options come straight from the data — the client renders one button
        # per option and never assumes a yes/no pair.
        view["options"] = [
            {"value": o["value"], "label": o["label"]} for o in step["options"]
        ]
        view["answer"] = answers.get(step["id"])
        view["done"] = view["answer"] is not None
    else:
        view["takes"] = takes.get(step["id"], 0)
        view["done"] = view["takes"] > 0
    return view


def build_category_view(
    category: Dict[str, Any],
    answers: Dict[str, str],
    takes: Dict[str, int],
) -> Dict[str, Any]:
    """One category's resolved flow state.

    `steps` holds only what is REACHABLE given the answers so far — an
    unanswered gate ends the list, because nothing behind it is knowable.
    """
    resolved = interview_config.resolve_steps(category["steps"], answers)
    steps = [_step_view(s, answers, takes) for s in resolved]
    settled = interview_config.category_is_settled(category, answers)

    done_count = sum(1 for s in steps if s["done"])
    current = next((s for s in steps if not s["done"]), None)
    complete = settled and current is None

    return {
        "id": category["id"],
        "label": category["name"],
        "steps": steps,
        "settled": settled,
        # Position of the step being worked on, 1-based, for "step N of M".
        # None when the category is complete — there is no current step.
        "position": (steps.index(current) + 1) if current is not None else None,
        # Only meaningful once settled; None means "not yet knowable", and the
        # UI must not substitute a guess. See §8.4.
        "total": len(steps) if settled else None,
        "done_count": done_count,
        "current_step_id": current["id"] if current is not None else None,
        "complete": complete,
    }


async def get_flow(
    db: AsyncSession, user: User, session: InterviewSession
) -> Dict[str, Any]:
    """The whole interview state for one producer's session.

    Every category is resolved against the SAME answer set in one pass — the
    accordion shows them all, so per-category fetching would be a round trip
    each for no benefit.
    """
    answers = await gate_answers.get_answers(db, session.id)
    takes = await _recordings_by_question(db, session.id)

    categories = [
        build_category_view(cat, answers, takes)
        for cat in interview_config._categories(user.recording_language)
    ]

    # The current category is the first incomplete one in document order —
    # which is the chronology, since the file's order is the chronology.
    current_id = next((c["id"] for c in categories if not c["complete"]), None)

    free_navigation = bool(user.free_navigation)
    for cat in categories:
        cat["current"] = cat["id"] == current_id
        # Reachable = openable in the accordion. Completed categories reopen
        # so an answer can be reviewed or re-recorded; everything past the
        # current one is inert until free navigation is on.
        #
        # Enforced here rather than only in the UI: hiding a click handler is
        # a presentation detail, and the API is what actually decides.
        cat["reachable"] = free_navigation or cat["complete"] or cat["current"]

    return {
        "interview_session_id": session.id,
        "free_navigation": free_navigation,
        "current_category_id": current_id,
        # True when every category is complete — the interview is finished.
        "complete": current_id is None,
        "categories": categories,
    }


def can_record(flow: Dict[str, Any], category_id: str, question_id: str) -> bool:
    """Whether this producer may record this question right now.

    The forward-navigation rule, decided server-side. The UI hides what cannot
    be reached, but hiding is not enforcing — a stale tab or a replayed request
    would otherwise write into a category the producer has not arrived at.
    """
    cat = next((c for c in flow["categories"] if c["id"] == category_id), None)
    if cat is None or not cat["reachable"]:
        return False
    return any(
        s["id"] == question_id and s["kind"] == interview_config.QUESTION
        for s in cat["steps"]
    )


def find_gate_category(language: str, gate_id: str) -> Optional[str]:
    """Which category a gate belongs to — so answering one can be authorised
    the same way recording is."""
    for cat in interview_config._categories(language):
        for step in interview_config.iter_steps(cat["steps"]):
            if step["id"] == gate_id:
                return cat["id"]
    return None
