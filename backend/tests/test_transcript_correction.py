"""Correcting a misheard word everywhere the transcript is stored.

The gap these cover: the transcript lives in TWO places that feed different
halves of the product. `RawSegment.transcript` feeds entity extraction;
`TranscriptChunk.text` + `word_timestamps` feed everything /talk does. A name
corrected on the entity alone leaves the tree saying יוכבד while every clip
still displays and searches יוכרת — which is the live state this was written
for.
"""

import pytest

from app.models import InterviewSession, RawSegment, TranscriptChunk, User
from app.services import transcript_correction as tc
from app.services.transcript_correction import CorrectionRefused

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def recording(db_session, monkeypatch):
    """A recording whose STT misheard יוכבד as יוכרת."""

    async def fake_embed(text):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(tc.embeddings, "embed_text", fake_embed)

    user = User(
        id="u-tc", email="tc@example.com", username="tc",
        hashed_password="x", role="producer",
    )
    db_session.add(user)
    await db_session.flush()
    session = InterviewSession(user_id=user.id, status="active")
    db_session.add(session)
    await db_session.flush()

    segment = RawSegment(
        interview_session_id=session.id, question_asked="q", question_index=0,
        status="ready", transcript="אצל סבתא יוכרת היינו אוכלים כל יום שישי",
    )
    db_session.add(segment)
    await db_session.flush()

    for i, (text, words) in enumerate(
        [
            ("אצל סבתא יוכרת", ["אצל", "סבתא", "יוכרת"]),
            ("היינו אוכלים כל יום שישי", ["היינו", "אוכלים", "כל", "יום", "שישי"]),
        ]
    ):
        db_session.add(
            TranscriptChunk(
                raw_segment_id=segment.id, sequence_index=i,
                start_sec=float(i * 5), end_sec=float(i * 5 + 4), text=text,
                word_timestamps=[
                    {"word": w, "start_sec": i * 5 + n * 0.5, "end_sec": i * 5 + n * 0.5 + 0.4}
                    for n, w in enumerate(words)
                ],
            )
        )
    await db_session.flush()
    return segment


async def test_the_correction_reaches_the_chunks_talk_actually_reads(db_session, recording):
    """The whole point. /talk builds every unit's text from word_timestamps and
    never reads RawSegment.transcript, so a fix that stops at the segment row
    leaves every clip still saying the wrong name."""
    result = await tc.correct_token(
        db_session, segment_id=recording.id, old="יוכרת", new="יוכבד"
    )

    chunks = (
        await db_session.execute(
            TranscriptChunk.__table__.select().where(
                TranscriptChunk.raw_segment_id == recording.id
            ).order_by(TranscriptChunk.sequence_index)
        )
    ).all()
    assert chunks[0].text == "אצל סבתא יוכבד"
    assert [w["word"] for w in chunks[0].word_timestamps] == ["אצל", "סבתא", "יוכבד"]
    assert result.transcript_rewritten
    assert "יוכבד" in recording.transcript and "יוכרת" not in recording.transcript


async def test_the_word_keeps_the_moment_it_was_said(db_session, recording):
    """Only what the machine HEARD is wrong. The timing is the thing being
    preserved — it is what the clip is cut on."""
    before = (
        await db_session.execute(
            TranscriptChunk.__table__.select().where(
                TranscriptChunk.raw_segment_id == recording.id,
                TranscriptChunk.sequence_index == 0,
            )
        )
    ).first()
    original = [(w["start_sec"], w["end_sec"]) for w in before.word_timestamps]

    await tc.correct_token(db_session, segment_id=recording.id, old="יוכרת", new="יוכבד")

    after = (
        await db_session.execute(
            TranscriptChunk.__table__.select().where(
                TranscriptChunk.raw_segment_id == recording.id,
                TranscriptChunk.sequence_index == 0,
            )
        )
    ).first()
    assert [(w["start_sec"], w["end_sec"]) for w in after.word_timestamps] == original


async def test_more_words_out_than_in_is_refused(db_session, recording):
    """An added word has no moment at which it was said, so there is nowhere in
    the clip to put it. Structural, not cautious."""
    with pytest.raises(CorrectionRefused, match="same number of words"):
        await tc.correct_token(
            db_session, segment_id=recording.id, old="יוכרת", new="יוכבד כהן"
        )


async def test_fewer_words_out_than_in_is_refused(db_session, recording):
    with pytest.raises(CorrectionRefused, match="same number of words"):
        await tc.correct_token(
            db_session, segment_id=recording.id, old="סבתא יוכרת", new="יוכבד"
        )


async def test_a_word_that_is_not_there_is_refused(db_session, recording):
    """Refused rather than silently doing nothing — a correction that reports
    success and changes nothing is the shape this session kept finding."""
    with pytest.raises(CorrectionRefused, match="does not appear"):
        await tc.correct_token(db_session, segment_id=recording.id, old="משה", new="מוישה")


async def test_only_whole_words_match(db_session, recording):
    """`\b` is defined against ASCII word characters and does not behave
    usefully around Hebrew, so matching is whole-token. "סבת" must not match
    inside "סבתא"."""
    with pytest.raises(CorrectionRefused, match="does not appear"):
        await tc.correct_token(db_session, segment_id=recording.id, old="סבת", new="סבא")


async def test_a_multi_word_name_of_the_same_length_is_allowed(db_session, recording):
    result = await tc.correct_token(
        db_session, segment_id=recording.id, old="סבתא יוכרת", new="סבתא יוכבד"
    )
    assert result.chunks_rewritten == 1
    chunk = (
        await db_session.execute(
            TranscriptChunk.__table__.select().where(
                TranscriptChunk.raw_segment_id == recording.id,
                TranscriptChunk.sequence_index == 0,
            )
        )
    ).first()
    assert [w["word"] for w in chunk.word_timestamps] == ["אצל", "סבתא", "יוכבד"]


async def test_the_neighbouring_chunk_is_reembedded_too(db_session, recording):
    """A chunk's vector is computed from its own text PLUS its neighbours, so a
    correction invalidates a window rather than one row. Getting this wrong is
    invisible: cosine similarity across a stale and a fresh vector still
    returns a plausible number."""
    result = await tc.correct_token(
        db_session, segment_id=recording.id, old="יוכרת", new="יוכבד"
    )
    assert result.chunks_reembedded == 2, "the corrected chunk AND its neighbour"
    assert result.segment_reembedded


async def test_punctuation_around_the_word_survives(db_session, recording):
    chunk = (
        await db_session.execute(
            TranscriptChunk.__table__.select().where(
                TranscriptChunk.raw_segment_id == recording.id,
                TranscriptChunk.sequence_index == 0,
            )
        )
    ).first()
    await db_session.execute(
        TranscriptChunk.__table__.update()
        .where(TranscriptChunk.id == chunk.id)
        .values(text="אצל סבתא יוכרת,")
    )
    await db_session.flush()

    await tc.correct_token(db_session, segment_id=recording.id, old="יוכרת", new="יוכבד")

    after = (
        await db_session.execute(
            TranscriptChunk.__table__.select().where(TranscriptChunk.id == chunk.id)
        )
    ).first()
    assert after.text == "אצל סבתא יוכבד,"
