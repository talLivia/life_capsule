"""Derived display data for the timeline's compact cards — docs/MEDIA_GALLERY.md §1.6.

Two LLM-derived pieces, both refreshed AT READ rather than at ingest, for the
same reason each: the timeline is the only consumer, the refresh needs no
pipeline coupling (nothing here touches analysis_graph, which is unsafe to
edit while a recording is in flight), and deletion staleness is handled by
construction — the read recomputes what the archive currently holds.

## The period summary

One sentence per (producer, category), stored in `period_summaries` with the
segment ids and language it was built from as the watermark. Regenerated
exactly when that watermark no longer matches; served from Postgres otherwise,
so repeat views cost zero LLM calls. If regeneration fails and a stored
sentence exists, the STALE sentence is served — yesterday's true sentence
beats a blank card — and the watermark is left unchanged so the next read
tries again.

## The organisation subtype

`entities.type` cannot say "school" — measured on the live archive, three
schools, the air force and a college all share type='organisation'. Grouping
them needs one closed-vocabulary label per organisation, assigned here in one
batched call for every organisation that has none. This is a DISPLAY judgement
about where a chip sits, never a fact the producer stated: a wrong label
misplaces a bubble, visibly and recoverably, which is why an LLM is acceptable
here when the governing rule forbids it for years and relations.

'other' is written when the model was asked and nothing fit, so the entity is
never re-sent; an unparseable or invented label leaves NULL, so the next read
retries. Same never-asked vs asked-and-unknown split as the *_asked_at columns.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entity, EntityMention, PeriodSummary, RawSegment
from app.services.llm import llm_service

logger = logging.getLogger(__name__)

SUBTYPE_VOCABULARY = frozenset(
    {"school", "higher_education", "military", "workplace", "community", "other"}
)

# Per-recording cap on what the summary prompt reads. Transcripts in this
# archive are answers to single interview questions, not lectures — the cap
# exists so one rambling take cannot crowd out the rest of the period.
_TRANSCRIPT_CHARS = 2000

_LANGUAGE_NAMES = {"he": "Hebrew", "en": "English"}


# ── organisation subtypes ─────────────────────────────────────────────────


def _parse_subtype_reply(raw: str, expected_ids: set) -> Dict[str, str]:
    """{entity_id: label} for every id the model answered validly.

    An invented label or unknown id is dropped, not coerced: writing 'other'
    for garbage would stamp asked-and-unknown on an entity the model never
    actually judged, and the stamp is what stops retries.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("subtype classification reply was not JSON; leaving NULLs")
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        entity_id: label
        for entity_id, label in parsed.items()
        if entity_id in expected_ids and label in SUBTYPE_VOCABULARY
    }


