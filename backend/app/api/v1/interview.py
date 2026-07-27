import logging
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.users import require_current_user
from app.config import settings
from app.database import get_db
from app.interview_config import get_questions
from app.models import InterviewSession, RawSegment, User
from app.schemas import (
    EntityConfirmRequest,
    InterviewQuestion,
    InterviewSessionResponse,
    InterviewSessionState,
    InterviewSessionUpdate,
    PendingConfirmationResponse,
    RawSegmentResponse,
    SegmentIngestRequest,
    SegmentPresignRequest,
    SegmentPresignResponse,
)
from app.services.segment_deletion import delete_segment_data
from app.services.storage import storage_service

logger = logging.getLogger(__name__)
router = APIRouter()


async def require_producer(user: User = Depends(require_current_user)) -> User:
    """/record is producer-only — family accounts (Prompt 9) get /talk, never this."""
    if user.role != "producer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the producer/owner account can record interview segments",
        )
    return user


def _questions_with_index(language: str) -> List[InterviewQuestion]:
    return [
        InterviewQuestion(index=i, **q) for i, q in enumerate(get_questions(language))
    ]


async def _get_or_create_session(db: AsyncSession, user: User) -> InterviewSession:
    """The producer's single in-progress interview pass. Resumability
    (Prompt 4's "browser refresh mid-interview resumes at the right
    question" requirement) works by always returning the same active row
    instead of creating a new one per page load."""
    result = await db.execute(
        select(InterviewSession)
        .where(InterviewSession.user_id == user.id, InterviewSession.status == "active")
        .order_by(InterviewSession.created_at.desc())
    )
    session = result.scalars().first()
    if session is not None:
        return session

    session = InterviewSession(user_id=user.id, status="active", current_question_index=0)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


@router.get("/questions", response_model=List[InterviewQuestion])
async def list_questions(user: User = Depends(require_producer)):
    """Fixed guided-interview question sequence, in the producer's recording_language."""
    return _questions_with_index(user.recording_language)


