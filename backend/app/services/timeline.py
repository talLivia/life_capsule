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

## A period's content is its RECORDINGS; people are a lens on them

docs/MEDIA_GALLERY.md §1. A childhood-hobbies answer names nobody, and that is
correct — but a period whose sub-bubbles are only mentioned people renders
empty despite holding a real, playable recording. So every period lists its
recordings, titled by their interview question, and each person carries the
segment ids that mention them so selecting one FILTERS the recordings rather
than opening anything separate. No special case for the entity-less period:
it shows its recordings exactly like every other one.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy import select
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
                "recordings": _recordings_in(segments),
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


def _recordings_in(segments: List[RawSegment]) -> List[Dict[str, Any]]:
    """The period's own content — one sub-bubble per recording.

    No query: build_timeline already holds the rows. `take_index` is not a
    column anywhere; takes of one question are separated by `created_at`
    order alone (the CLAUDE.md rule), so it is derived here the same way.
    `video_url` is served as stored, exactly as `get_entity_moments` does.
    """
    seen_takes: Dict[str, int] = {}
    take_totals: Dict[str, int] = {}
    for segment in segments:
        take_totals[segment.question_id] = take_totals.get(segment.question_id, 0) + 1

    recordings = []
    for segment in segments:  # already created_at-sorted by the caller
        seen_takes[segment.question_id] = seen_takes.get(segment.question_id, 0) + 1
        recordings.append(
            {
                "segment_id": segment.id,
                "question_asked": segment.question_asked,
                "question_id": segment.question_id,
                "created_at": segment.created_at,
                "take_index": seen_takes[segment.question_id],
                # So the client can label "take 2 of 3" without counting —
                # a second take of one question renders under the same title.
                "take_count": take_totals[segment.question_id],
                "video_url": segment.video_url,
            }
        )
    return recordings


async def _people_in(
    db: AsyncSession, producer_id: str, segment_ids: List[str]
) -> List[Dict[str, Any]]:
    """Who this period is about — a lens on its recordings, not the content.

    A person is not owned by one period: someone mentioned in two life stages
    appears in both, which is correct and needs no special case.

    Each person carries the ids of the segments mentioning them, so selecting
    one filters the recording sub-bubbles client-side — no second request and
    no separate endpoint to drift.
    """
    if not segment_ids:
        return []
    rows = (
        await db.execute(
            select(Entity, EntityMention.raw_segment_id)
            .join(EntityMention, EntityMention.entity_id == Entity.id)
            .where(
                Entity.producer_id == producer_id,
                EntityMention.raw_segment_id.in_(segment_ids),
            )
        )
    ).all()

    by_entity: Dict[str, Dict[str, Any]] = {}
    for entity, raw_segment_id in rows:
        person = by_entity.setdefault(
            entity.id,
            {
                "id": entity.id,
                "name": entity.name,
                "type": entity.type,
                # Decoration only — never used to order anything. See the header.
                "year_start": entity.year_start,
                "segment_ids": [],
            },
        )
        person["segment_ids"].append(raw_segment_id)

    people = list(by_entity.values())
    for person in people:
        # One mention row per (entity, recording), so the count IS the list.
        person["mentions"] = len(person["segment_ids"])
    people.sort(key=lambda p: (-p["mentions"], p["name"]))
    return people