async def classify_organisation_subtypes(db: AsyncSession, producer_id: str) -> None:
    """Assign a subtype to every organisation that has none — one batched call.

    A no-op when nothing is unclassified, which is every read except the first
    after a new organisation lands. Failures leave NULL and are retried on the
    next read; a NULL groups under "more" meanwhile, so the page never waits
    on this succeeding.
    """
    organisations = (
        (
            await db.execute(
                select(Entity).where(
                    Entity.producer_id == producer_id,
                    Entity.type == "organisation",
                    Entity.subtype.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not organisations:
        return

    # What each recording said about them — the classification signal. The
    # name alone is often enough (בית ספר תחכמוני), but הכפר הירוק is a school
    # only the summaries say is one.
    mentions = (
        await db.execute(
            select(EntityMention.entity_id, EntityMention.summary).where(
                EntityMention.entity_id.in_([o.id for o in organisations]),
                EntityMention.summary.isnot(None),
            )
        )
    ).all()
    summaries_by_id: Dict[str, List[str]] = {}
    for entity_id, summary in mentions:
        summaries_by_id.setdefault(entity_id, []).append(summary)

    lines = []
    for org in organisations:
        context = "; ".join(summaries_by_id.get(org.id, [])[:3])
        lines.append(f"{org.id}: {org.name}" + (f" — {context}" if context else ""))

    try:
        raw = await llm_service.generate_response(
            messages=[{"role": "user", "content": "\n".join(lines)}],
            system_prompt=(
                "Each line is an organisation from a life-story archive: "
                "id: name — what recordings said about it.\n"
                "Classify each into exactly one of: school, higher_education, "
                "military, workplace, community, other. Use 'other' when none fits.\n"
                'Reply with ONLY a JSON object mapping id to label, e.g. '
                '{"<id>": "school"}.'
            ),
            temperature=0,  # closed-vocabulary classification — deterministic
        )
    except Exception as e:
        logger.warning(f"subtype classification failed, leaving NULLs: {e}")
        return

    labels = _parse_subtype_reply(raw, {o.id for o in organisations})
    if not labels:
        return
    for org in organisations:
        if org.id in labels:
            org.subtype = labels[org.id]
    await db.commit()


# ── moment titles ─────────────────────────────────────────────────────────

# Segments per titling call. A first load of a full real archive has a
# hundred untitled recordings; one giant prompt risks a truncated reply that
# loses every title in it, where a lost chunk loses twenty and retries.
_TITLE_BATCH = 20


async def ensure_moment_titles(
    db: AsyncSession, language: str, segments: Sequence[RawSegment]
) -> None:
    """Give every transcribed segment a content title, once.

    The interview question never renders by default, so a moment's only name
    is this. Untranscribed segments are skipped (nothing to title yet) and
    picked up once the transcript lands; an unparseable reply leaves NULL and
    the next read retries. The question text IS given to the model — it names
    referents the words never do ("my commander") — it just never renders.
    """
    untitled = [
        s
        for s in segments
        if (s.transcript or "").strip()
        and (s.moment_title is None or s.moment_title_language != language)
    ]
    if not untitled:
        return

    language_name = _LANGUAGE_NAMES.get(language, language)
    wrote = False
    for start in range(0, len(untitled), _TITLE_BATCH):
        batch = untitled[start : start + _TITLE_BATCH]
        lines = [
            f"{s.id}:\nQ: {s.question_asked}\nA: {(s.transcript or '')[:_TRANSCRIPT_CHARS]}"
            for s in batch
        ]
        try:
            raw = await llm_service.generate_response(
                messages=[{"role": "user", "content": "\n\n".join(lines)}],
                system_prompt=(
                    "Each block is one recorded moment from a life-story "
                    "archive: an id, the interview question asked, and what "
                    "the storyteller answered.\n"
                    f"Give each moment a short, concrete title in {language_name} "
                    "(3-6 words) naming what the ANSWER is about — never a "
                    "rephrasing of the question.\n"
                    'Reply with ONLY a JSON object mapping id to title, e.g. '
                    '{"<id>": "..."}.'
                ),
            )
        except Exception as e:
            logger.warning(f"moment titling failed, leaving NULLs: {e}")
            continue
        titles = _parse_title_reply(raw, {s.id for s in batch})
        for segment in batch:
            title = titles.get(segment.id)
            if title:
                segment.moment_title = title
                segment.moment_title_language = language
                wrote = True
    if wrote:
        await db.commit()


def _parse_title_reply(raw: str, expected_ids: set) -> Dict[str, str]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("moment-title reply was not JSON; leaving NULLs")
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        segment_id: title.strip()
        for segment_id, title in parsed.items()
        if segment_id in expected_ids and isinstance(title, str) and title.strip()
    }


# ── period summaries ──────────────────────────────────────────────────────


def _build_summary_prompt(
    category_label: str, language: str, segments: Sequence[RawSegment]
) -> Tuple[str, str]:
    """(system_prompt, user_content) for one period's sentence."""
    language_name = _LANGUAGE_NAMES.get(language, language)
    parts = []
    for segment in segments:
        transcript = (segment.transcript or "").strip()
        if not transcript:
            continue
        parts.append(f"Q: {segment.question_asked}\nA: {transcript[:_TRANSCRIPT_CHARS]}")
    system = (
        "You summarize one period of a person's life story from interview "
        "answers they recorded. Write in the third person about the "
        "storyteller.\n"
        f"Reply with EXACTLY ONE short sentence in {language_name}. "
        "No preamble, no quotes, no list."
    )
    user = f'Life period: "{category_label}"\n\n' + "\n\n".join(parts)
    return system, user


async def refresh_period_summaries(
    db: AsyncSession,
    producer_id: str,
    language: str,
    periods: List[Tuple[str, str, List[RawSegment]]],
) -> Dict[str, Optional[str]]:
    """{category: sentence} for every period, regenerating only what is stale.

    `periods` is (category, category_label, segments) per visible period. A
    period is stale when no row exists, the recording ids behind it changed
    (either direction — deletion too), or the language did. Everything else is
    served from the table with no LLM call.
    """
    rows = (
        (
            await db.execute(
                select(PeriodSummary).where(PeriodSummary.producer_id == producer_id)
            )
        )
        .scalars()
        .all()
    )
    stored = {row.category: row for row in rows}

    result: Dict[str, Optional[str]] = {}
    stale: List[Tuple[str, str, List[RawSegment], Any]] = []
    for category, label, segments in periods:
        row = stored.get(category)
        current_ids = sorted(s.id for s in segments)
        if row is not None and row.language == language and sorted(row.source_segment_ids) == current_ids:
            result[category] = row.summary
        else:
            stale.append((category, label, segments, row))

    for category, label, segments, row in stale:
        system, user = _build_summary_prompt(label, language, segments)
        try:
            sentence = (
                await llm_service.generate_response(
                    messages=[{"role": "user", "content": user}],
                    system_prompt=system,
                )
            ).strip()
        except Exception as e:
            # A stale sentence beats a blank card; the unchanged watermark
            # makes the next read try again.
            logger.warning(f"period summary for {category!r} failed: {e}")
            result[category] = row.summary if row is not None else None
            continue
        if not sentence:
            result[category] = row.summary if row is not None else None
            continue
        if row is None:
            db.add(
                PeriodSummary(
                    producer_id=producer_id,
                    category=category,
                    summary=sentence,
                    language=language,
                    source_segment_ids=sorted(s.id for s in segments),
                )
            )
        else:
            row.summary = sentence
            row.language = language
            row.source_segment_ids = sorted(s.id for s in segments)
        result[category] = sentence

    if stale:
        await db.commit()
    return result
