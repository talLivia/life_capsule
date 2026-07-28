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
    EntityBatchConfirmRequest,
    InterviewQuestion,
    InterviewSessionResponse,
    InterviewSessionState,
    InterviewSessionUpdate,
    PendingConfirmationResponse,
    RawSegmentResponse,
    SegmentIngestRequest,
    SegmentExtractionResponse,
    SegmentPresignRequest,
    SegmentPresignResponse,
)
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


def _validated_content_type(content_type: str) -> str:
    """The base content type, or 400.

    Recording only ever produces what MediaRecorder negotiated, so this was
    never load-bearing. UPLOADING lets the producer hand over any file at
    all, and the fallback below used to name anything unrecognised `.webm`
    — so a PDF became `segments/.../x.webm` and only revealed itself as a
    decode failure deep inside transcription, minutes later and nowhere near
    the file picker. Reject it at the door instead, where the message can
    name the actual problem.

    The list is what the whole downstream chain already handles: PyAV
    decodes any of them from raw bytes, Deepgram sniffs the container
    itself, and ffmpeg trims all three during clip assembly.
    """
    base_type = content_type.split(";", 1)[0].strip().lower()
    if base_type not in _EXT_BY_CONTENT_TYPE:
        raise HTTPException(
            status_code=400,
            detail=(
                "That file type isn't supported — please upload a video "
                "(.webm, .mp4 or .mov)"
            ),
        )
    return base_type


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
    Presigned upload target for a take — whether it was recorded in the
    browser or picked from disk. Both go through here, then the same PUT,
    then the same /segments/ingest: an uploaded video is not a second kind
    of recording and gets no second pipeline.

    In R2 (or S3) mode this is a real presigned PUT straight to object
    storage — the video never passes through this backend. In local-storage
    dev mode there's no such thing as a presigned URL for the filesystem, so
    we hand back our own PUT endpoint below, which behaves the same way from
    the frontend's point of view.
    """
    content_type = _validated_content_type(payload.content_type)
    session = await _get_or_create_session(db, user)
    video_key = _segment_video_key(user.id, session.id, payload.question_index, content_type)

    if getattr(settings, "USE_LOCAL_STORAGE", True):
        upload_url = f"{settings.BACKEND_URL}/api/v1/interview/segments/upload-local/{video_key}"
    else:
        upload_url = await storage_service.presigned_upload_url(
            video_key, content_type=content_type
        )

    return SegmentPresignResponse(
        upload_url=upload_url, video_key=video_key, content_type=content_type
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

    SIZE ENFORCEMENT IS ASYMMETRIC, deliberately. Here the bytes pass through
    us, so the cap is real. In R2 mode they do not — the browser PUTs
    straight to object storage, and a presigned URL carries no size condition
    (only a POST policy could, which is a different upload shape). There the
    client-side check is the only guard, and it is a guard against picking
    the wrong file, not against a determined caller. Saying so here beats a
    comment claiming a limit that only holds in dev.
    """
    if not getattr(settings, "USE_LOCAL_STORAGE", True):
        raise HTTPException(status_code=404, detail="Not found")
    if not video_key.startswith(f"segments/{user.id}/"):
        raise HTTPException(status_code=403, detail="Not authorised to write this key")

    max_bytes = settings.MAX_SEGMENT_UPLOAD_BYTES
    # Checked BEFORE reading the body: on an oversized upload this refuses
    # without first buffering the whole thing into memory.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"That video is too large (max {max_bytes // (1024 * 1024)} MB)",
        )

    content_type = _validated_content_type(
        request.headers.get("content-type", "video/webm")
    )
    body = await request.body()
    # Content-Length can be absent (chunked) or simply wrong, so the real
    # bytes are checked too — otherwise the header check is advisory.
    if len(body) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"That video is too large (max {max_bytes // (1024 * 1024)} MB)",
        )
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

    APPENDS. One question can have SEVERAL recordings — a storyteller often
    remembers something later, or wants to add to an answer without losing
    what they already said. This used to upsert (and briefly, to replace), so
    a second take destroyed the first.

    Replacing a specific take is therefore delete + add, not a separate flow:
    DELETE /segments/{id} removes one recording and everything derived from
    it, then this endpoint adds the new one. Nothing here deletes.
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

    # No lookup of an "existing" segment: several are legitimate now. The old
    # code used scalar_one_or_none() here, which RAISES on a second row — so
    # this had to change in the same commit that allows siblings, not after.
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


