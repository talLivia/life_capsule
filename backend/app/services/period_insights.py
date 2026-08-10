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

## Where the moment title went

Not here any more. Titles are generated ONCE, at save time, by
`analysis_graph.extract_topics_node` — the same pipeline run that writes
`topic_tags` — and read as-is everywhere (§1.10). Migration 0025 removed the
watermark columns this module used to check at read; the summary above is
the only read-time refresh left.

There is no classification pass any more either: grouping comes straight
from the `topic_tags` ingestion already writes (§1.8), so the only labels
here are the ones the archive's own content produced.
"""

from __future__ import annotations

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
