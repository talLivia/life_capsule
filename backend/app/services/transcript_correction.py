"""Correcting a misheard word in a recording's transcript, everywhere it lives.

THE PROBLEM THIS FIXES. The transcript is stored TWICE and the copies feed
different halves of the product:

  * `RawSegment.transcript`  -> entity extraction, relations, topic tags, the
                                segment embedding, the extraction panel
  * `TranscriptChunk.text` + `word_timestamps` -> everything `/talk` does

`full_archive_retrieval` builds each utterance unit's TEXT and its play range
from `word_timestamps`, and never reads `RawSegment.transcript` at all. So a
name corrected only on the entity — which is what the confirmation screen's
name edit does — leaves the archive saying `יוכבד` in the tree while every clip
still displays and searches `יוכרת`. Live example in this archive: STT heard
"סבתא יוכרת" and the producer corrected the entity to `יוכבד`.

SAME WORD COUNT ONLY, and the reason is structural rather than cautious. Every
word in a unit is anchored to a `{word, start_sec, end_sec}` entry, and the
clip's boundaries come from the first and last of them. A replacement with more
words has no time at which the extra ones were said; one with fewer would leave
a span of audio with no word against it. A misheard NAME is almost always one
token for one token, which is exactly the case this serves.

WHAT IT DELIBERATELY DOES NOT DO. It does not re-transcribe, so the unit
boundaries the eval's reference ranges are measured against do not move. It
does not touch the audio, which remains the thing that was actually said — this
corrects what the machine HEARD, never what the person said.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RawSegment, TranscriptChunk
from app.services import embeddings

logger = logging.getLogger(__name__)

#: Matches `_CHUNK_CONTEXT_WINDOW` in analysis_graph. A chunk's embedding is
#: computed from its own text PLUS its immediate neighbours, so correcting one
#: chunk invalidates the neighbours' vectors too — not only its own.
_CONTEXT_WINDOW = 1

#: Trailing/leading marks a token can carry without being a different word.
#: Deliberately small: Hebrew gershayim (") is INSIDE words (תנ"ך) and must
#: never be stripped, and an apostrophe is part of ג'ולי.
_EDGE_PUNCTUATION = " \t\n.,!?;:()[]{}—–-"


class CorrectionRefused(ValueError):
    """The correction cannot be applied without inventing timing."""


@dataclass
class CorrectionResult:
    chunks_rewritten: int = 0
    words_rewritten: int = 0
    transcript_rewritten: bool = False
    chunks_reembedded: int = 0
    segment_reembedded: bool = False
    warnings: List[str] = field(default_factory=list)


def _bare(token: str) -> str:
    """A token without its edge punctuation, for comparison only."""
    return token.strip(_EDGE_PUNCTUATION)


def _replace_in_text(text: str, old_words: Sequence[str], new: str) -> tuple[str, int]:
    """Replace whole-token runs of `old_words` with `new`. Returns (text, hits).

    Whitespace-delimited rather than regex word boundaries: `\\b` is defined
    against ASCII word characters and does not behave usefully around Hebrew,
    so a naive boundary match would either miss every match or match inside
    words. Splitting on whitespace and comparing whole tokens is exact, and it
    is what "the same word" means here.
    """
    tokens = re.split(r"(\s+)", text or "")
    # Even indices are tokens, odd are the whitespace between them — kept so
    # the original spacing survives the round trip.
    words = [(i, t) for i, t in enumerate(tokens) if i % 2 == 0 and t]
    new_parts = new.split()
    hits = 0

    i = 0
    while i < len(words):
        run = words[i : i + len(old_words)]
        if len(run) == len(old_words) and all(
            _bare(tok) == old for (_, tok), old in zip(run, old_words)
        ):
            for offset, (index, original) in enumerate(run):
                # Carry the original's edge punctuation onto the replacement so
                # "יוכרת," stays comma'd.
                bare = _bare(original)
                prefix = original[: original.index(bare)] if bare and bare in original else ""
                suffix = original[len(prefix) + len(bare) :] if bare else ""
                tokens[index] = f"{prefix}{new_parts[offset]}{suffix}"
            hits += 1
            i += len(old_words)
            continue
        i += 1
    return "".join(tokens), hits


def _rewrite_word_timestamps(
    entries: Optional[List[Dict[str, Any]]], old_words: Sequence[str], new: str
) -> tuple[Optional[List[Dict[str, Any]]], int]:
    """Rewrite the `word` of matching entries, KEEPING their start/end.

    The timing is the thing being preserved: the person said a word at that
    moment, and only what the machine heard it as is wrong.
    """
    if not entries:
        return entries, 0
    new_parts = new.split()
    out = [dict(e) for e in entries]
    hits = 0
    i = 0
    while i < len(out):
        run = out[i : i + len(old_words)]
        if len(run) == len(old_words) and all(
            _bare(str(e.get("word", ""))) == old for e, old in zip(run, old_words)
        ):
            for offset, entry in enumerate(run):
                entry["word"] = new_parts[offset]
            hits += 1
            i += len(old_words)
            continue
        i += 1
    return out, hits


async def correct_token(
    db: AsyncSession,
    *,
    segment_id: str,
    old: str,
    new: str,
    reembed: bool = True,
) -> CorrectionResult:
    """Rewrite a misheard word everywhere this recording stores it.

    Flushes, never commits — the caller owns the transaction, matching
    `entity_store`'s rule so the text change and anything alongside it land
    together.
    """
    old_words = old.split()
    new_words = new.split()
    if not old_words or not new_words:
        raise CorrectionRefused("Both the old and the new text are required.")
    if len(old_words) != len(new_words):
        raise CorrectionRefused(
            f"A correction has to keep the same number of words — "
            f"{len(old_words)} in, {len(new_words)} out. Every word is anchored "
            f"to the moment it was said, and an added word has no such moment."
        )
    if old == new:
        raise CorrectionRefused("The correction is identical to what is there.")

    segment = (
        await db.execute(select(RawSegment).where(RawSegment.id == segment_id))
    ).scalar_one_or_none()
    if segment is None:
        raise CorrectionRefused("No such recording.")

    chunks = list(
        (
            await db.execute(
                select(TranscriptChunk)
                .where(TranscriptChunk.raw_segment_id == segment_id)
                .order_by(TranscriptChunk.sequence_index)
            )
        ).scalars().all()
    )

    result = CorrectionResult()
    touched: set = set()

    for position, chunk in enumerate(chunks):
        text, text_hits = _replace_in_text(chunk.text or "", old_words, new)
        stamps, stamp_hits = _rewrite_word_timestamps(chunk.word_timestamps, old_words, new)
        if not text_hits and not stamp_hits:
            continue
        chunk.text = text
        if stamp_hits:
            # Reassigned rather than mutated in place: word_timestamps is a
            # JSON column, and SQLAlchemy does not track in-place edits to one.
            chunk.word_timestamps = stamps
        result.chunks_rewritten += 1
        result.words_rewritten += max(text_hits, stamp_hits)
        touched.add(position)
        if text_hits and not stamp_hits and chunk.word_timestamps:
            # The displayed text now says one thing and the per-word timing
            # another, which is the drift this function exists to remove.
            result.warnings.append(
                f"chunk {chunk.sequence_index}: text matched but its word timings did not"
            )

    transcript, transcript_hits = _replace_in_text(segment.transcript or "", old_words, new)
    if transcript_hits:
        segment.transcript = transcript
        result.transcript_rewritten = True

    if not touched and not transcript_hits:
        raise CorrectionRefused(f'"{old}" does not appear in this recording.')

    await db.flush()

    if not reembed:
        return result

    # A chunk's vector is computed from its own text PLUS its neighbours, so a
    # correction invalidates a window, not a single row. Getting this wrong is
    # invisible: cosine similarity across a stale and a fresh vector still
    # returns a plausible number, and retrieval just quietly degrades — the
    # same failure the embeddings rewrite was careful about.
    window: set = set()
    for position in touched:
        for offset in range(-_CONTEXT_WINDOW, _CONTEXT_WINDOW + 1):
            neighbour = position + offset
            if 0 <= neighbour < len(chunks):
                window.add(neighbour)

    texts = [c.text or "" for c in chunks]
    for position in sorted(window):
        start = max(0, position - _CONTEXT_WINDOW)
        end = min(len(texts), position + _CONTEXT_WINDOW + 1)
        context = " ".join(t for t in texts[start:end] if t).strip()
        if not context:
            continue
        try:
            chunks[position].embedding = await embeddings.embed_text(context)
            result.chunks_reembedded += 1
        except Exception as exc:  # noqa: BLE001 — fail soft, same as ingestion
            logger.warning(f"Re-embedding chunk {position} failed: {exc}")
            result.warnings.append(f"chunk {position}: could not be re-embedded")

    if result.transcript_rewritten and segment.transcript:
        try:
            segment.embedding = await embeddings.embed_text(segment.transcript)
            result.segment_reembedded = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Re-embedding segment {segment_id} failed: {exc}")
            result.warnings.append("the recording's own embedding could not be refreshed")

    await db.flush()
    return result
