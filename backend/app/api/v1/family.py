"""
Family access control (Prompt 9) — a producer issues an invite token, a
family member redeems it to link their account (User.producer_id) to that
producer's archive, unlocking /talk. Deliberately simple: one producer per
family account, no multi-producer sharing — matches this POC's
single-storyteller-per-deployment design — the same assumption
`entity_store` makes by scoping every entity to one producer_id.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.users import require_current_user
from app.database import get_db
from app.models import Avatar, FamilyInvite, InterviewSession, RawSegment, User
from app.schemas import (
    FamilyInviteRedeemRequest,
    FamilyInviteResponse,
    TalkAvailabilityResponse,
    UserResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()

INVITE_TTL_DAYS = 7


async def require_producer(user: User = Depends(require_current_user)) -> User:
    """Shared with interview.py's dependency in spirit, but kept local here
    (a small duplication) rather than a cross-import — this router's own
    producer-only endpoints (invite create/list/revoke) shouldn't couple to
    interview.py's module for an unrelated concern."""
    if user.role != "producer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the producer/owner account can do this",
        )
    return user


async def require_family(user: User = Depends(require_current_user)) -> User:
    if user.role != "family" or not user.producer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not linked to a storyteller's archive yet",
        )
    return user


def _generate_token() -> str:
    return secrets.token_urlsafe(24)


@router.post("/invites", response_model=FamilyInviteResponse, status_code=status.HTTP_201_CREATED)
async def create_invite(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    invite = FamilyInvite(
        producer_id=user.id,
        token=_generate_token(),
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(days=INVITE_TTL_DAYS),
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)
    logger.info(f"Family invite created: {invite.id} (producer={user.id})")
    return invite


@router.get("/invites", response_model=List[FamilyInviteResponse])
async def list_invites(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    result = await db.execute(
        select(FamilyInvite)
        .where(FamilyInvite.producer_id == user.id)
        .order_by(FamilyInvite.created_at.desc())
    )
    return result.scalars().all()


@router.delete("/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    invite_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_producer),
):
    result = await db.execute(select(FamilyInvite).where(FamilyInvite.id == invite_id))
    invite = result.scalar_one_or_none()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.producer_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorised to revoke this invite")
    if invite.status != "pending":
        raise HTTPException(status_code=409, detail=f"Invite is already {invite.status}")

    invite.status = "revoked"
    await db.commit()
    logger.info(f"Family invite revoked: {invite.id}")


@router.post("/invites/redeem", response_model=UserResponse)
async def redeem_invite(
    payload: FamilyInviteRedeemRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_current_user),
):
    """Any authenticated user redeems a token to link their account to the
    issuing producer's archive.

    Every fresh registration defaults to role="producer" (Prompt 4) — that's
    just "hasn't done anything yet", not a deliberate choice — so redemption
    can't reject on role=="producer" alone, or nobody could ever redeem an
    invite with a brand-new account. Instead it blocks only an account that
    has ALREADY recorded content of its own (owns an interview_session):
    that's a real storyteller, and shouldn't be silently repurposed as a
    family viewer of someone else's archive in this single-archive-per-
    account POC.
    """
    result = await db.execute(select(FamilyInvite).where(FamilyInvite.token == payload.token))
    invite = result.scalar_one_or_none()
    if not invite:
        raise HTTPException(status_code=404, detail="Invalid invite code")

    now = datetime.now(timezone.utc)
    if invite.status != "pending":
        raise HTTPException(
            status_code=409, detail=f"This invite has already been {invite.status}"
        )
    expires_at = invite.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise HTTPException(status_code=410, detail="This invite has expired")
    if invite.producer_id == user.id:
        raise HTTPException(status_code=400, detail="You cannot redeem your own invite")

    if user.role == "producer":
        has_content = await db.execute(
            select(InterviewSession.id).where(InterviewSession.user_id == user.id).limit(1)
        )
        if has_content.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This account already has recorded story content and cannot "
                    "become a family member of another archive"
                ),
            )

    user.role = "family"
    user.producer_id = invite.producer_id
    invite.status = "redeemed"
    invite.redeemed_by_user_id = user.id
    invite.redeemed_at = now

    await db.commit()
    await db.refresh(user)
    logger.info(f"Family invite redeemed: {invite.id} by user={user.id}")
    return user


@router.get("/talk-availability", response_model=TalkAvailabilityResponse)
async def talk_availability(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_family),
):
    """Whether /talk should actually be usable yet: the linked producer must
    have at least one 'ready' segment — and, ONLY when their chat mode is
    'avatar', a ready avatar image for MuseTalk to animate. v2 plays the
    producer's real recorded footage and needs no avatar at all
    (docs/V2_PRIMARY_AVATAR_DORMANT_PLAN.md §3.4). Never exposes
    thresholds/scores from Prompts 6-8 — just a plain yes/no plus enough to
    start a session."""
    result = await db.execute(select(User).where(User.id == user.producer_id))
    producer = result.scalar_one_or_none()
    if not producer:
        raise HTTPException(status_code=404, detail="Linked storyteller account not found")

    count_result = await db.execute(
        select(func.count())
        .select_from(RawSegment)
        .join(InterviewSession, RawSegment.interview_session_id == InterviewSession.id)
        .where(InterviewSession.user_id == producer.id, RawSegment.status == "ready")
    )
    ready_count = int(count_result.scalar() or 0)

    avatar_result = await db.execute(
        select(Avatar)
        .where(Avatar.user_id == producer.id, Avatar.status == "ready")
        .order_by(Avatar.created_at.desc())
        .limit(1)
    )
    avatar = avatar_result.scalar_one_or_none()

    # An avatar gates availability only where one is actually rendered.
    avatar_needed = producer.chat_mode == "avatar"

    return TalkAvailabilityResponse(
        producer_id=producer.id,
        producer_name=producer.full_name or producer.username,
        available=ready_count > 0 and (avatar is not None or not avatar_needed),
        ready_segment_count=ready_count,
        avatar_id=avatar.id if avatar else None,
        # Family accounts don't own this avatar, so avatars.py's own
        # ownership-gated endpoints (list/get) aren't reachable for them —
        # hand back the image URL directly here instead of relaxing that
        # unrelated permission model just for an idle-state preview image.
        avatar_image_url=avatar.image_url if avatar else None,
        chat_mode=producer.chat_mode,
    )
