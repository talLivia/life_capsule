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

## A period's content is its RECORDINGS

docs/MEDIA_GALLERY.md §1. A childhood-hobbies answer names nobody, and that is
correct — the original bug was a period rendering empty despite holding a
real, playable recording, because people were the only sub-bubble type. A
period's recordings are its content; people feed highlight captions and the
family tree's views, and an entity-less period needs no special case — its
recordings land in bubbles exactly like every other period's.

## The default card is a SUMMARY; every deeper level keeps a constant shape

docs/MEDIA_GALLERY.md §1.6–§1.8. The collapsed card is title, one generated
sentence, and grouped bubbles — the SAME shape at 3 recordings or 50; volume
is absorbed by grouping and summarization, never expressed as more chips or
rows. Bubbles are REAL TAG CONTENT: the period's own `topic_tags`, chosen by
set cover, capped, and a TOTAL PARTITION — every recording belongs to
exactly one bubble, with the catch-all (עוד) holding whatever no winning tag
claimed. The card itself is static: bubbles are the SOLE route into a
period's recordings — there is no period-wide list. A bubble opens a CAPPED
highlight selection ranked by the importance score ingestion already
computed, with the bubble's own full list one click deeper, so §1.2's
reachability rule holds through the partition. Raw interview-question text
never renders here at any level; a moment's only name is its generated
content title (period_insights).

No year range yet, deliberately: §1.4's producer-scoped attribution is not
built, and the only year in the live archive is the father's birth year —
exactly the year a childhood range must NOT be derived from.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import interview_config
from app.models import Entity, EntityMention, InterviewSession, RawSegment
import asyncio

from app.database import AsyncSessionLocal
from app.services import period_insights


def _schedule_refresh(coro) -> None:
    """Fire-and-forget by default; tests monkeypatch this to await inline so
    the refresh outcome is deterministic."""
    asyncio.create_task(coro)

logger = logging.getLogger(__name__)

# Bubbles per period, and highlights per bubble — the shape is constant
# whatever the archive holds; a bubble's full list is one click deeper
# inside that bubble.
_TAG_BUBBLE_CAP = 5
_HIGHLIGHT_CAP = 4

# The catch-all bubble: every recording no winning tag claimed — tags that
# lost the cap, and untagged segments alike. Bubbles are the SOLE route into
# a period's recordings (the period-wide "all moments" screen is gone), so
# the catch-all is what makes the partition total: exactly one bubble per
# recording, always. When it is the ONLY bubble (nothing tagged yet) it is
# labelled רגעים — "עוד" with nothing before it reads as "more than what?".
_MORE_LABELS = {"he": "עוד", "en": "More"}
_MOMENTS_LABELS = {"he": "רגעים", "en": "Moments"}


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
    period_segments: List[List[RawSegment]] = []
    hidden = 0
    for bucket in _buckets(language):
        ids = set(bucket["question_ids"]) | set(bucket["retired_question_ids"])
        segments = [s for qid in ids for s in by_question.get(qid, [])]
        if not segments:
            hidden += 1
            continue
        segments.sort(key=lambda s: s.created_at)
        # Fetched for the highlight captions only — the payload carries no
        # people list any more: nothing renders one since the per-entity
        # chips went, and the tree's moments endpoint serves entity views.
        people = await _people_in(db, producer_id, [s.id for s in segments])
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
                "groups": _groups_for(people, language, segments),
            }
        )
        period_segments.append(segments)

    # One sentence per period, SERVED FROM THE STORE — never regenerated
    # in-request. A 164-recording bulk import staled every period at once
    # and the read-time refresh (16 sequential LLM calls) blew the client's
    # 30s timeout (live, 2026-08-28). The refresh now runs fire-and-forget
    # with its own session; this load shows the previous sentences (or none)
    # and the next load is fresh. Same watermark gating, same total LLM
    # spend — only WHEN it is paid changed.
    summaries = await period_insights.stored_summaries(db, producer_id)
    period_args = [
        (p["category"], p["category_label"], segments)
        for p, segments in zip(periods, period_segments)
    ]

    async def _refresh_in_background() -> None:
        try:
            async with AsyncSessionLocal() as bg_db:
                await period_insights.refresh_period_summaries(
                    bg_db, producer_id, language, period_args
                )
        except Exception as e:  # a failed refresh just means stale sentences
            logger.warning(f"background period-summary refresh failed: {e}")

    _schedule_refresh(_refresh_in_background())
    for period in periods:
        period["summary"] = summaries.get(period["category"])

    return {
        "periods": periods,
        # Said out loud rather than left as a gap in the page. A producer who
        # sees four bubbles should know whether that is the whole interview.
        "hidden_empty_periods": hidden,
        "unplaced_recordings": len(undated),
    }


