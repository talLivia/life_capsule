"""Derived display data for the timeline's compact cards — docs/MEDIA_GALLERY.md §1.6–§1.8.

Two LLM-derived pieces, both refreshed AT READ rather than at ingest, for the
same reason each: the timeline is the only consumer, the refresh needs no
pipeline coupling (nothing here touches analysis_graph, which is unsafe to
edit while a recording is in flight), and staleness is handled by
construction — the read recomputes against what the archive currently holds.

## The period summary

One sentence per (producer, category), stored in `period_summaries` with the
segment ids and language it was built from as the watermark. Regenerated
exactly when that watermark no longer matches; served from Postgres otherwise,
so repeat views cost zero LLM calls. If regeneration fails and a stored
sentence exists, the STALE sentence is served — yesterday's true sentence
beats a blank card — and the watermark is left unchanged so the next read
tries again.

## The moment title

One content title per recording — its only rendered name, since raw question
text never renders. Same watermark pattern, one level down: the stored
sha256 of the transcript the title was generated from (migration 0024), so a
title regenerates exactly when ITS OWN words change (an in-place re-analysis)
or the language does. A new or re-recorded segment is a new row and titles
itself on the next read; nothing regenerates on unrelated saves.

There is no classification pass any more: grouping comes straight from the
`topic_tags` ingestion already writes (§1.8), so the only labels here are the
ones the archive's own content produced.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PeriodSummary, RawSegment
from app.services.llm import llm_service

logger = logging.getLogger(__name__)

# Per-recording cap on what the summary prompt reads. Transcripts in this
# archive are answers to single interview questions, not lectures — the cap
# exists so one rambling take cannot crowd out the rest of the period.
_TRANSCRIPT_CHARS = 2000

_LANGUAGE_NAMES = {"he": "Hebrew", "en": "English"}


# ── moment titles ─────────────────────────────────────────────────────────

# Segments per titling call. A first load of a full real archive has a
# hundred untitled recordings; one giant prompt risks a truncated reply that
# loses every title in it, where a lost chunk loses twenty and retries.
_TITLE_BATCH = 20


def _transcript_hash(transcript: str) -> str:
    # Duplicated verbatim in migration 0024's backfill — changing one without
    # the other silently regenerates every title once.
    return hashlib.sha256(transcript.encode("utf-8")).hexdigest()


async def ensure_moment_titles(
    db: AsyncSession, language: str, segments: Sequence[RawSegment]
) -> None:
    """Give every transcribed segment a content title, refreshed with its words.

    The interview question never renders by default, so a moment's only name
    is this. Stale when no title exists, the language changed, or the
    transcript no longer matches the stored hash it was generated from — an
    in-place re-analysis must not leave a title naming words that are gone.
    Untranscribed segments are skipped (nothing to title yet) and picked up
    once the transcript lands; an unparseable reply leaves NULL and the next
    read retries. The question text IS given to the model — it names
    referents the words never do ("my commander") — it just never renders.
    """
    untitled = [
        s
        for s in segments
        if (s.transcript or "").strip()
        and (
            s.moment_title is None
            or s.moment_title_language != language
            or s.moment_title_source != _transcript_hash(s.transcript)
        )
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
                segment.moment_title_source = _transcript_hash(segment.transcript)
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
