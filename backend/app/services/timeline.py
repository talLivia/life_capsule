"""The timeline — one bubble per life period, in the interview's own order.

Phase 5 of docs/FAMILY_TREE_TIMELINE.md. Read-only.

## The order is the file's order

Chronology comes from `interview_config.get_categories`, which is document
order in `interview_questions.json`. Reordering the file reorders the timeline
with no code change, which is the §2A rule — nothing here knows a category
name, a count, or a `question_index` range.

Entity YEARS are deliberately not used to order anything. They exist now
(Phase 3) and they are the sharper axis in principle, but a page ordered two
ways is a page that disagrees with itself, and one entity in the live archive
has a year. Years decorate a sub-bubble; they never move it.

## Retired questions still have to land somewhere

The correction in §3, and it is the whole reason this file is not four lines
of SQL. `get_categories()['question_ids']` is LIVE ids only — deliberately, so
nothing can offer a withdrawn question to a producer. Matching recordings on
that alone drops every recording of a retired question, and a category that no
longer exists at all is never even yielded, so its recordings have nowhere to
appear. Both were measured: 16 of 16 recordings placed via
`category_for_question_id`, 0 of 16 via the per-category id sets.

So a bucket matches on live ids UNION its retired ids, and retired-only
categories are appended after the live ones. They have no position in the new
chronology, which is honest — they are historical.

Retired ids stay in their own field. Folding them into `question_ids` would
make that field mean two different things depending on the caller, which is the
class of bug the whole stable-`question_id` design exists to avoid.

## Empty periods are hidden

A category with no recordings is a question not yet answered, not a fact about
the life. Sixteen bubbles with eleven empty reads as a broken page, so they are
hidden and counted. This is the opposite call from the tree's "not yet placed",
and deliberately: there, the person IS a fact — someone the producer talked
about — and hiding them would lose something real.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import interview_config
from app.models import Entity, EntityMention, InterviewSession, RawSegment

logger = logging.getLogger(__name__)


def _buckets(language: str) -> List[Dict[str, Any]]:
    """Live periods in chronological order, then retired-only ones."""
    buckets: List[Dict[str, Any]] = []
    index: Dict[str, Dict[str, Any]] = {}

    for category in interview_config.get_categories(language):
        bucket = {
            "category": category["category"],
            "category_label": category["category_label"],
            "question_ids": list(category["question_ids"]),
            "retired_question_ids": [],
            "retired_only": False,
        }
        buckets.append(bucket)
        index[bucket["category"]] = bucket

    for item in interview_config.get_retired():
        bucket = index.get(item["category"])
        if bucket is None:
            bucket = {
                "category": item["category"],
                # The retired entry's own label, not the raw key — a bucket
                # reading `post_military` beside Hebrew labels is a bug in a
                # page whose entire job is being legible to a family.
                "category_label": item.get("category_label")
                or item.get("label")
                or item["category"],
                "question_ids": [],
                "retired_question_ids": [],
                # No position in the new chronology, so appended rather than
                # slotted in. They are historical and the page says so.
                "retired_only": True,
            }
            buckets.append(bucket)
            index[bucket["category"]] = bucket
        bucket["retired_question_ids"].append(item["id"])

    return buckets


async def build_timeline(
    db: AsyncSession, producer_id: str, language: str = "he"
) -> Dict[str, Any]:
    """One bubble per life period that actually holds a recording."""
    rows = (
        await db.execute(
            select(RawSegment)
            .join(InterviewSession, InterviewSession.id == RawSegment.interview_session_id)
            .where(InterviewSession.user_id == producer_id)
            .order_by(RawSegment.created_at)
        )
    ).scalars().all()

    by_question: Dict[str, List[RawSegment]] = {}
    undated: List[RawSegment] = []
    for segment in rows:
        if segment.question_id:
            by_question.setdefault(segment.question_id, []).append(segment)
        else:
            # A recording made before question_id existed, or an upload
            # outside the guided set. Counted, never silently dropped.
            undated.append(segment)

    periods: List[Dict[str, Any]] = []
    hidden = 0
    for bucket in _buckets(language):
        ids = set(bucket["question_ids"]) | set(bucket["retired_question_ids"])
        segments = [s for qid in ids for s in by_question.get(qid, [])]
        if not segments:
            hidden += 1
            continue
        segments.sort(key=lambda s: s.created_at)
        periods.append(
            {
                "category": bucket["category"],
                "category_label": bucket["category_label"],
                "retired_only": bucket["retired_only"],
                "recording_count": len(segments),
                # DISTINCT questions, not recordings: three takes on one
                # question is one question answered. Same rule as /record.
                "question_count": len({s.question_id for s in segments}),
                "people": await _people_in(db, producer_id, [s.id for s in segments]),
            }
        )

    return {
        "periods": periods,
        # Said out loud rather than left as a gap in the page. A producer who
        # sees four bubbles should know whether that is the whole interview.
        "hidden_empty_periods": hidden,
        "unplaced_recordings": len(undated),
    }


async def _people_in(
    db: AsyncSession, producer_id: str, segment_ids: List[str]
) -> List[Dict[str, Any]]:
    """Who this period is about — the sub-bubbles.

    A person is not owned by one period: someone mentioned in two life stages
    appears in both, which is correct and needs no special case.
    """
    if not segment_ids:
        return []
    rows = (
        await db.execute(
            select(Entity, func.count(EntityMention.id))
            .join(EntityMention, EntityMention.entity_id == Entity.id)
            .where(
                Entity.producer_id == producer_id,
                EntityMention.raw_segment_id.in_(segment_ids),
            )
            .group_by(Entity.id)
            .order_by(func.count(EntityMention.id).desc(), Entity.name)
        )
    ).all()
    return [
        {
            "id": entity.id,
            "name": entity.name,
            "type": entity.type,
            "mentions": mentions,
            # Decoration only — never used to order anything. See the header.
            "year_start": entity.year_start,
        }
        for entity, mentions in rows
    ]
