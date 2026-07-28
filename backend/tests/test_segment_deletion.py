"""Deletion must be COMPLETE — a partial delete leaves data the user believes
is gone, which is the failure mode this module exists to prevent.

These now exercise the entity side FOR REAL rather than against a stubbed
graph. That is the point of the move: deleting a recording and cleaning up its
entities is one Postgres transaction, so a test can assert the end state
instead of asserting that we remembered to call a second database.
"""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Entity, EntityMention, InterviewSession, RawSegment
from app.services import segment_deletion

pytestmark = pytest.mark.asyncio


@pytest.fixture
def sd_session_factory(test_engine, monkeypatch):
    """Point the module's own session factory at the test engine — it opens
    its own sessions rather than receiving one."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(segment_deletion, "AsyncSessionLocal", factory)
    return factory


@pytest.fixture
def patched(monkeypatch):
    """Stub only what is genuinely external — object storage. The database is
    exercised for real."""
    calls = {"files": []}

    async def fake_delete_file(key):
        calls["files"].append(key)

    monkeypatch.setattr(segment_deletion.storage_service, "delete_file", fake_delete_file)
    monkeypatch.setattr(segment_deletion, "_refresh_caches", AsyncMock())
    return calls


async def _seed(db_session, test_user, seg_id="seg-del", key="segments/x/a.webm"):
    session = InterviewSession(id=f"int-{seg_id}", user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    seg = RawSegment(
        id=seg_id, interview_session_id=session.id, question_asked="Q",
        question_index=0, status="ready", video_key=key,
    )
    db_session.add(seg)
    await db_session.commit()
    return seg


async def _mention(db_session, producer_id, name, segment_ids):
    """One entity mentioned by each of `segment_ids`."""
    entity = Entity(
        producer_id=producer_id, name=name, normalized_name=name.lower(), type="person"
    )
    db_session.add(entity)
    await db_session.flush()
    for sid in segment_ids:
        db_session.add(EntityMention(entity_id=entity.id, raw_segment_id=sid))
    await db_session.commit()
    return entity


async def test_delete_segment_removes_file_row_and_entities(
    db_session, test_user, patched, sd_session_factory
):
    await _seed(db_session, test_user)
    await _mention(db_session, test_user.id, "Gila", ["seg-del"])

    result = await segment_deletion.delete_segment_data("seg-del", test_user.id)

    assert result.segments_deleted == 1
    assert result.entities_removed == 1
    assert result.files_deleted == 1
    assert patched["files"] == ["segments/x/a.webm"], "stored video must be deleted"
    assert result.ok

    db_session.expire_all()
    assert (await db_session.execute(select(EntityMention))).scalars().all() == []
    assert (await db_session.execute(select(Entity))).scalars().all() == []


async def test_mentions_cascade_even_with_no_entity_to_orphan(
    db_session, test_user, patched, sd_session_factory
):
    """The mention goes with the recording regardless; the sweep is only about
    the ENTITY. A mention surviving its recording is the ghost this migration
    exists to make impossible."""
    await _seed(db_session, test_user, seg_id="seg-a")
    await _seed(db_session, test_user, seg_id="seg-b", key="segments/x/b.webm")
    await _mention(db_session, test_user.id, "Montreal", ["seg-a", "seg-b"])

    result = await segment_deletion.delete_segment_data("seg-a", test_user.id)

    db_session.expire_all()
    mentions = (await db_session.execute(select(EntityMention))).scalars().all()
    assert [m.raw_segment_id for m in mentions] == ["seg-b"]
    assert result.entities_removed == 0


async def test_an_entity_another_recording_still_mentions_survives(
    db_session, test_user, patched, sd_session_factory
):
    """Graphiti's "drop only when the MENTIONS count is 1" rule, now a NOT
    EXISTS the engine enforces. Verified live on מונטריאול (mentioned by two
    recordings, kept when either was removed); this locks it in."""
    await _seed(db_session, test_user, seg_id="seg-a")
    await _seed(db_session, test_user, seg_id="seg-b", key="segments/x/b.webm")
    await _mention(db_session, test_user.id, "Montreal", ["seg-a", "seg-b"])
    await _mention(db_session, test_user.id, "OnlyInA", ["seg-a"])

    result = await segment_deletion.delete_segment_data("seg-a", test_user.id)

    db_session.expire_all()
    surviving = (await db_session.execute(select(Entity))).scalars().all()
    assert [e.name for e in surviving] == ["Montreal"], "the shared entity survives"
    assert result.entities_removed == 1, "only the one nothing mentions any more"


async def test_the_self_entity_survives_deleting_every_recording(
    db_session, test_user, patched, sd_session_factory
):
    """The producer's own entity has no mentions by construction — it is the
    family tree's root. A reset must not take it with everything else."""
    await _seed(db_session, test_user, seg_id="seg-a")
    db_session.add(
        Entity(
            producer_id=test_user.id, name="Test User", normalized_name="test user",
            type="person", is_self=True,
        )
    )
    await db_session.commit()

    await segment_deletion.delete_all_producer_recordings(test_user.id)

    db_session.expire_all()
    remaining = (await db_session.execute(select(Entity))).scalars().all()
    assert [e.name for e in remaining] == ["Test User"]