def _groups_for(
    people: List[Dict[str, Any]],
    language: str,
    segments: List[RawSegment],
) -> List[Dict[str, Any]]:
    """The compact card's bubbles — real tag content, capped, DISJOINT (§1.8).

    Bubbles come straight from the `topic_tags` ingestion already writes per
    segment; no classification pass, no taxonomy — the label IS the tag the
    archive's own content produced ('בתי ספר', 'שירות צבאי'). Capped because
    free-form tags are mostly one-offs — measured 43 of 47 distinct tags
    appearing once — and a bubble per one-off is the density bug in bubble
    form.

    Bubbles PARTITION the recordings: a segment carries several tags, but it
    belongs to exactly one bubble — tags are chosen greedily by how many
    still-unassigned recordings they cover (set cover, ties by first
    appearance), and each chosen tag claims its unassigned recordings. The
    same recording showing under both משפחה and טבריה read as a duplication
    bug, and exclusive membership is what makes the counts honest: they sum
    to at most the period's recordings, never more.

    The partition is TOTAL: recordings no winning tag claimed — losers of
    the cap and untagged segments alike — land in the catch-all bubble, so
    every recording belongs to exactly one bubble and bubbles are the sole
    route into a period. The period-wide "all moments" screen is gone;
    within a bubble, everything beyond the highlights is its own full list.
    """
    coverage: Dict[str, List[RawSegment]] = {}
    first_seen: Dict[str, int] = {}
    for segment in segments:
        for tag in segment.topic_tags or []:
            if not isinstance(tag, str) or not tag.strip():
                continue
            tag = tag.strip()
            if tag not in coverage:
                coverage[tag] = []
                first_seen[tag] = len(first_seen)
            coverage[tag].append(segment)

    groups: List[Dict[str, Any]] = []
    assigned: set = set()
    while len(groups) < _TAG_BUBBLE_CAP and coverage:
        remaining = {
            tag: [s for s in tagged if s.id not in assigned]
            for tag, tagged in coverage.items()
        }
        best = min(
            remaining,
            key=lambda t: (-len(remaining[t]), first_seen[t]),
        )
        members = remaining[best]
        if not members:
            break
        assigned.update(s.id for s in members)
        del coverage[best]
        groups.append(
            {
                "key": best,
                "label": best,
                "count": len(members),
                "segment_ids": [s.id for s in members],
                "highlights": _highlights(members, people),
            }
        )
    leftovers = [s for s in segments if s.id not in assigned]
    if leftovers:
        labels = _MORE_LABELS if groups else _MOMENTS_LABELS
        groups.append(
            {
                "key": "more",
                "label": labels.get(language, labels["en"]),
                "count": len(leftovers),
                "segment_ids": [s.id for s in leftovers],
                "highlights": _highlights(leftovers, people),
            }
        )
    return groups


def _highlights(
    segments: List[RawSegment], people: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """A capped, content-aware selection — never a proportionally longer list.

    Ranked by the importance score ingestion already computed (no LLM call
    here), diversified across QUESTIONS so three takes of one answer cannot
    fill every slot: the first pass prefers moments answering a question not
    yet represented, the second fills the rest by rank. Presented in
    recording order — the cap decides WHAT is shown, chronology decides the
    order, which is the "years decorate, they never move" rule one level
    down.

    Captions quote the stored mention summaries — what the recording said
    about its most-mentioned person — and are absent when nobody was named:
    the title carries the content there.
    """
    ranked = sorted(
        segments,
        key=lambda s: (
            -(s.importance_score if s.importance_score is not None else -1.0),
            s.created_at,
        ),
    )
    picked: List[RawSegment] = []
    covered_questions: set = set()
    for segment in ranked:
        if len(picked) >= _HIGHLIGHT_CAP:
            break
        if segment.question_id not in covered_questions:
            picked.append(segment)
            covered_questions.add(segment.question_id)
    for segment in ranked:
        if len(picked) >= _HIGHLIGHT_CAP:
            break
        if segment not in picked:
            picked.append(segment)
    picked.sort(key=lambda s: s.created_at)

    highlights = []
    for segment in picked:
        caption = None
        for person in people:  # already mentions-desc ordered
            summary = person.get("mention_summaries", {}).get(segment.id)
            if summary:
                caption = summary
                break
        highlights.append({"segment_id": segment.id, "caption": caption})
    return highlights


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
                # The generated content title — the moment's ONLY rendered
                # name. question_asked stays in the payload as data, but the
                # page never shows raw question text at any archive size.
                "title": segment.moment_title,
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
    """Who this period mentions — INTERNAL, feeds the highlight captions.

    Not part of the timeline payload since the per-entity chips went
    (§1.9): a caption quotes what a recording said about its most-mentioned
    person, and the mentions-desc order here is what "most-mentioned" means.
    Entity-centric views live on the family tree page.
    """
    if not segment_ids:
        return []
    rows = (
        await db.execute(
            select(Entity, EntityMention.raw_segment_id, EntityMention.summary)
            .join(EntityMention, EntityMention.entity_id == Entity.id)
            .where(
                Entity.producer_id == producer_id,
                EntityMention.raw_segment_id.in_(segment_ids),
            )
        )
    ).all()

    by_entity: Dict[str, Dict[str, Any]] = {}
    for entity, raw_segment_id, mention_summary in rows:
        person = by_entity.setdefault(
            entity.id,
            {
                "id": entity.id,
                "name": entity.name,
                "type": entity.type,
                # Decoration only — never used to order anything. See the header.
                "year_start": entity.year_start,
                "segment_ids": [],
                # What each recording said about them — feeds the highlight
                # captions, stripped before the payload ships.
                "mention_summaries": {},
            },
        )
        person["segment_ids"].append(raw_segment_id)
        if mention_summary:
            person["mention_summaries"][raw_segment_id] = mention_summary

    people = list(by_entity.values())
    for person in people:
        # One mention row per (entity, recording), so the count IS the list.
        person["mentions"] = len(person["segment_ids"])
    people.sort(key=lambda p: (-p["mentions"], p["name"]))
    return people