@router.get("/segments/{segment_id}/extraction", response_model=SegmentExtractionResponse)
async def get_segment_extraction(
    segment_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """What the system understood from this recording — entities, topic tags,
    how many utterance units it was split into, and the transcript.

    Transparency, so a mishearing or a missed person is caught here rather
    than later through a bad answer. Read-only.

    All assembly lives in segment_extraction, which is also the only place
    that knows entities currently come from Graphiti. This endpoint stays
    valid when they move to Postgres.
    """
    from app.services.segment_extraction import get_segment_extraction as load

    extraction = await load(db, segment_id, user.id)
    if extraction is None:
        # Same 404-for-both as delete: distinguishing "not yours" from
        # "doesn't exist" would confirm another producer's segment id.
        raise HTTPException(status_code=404, detail="Recording not found")
    return extraction


@router.delete("/segments/{segment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_segment(
    segment_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """Delete ONE recording, leaving any siblings on the same question alone.

    Now that a question holds several takes, "replace this one" is expressed
    as delete + record rather than being implied by ingesting again — the
    producer says which take goes, instead of the newest silently destroying
    the previous one.

    The work itself is delegated to segment_deletion, the same implementation
    the account-reset path uses: row, chunks, stored file, Graphiti episode
    and derived caches. Ownership is checked by joining through the session,
    so a producer can only ever reach their own recordings.
    """
    from app.services.segment_deletion import delete_segment_data

    owned = (
        await db.execute(
            select(RawSegment.id)
            .join(InterviewSession, RawSegment.interview_session_id == InterviewSession.id)
            .where(RawSegment.id == segment_id, InterviewSession.user_id == user.id)
        )
    ).scalar_one_or_none()
    if owned is None:
        # 404 for both "no such segment" and "not yours" — telling the caller
        # which would confirm the existence of another producer's recording.
        raise HTTPException(status_code=404, detail="Recording not found")

    result = await delete_segment_data(segment_id, user.id)
    if not result.ok:
        # The row may well be gone while the graph cleanup failed. Say so
        # rather than reporting a clean success over a half-finished delete.
        logger.error(f"Incomplete deletion of {segment_id}: {result.failures}")
        raise HTTPException(
            status_code=500,
            detail="The recording was only partly deleted — please try again",
        )
    logger.info(f"Segment deleted: {segment_id} (producer={user.id})")


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


@router.post("/segments/{segment_id}/confirm-entities", response_model=RawSegmentResponse)
async def confirm_entities(
    segment_id: str,
    payload: EntityBatchConfirmRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    """
    Answer EVERY pending question for a segment at once, resuming
    analysis_graph.py exactly where it paused (LangGraph's Postgres-backed
    checkpoint). One submit, one resume, and the pipeline runs to completion —
    it never pauses again for the same recording.

    Replaces the per-name `confirm-entity` endpoint. That one answered a
    single question and let the graph pause again with the next, so a
    recording with three ambiguities meant three round trips.

    EVERY question must be answered. Partial submission is rejected rather
    than defaulted, because the two plausible defaults are both wrong: taking
    "same as existing" would silently merge two people, and taking "someone
    new" would silently split one. The producer is looking at the whole screen
    — the answer to ask for is all of it.
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

    pending = segment.pending_confirmation
    identity_questions = {q["name"]: q for q in pending.get("identity_questions") or []}
    type_questions = {q["name"]: q for q in pending.get("type_questions") or []}

    # Staleness, both directions. An answer to a name this screen never asked
    # about means the client is answering a payload the pipeline has moved
    # past; a missing answer means the screen was submitted incomplete.
    unknown = (set(payload.identity) - set(identity_questions)) | (
        set(payload.types) - set(type_questions)
    )
    if unknown:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Not questions for this recording: {sorted(unknown)} — "
                "refresh pending-confirmations"
            ),
        )
    missing = (set(identity_questions) - set(payload.identity)) | (
        set(type_questions) - set(payload.types)
    )
    if missing:
        raise HTTPException(
            status_code=400, detail=f"Every question must be answered; missing: {sorted(missing)}"
        )

    identity: dict = {}
    for name, answer in payload.identity.items():
        candidates = identity_questions[name].get("candidates") or []
        candidate_uuid = answer.candidate_uuid
        if answer.same_as_existing:
            if not candidate_uuid and len(candidates) == 1:
                # Only one plausible match was ever shown — a bare "yes" is
                # unambiguous. With 2+ candidates the caller must say which.
                candidate_uuid = candidates[0]["uuid"]
            if not candidate_uuid or candidate_uuid not in {c["uuid"] for c in candidates}:
                raise HTTPException(
                    status_code=400,
                    detail=f'candidate_uuid for "{name}" must be one of its own candidates',
                )
        identity[name] = {
            "same_as_existing": answer.same_as_existing,
            "candidate_uuid": candidate_uuid,
        }

    for name, chosen in payload.types.items():
        question = type_questions[name]
        allowed = {question["type"], question["alternative_type"]}
        if chosen not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f'type for "{name}" must be one of {sorted(allowed)}',
            )

    from app.analysis_graph import resume_segment_analysis

    await resume_segment_analysis(
        segment_id, {"identity": identity, "types": dict(payload.types)}
    )

    await db.refresh(segment)
    return segment