async def test_another_producers_entities_are_untouched(
    db_session, test_user, patched, sd_session_factory
):
    """The orphan sweep is scoped to one producer — the caller only knows that
    ITS producer's mentions are settled."""
    from app.api.v1.users import get_password_hash
    from app.models import User

    other = User(
        email="other@example.com", username="other",
        hashed_password=get_password_hash("x" * 12),
    )
    db_session.add(other)
    await db_session.commit()
    db_session.add(
        Entity(producer_id=other.id, name="Theirs", normalized_name="theirs", type="person")
    )
    await db_session.commit()

    await _seed(db_session, test_user, seg_id="seg-a")
    await segment_deletion.delete_segment_data("seg-a", test_user.id)

    db_session.expire_all()
    remaining = (await db_session.execute(select(Entity))).scalars().all()
    assert [e.name for e in remaining] == ["Theirs"]


async def test_missing_stored_file_does_not_fail_the_delete(
    db_session, test_user, monkeypatch, sd_session_factory
):
    await _seed(db_session, test_user, seg_id="seg-nofile")

    async def missing(key):
        raise FileNotFoundError(key)

    monkeypatch.setattr(segment_deletion.storage_service, "delete_file", missing)
    monkeypatch.setattr(segment_deletion, "_refresh_caches", AsyncMock())

    result = await segment_deletion.delete_segment_data("seg-nofile", test_user.id)
    assert result.segments_deleted == 1, "a missing file is fine — it's gone either way"


async def test_reset_deletes_every_recording_for_the_producer(
    db_session, test_user, patched, sd_session_factory
):
    for i in range(3):
        await _seed(db_session, test_user, seg_id=f"seg-r{i}", key=f"segments/x/{i}.webm")

    result = await segment_deletion.delete_all_producer_recordings(test_user.id)

    assert result.segments_deleted == 3
    assert len(patched["files"]) == 3


async def test_delete_unknown_segment_is_a_noop(patched, sd_session_factory):
    result = await segment_deletion.delete_segment_data("does-not-exist", "group")
    assert result.segments_deleted == 0


async def test_deleting_one_sibling_leaves_the_other(
    db_session, test_user, patched, sd_session_factory
):
    """1:many makes this a ROUTINE path, not an edge case: two recordings on
    the SAME question. Deleting one must never widen the blast radius to the
    question or the producer."""
    session = InterviewSession(id="int-sib", user_id=test_user.id, status="active")
    db_session.add(session)
    await db_session.flush()
    # SAME question_index — siblings, which the 1:many model now allows.
    for sid in ("sib-a", "sib-b"):
        db_session.add(RawSegment(
            id=sid, interview_session_id=session.id, question_asked="Same question",
            question_index=3, status="ready", video_key=f"segments/x/{sid}.webm",
        ))
    await db_session.commit()

    result = await segment_deletion.delete_segment_data("sib-a", test_user.id)

    assert result.segments_deleted == 1
    db_session.expire_all()
    surviving = (await db_session.execute(
        select(RawSegment).where(RawSegment.question_index == 3)
    )).scalars().all()
    assert [s.id for s in surviving] == ["sib-b"], "the sibling survives"
    assert surviving[0].video_key == "segments/x/sib-b.webm", "and is untouched"