@router.get("/session", response_model=InterviewSessionState)
async def get_interview_session(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """Get-or-create the producer's active interview session, plus the
    question list and any segments already recorded this session — enough
    for the frontend to resume exactly where it left off."""
    session = await _get_or_create_session(db, user)

    result = await db.execute(
        select(RawSegment)
        .where(RawSegment.interview_session_id == session.id)
        .order_by(RawSegment.question_index)
    )
    segments = result.scalars().all()

    return InterviewSessionState(
        session=session,
        questions=_questions_with_index(user.recording_language),
        segments=segments,
    )


@router.patch("/session/{session_id}", response_model=InterviewSessionResponse)
async def update_interview_session(
    session_id: str,
    payload: InterviewSessionUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """Move the current-question pointer (next/back navigation)."""
    result = await db.execute(
        select(InterviewSession).where(InterviewSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Interview session not found")
    if session.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorised to modify this session")

    max_index = max(len(get_questions(user.recording_language)) - 1, 0)
    if payload.current_question_index > max_index:
        raise HTTPException(status_code=400, detail="Question index out of range")

    session.current_question_index = payload.current_question_index
    await db.commit()
    await db.refresh(session)
    return session


_EXT_BY_CONTENT_TYPE = {
    "video/webm": "webm",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
}


def _segment_video_key(
    user_id: str, interview_session_id: str, question_index: int, content_type: str
) -> str:
    # A fresh uuid per upload (rather than a fixed slot name) so a
    # re-record never overwrites an object still referenced by an
    # in-flight presigned GET/transcription job for the previous take.
    base_type = content_type.split(";", 1)[0].strip().lower()
    ext = _EXT_BY_CONTENT_TYPE.get(base_type, "webm")
    return f"segments/{user_id}/{interview_session_id}/{question_index}/{uuid.uuid4()}.{ext}"


@router.post("/segments/presign", response_model=SegmentPresignResponse)
async def presign_segment_upload(
    payload: SegmentPresignRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """
    Presigned upload target for a recorded take. In R2 (or S3) mode this is a
    real presigned PUT straight to object storage — the video never passes
    through this backend. In local-storage dev mode there's no such thing as
    a presigned URL for the filesystem, so we hand back our own PUT endpoint
    below, which behaves the same way from the frontend's point of view.
    """
    session = await _get_or_create_session(db, user)
    video_key = _segment_video_key(
        user.id, session.id, payload.question_index, payload.content_type
    )

    if getattr(settings, "USE_LOCAL_STORAGE", True):
        upload_url = f"{settings.BACKEND_URL}/api/v1/interview/segments/upload-local/{video_key}"
    else:
        upload_url = await storage_service.presigned_upload_url(
            video_key, content_type=payload.content_type
        )

    return SegmentPresignResponse(
        upload_url=upload_url, video_key=video_key, content_type=payload.content_type
    )


@router.put("/segments/upload-local/{video_key:path}", status_code=status.HTTP_204_NO_CONTENT)
async def upload_local_segment(
    video_key: str,
    request: Request,
    user: User = Depends(require_producer),
):
    """
    Local-storage-only counterpart to a presigned PUT — exists so /record
    works against `docker compose up` local dev without R2 credentials.
    Only reachable in USE_LOCAL_STORAGE mode; the key must be one this user
    was actually issued (prevents writing into another producer's segments
    or escaping the segments/ prefix).
    """
    if not getattr(settings, "USE_LOCAL_STORAGE", True):
        raise HTTPException(status_code=404, detail="Not found")
    if not video_key.startswith(f"segments/{user.id}/"):
        raise HTTPException(status_code=403, detail="Not authorised to write this key")

    content_type = request.headers.get("content-type", "video/webm")
    body = await request.body()
    await storage_service.upload_file(body, video_key, content_type=content_type)


@router.post("/segments/ingest", response_model=RawSegmentResponse, status_code=status.HTTP_201_CREATED)
async def ingest_segment(
    payload: SegmentIngestRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """
    Accept a recorded-and-uploaded take: persist the segment row and enqueue
    transcription.

    Re-recording REPLACES: the previous take for that (session,
    question_index) is fully deleted first — row, chunks, stored video and
    Graphiti episode — and a fresh row is created. This used to upsert the
    row in place, which quietly left the old stored video orphaned (its key
    was overwritten) and the old episode in the graph.

    Deleting BEFORE the pipeline runs also fixes an ordering bug: entity
    resolution (check_entities) matches new names against the producer's
    existing graph, and if the old episode were still present at that point
    the resolved entity could be deleted moments later by the replacement,
    leaving the extraction instruction pointing at a dead uuid — which is
    exactly how "מונטריאול" ended up as two separate nodes.
    """
    result = await db.execute(
        select(InterviewSession).where(InterviewSession.id == payload.interview_session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Interview session not found")
    if session.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorised to modify this session")

    if not payload.video_key.startswith(f"segments/{user.id}/"):
        raise HTTPException(status_code=400, detail="video_key does not belong to this session")

    video_url = storage_service.get_url(payload.video_key)

    existing_result = await db.execute(
        select(RawSegment).where(
            RawSegment.interview_session_id == session.id,
            RawSegment.question_index == payload.question_index,
        )
    )
    previous = existing_result.scalar_one_or_none()

    if previous is not None:
        # Fully remove the old take before creating the new one. Cache warming
        # is skipped here: the replacement is about to be ingested, and
        # ingestion warms the cache itself once it completes.
        await db.commit()  # release our hold before the deleter opens its own session
        await delete_segment_data(previous.id, session.user_id, warm_cache=False)

    segment = RawSegment(
        interview_session_id=session.id,
        question_asked=payload.question_asked,
        question_index=payload.question_index,
        video_url=video_url,
        video_key=payload.video_key,
        status="pending_transcription",
    )
    db.add(segment)

    await db.commit()
    await db.refresh(segment)

    if settings.DEBUG:
        # Local dev convenience only (never runs in production, where
        # DEBUG=false and a real Celery worker/broker are expected): the
        # common local setup has no Redis/Celery worker running at all.
        # Deliberately skip attempting analyze_segment_task.delay() rather
        # than try-it-then-fall-back-on-exception - measured directly
        # against an unreachable local broker, Kombu's connection/retry
        # behavior doesn't fail fast, it hangs for well over axios's 30s
        # client-side timeout before ever raising. Go straight to running
        # the same pipeline in-process instead.
        import asyncio

        from app.analysis_graph import run_segment_analysis

        asyncio.create_task(run_segment_analysis(segment.id))
    else:
        try:
            from app.celery_app import analyze_segment_task

            analyze_segment_task.delay(segment.id)
        except Exception as e:
            # Segment is safely persisted either way; a down broker just
            # means analysis is delayed rather than the upload failing.
            logger.warning(f"Could not enqueue analysis for segment {segment.id}: {e}")

    logger.info(f"Segment ingested: {segment.id} (session={session.id}, q={payload.question_index})")
    return segment


@router.get("/segments/session/{session_id}", response_model=List[RawSegmentResponse])
async def list_session_segments(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    result = await db.execute(
        select(InterviewSession).where(InterviewSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Interview session not found")
    if session.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorised to access this session")

    result = await db.execute(
        select(RawSegment)
        .where(RawSegment.interview_session_id == session_id)
        .order_by(RawSegment.question_index)
    )
    return result.scalars().all()


@router.get("/segments/pending-confirmations", response_model=List[PendingConfirmationResponse])
async def list_pending_confirmations(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """
    Segments currently paused on a human_confirm question (Prompt 5), for
    this producer only. Reads the lightweight copy of the live LangGraph
    interrupt payload that analysis_graph.py mirrors onto the segment row
    itself, so polling this never has to touch the checkpointer.
    """
    result = await db.execute(
        select(RawSegment, InterviewSession)
        .join(InterviewSession, RawSegment.interview_session_id == InterviewSession.id)
        .where(InterviewSession.user_id == user.id, RawSegment.status == "pending_confirmation")
        .order_by(RawSegment.updated_at)
    )
    return [
        PendingConfirmationResponse(
            segment_id=segment.id,
            interview_session_id=segment.interview_session_id,
            question_asked=segment.question_asked,
            pending_confirmation=segment.pending_confirmation or {},
        )
        for segment, _session in result.all()
    ]


@router.post("/segments/{segment_id}/confirm-entity", response_model=RawSegmentResponse)
async def confirm_entity(
    segment_id: str,
    payload: EntityConfirmRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """
    Answer the currently-pending human_confirm question for a segment,
    resuming analysis_graph.py exactly where it paused (LangGraph's
    Postgres-backed checkpoint). If the segment has more than one ambiguous
    entity, resuming answers only the CURRENT one — the graph will pause
    again with the next question, and the frontend's poll of
    pending-confirmations will pick that up.
    """
    result = await db.execute(
        select(RawSegment, InterviewSession)
        .join(InterviewSession, RawSegment.interview_session_id == InterviewSession.id)
        .where(RawSegment.id == segment_id)
    )
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Segment not found")
    segment, session = row
    if session.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorised to confirm this segment")
    if segment.status != "pending_confirmation" or not segment.pending_confirmation:
        raise HTTPException(status_code=409, detail="This segment has no pending confirmation")
    if segment.pending_confirmation.get("entity_name") != payload.entity_name:
        raise HTTPException(
            status_code=409,
            detail="The pending question has moved on — refresh pending-confirmations",
        )

    candidates = segment.pending_confirmation.get("candidates") or []
    candidate_uuid = payload.candidate_uuid
    if payload.same_as_existing:
        if not candidate_uuid and len(candidates) == 1:
            # Only one plausible match was ever shown — a bare "yes" is
            # unambiguous. With 2+ candidates the caller must say which one.
            candidate_uuid = candidates[0]["uuid"]
        if not candidate_uuid or candidate_uuid not in {c["uuid"] for c in candidates}:
            raise HTTPException(
                status_code=400,
                detail="candidate_uuid must be one of the pending question's candidates",
            )

    from app.analysis_graph import resume_segment_analysis

    await resume_segment_analysis(
        segment_id,
        {"same_as_existing": payload.same_as_existing, "candidate_uuid": candidate_uuid},
    )

    await db.refresh(segment)
    return segment
