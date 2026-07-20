from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, Field


# User Schemas
class UserBase(BaseModel):
    email: EmailStr
    username: str
    full_name: Optional[str] = None


class UserCreate(UserBase):
    password: str


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    username: Optional[str] = None
    full_name: Optional[str] = None
    password: Optional[str] = None


class UserResponse(UserBase):
    id: str
    is_active: bool
    role: str
    recording_language: str
    created_at: datetime

    model_config = {"from_attributes": True}


# Avatar Schemas
class AvatarBase(BaseModel):
    name: str


class AvatarResponse(AvatarBase):
    id: str
    user_id: str
    image_url: str
    thumbnail_url: Optional[str] = None
    status: str
    voice_id: Optional[str] = None
    avatar_metadata: Optional[Dict[str, Any]] = Field(None, alias="avatar_metadata")
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


# Session Schemas
class SessionCreate(BaseModel):
    avatar_id: str
    settings: Optional[Dict[str, Any]] = None


class SessionResponse(BaseModel):
    id: str
    user_id: str
    avatar_id: str
    status: str
    settings: Optional[Dict[str, Any]] = None
    started_at: datetime
    ended_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# Message Schemas
class MessageBase(BaseModel):
    content: str
    content_type: str = "text"


class MessageCreate(MessageBase):
    session_id: str


class MessageResponse(MessageBase):
    id: str
    session_id: str
    role: str
    audio_url: Optional[str] = None
    video_url: Optional[str] = None
    message_metadata: Optional[Dict[str, Any]] = Field(None, alias="message_metadata")
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


# Conversation Schemas
class ConversationResponse(BaseModel):
    id: str
    session_id: str
    title: Optional[str] = None
    summary: Optional[str] = None
    message_count: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class AvatarRename(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class AvatarMetadataUpdate(BaseModel):
    """Allowed editable metadata fields for an avatar.

    Restrict to a known allowlist so users cannot stuff arbitrary keys into
    the JSON column (which would otherwise let them shadow internal flags or
    bloat the row).

    NOTE: `system_prompt`/`personality` (free-chat persona knobs from the
    base project) were removed in Prompt 1 — this system never lets a user
    hand the model an arbitrary persona to answer from general knowledge.
    Only visual customization remains here.
    """

    background_color: Optional[str] = Field(default=None, max_length=32)
    animation_style: Optional[str] = Field(default=None, max_length=32)

    model_config = {"extra": "forbid"}


# Token Schema
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    user_id: Optional[str] = None


# Interview Schemas (Prompt 4 — guided-interview recording flow)
class InterviewQuestion(BaseModel):
    id: str
    category: str
    category_label: str
    text: str
    index: int


class InterviewSessionResponse(BaseModel):
    id: str
    user_id: str
    status: str
    current_question_index: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class RawSegmentResponse(BaseModel):
    id: str
    interview_session_id: str
    question_asked: str
    question_index: int
    video_url: Optional[str] = None
    video_key: Optional[str] = None
    transcript: Optional[str] = None
    topic_tags: Optional[List[str]] = None
    importance_score: Optional[float] = None
    pending_confirmation: Optional[Dict[str, Any]] = None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class EntityConfirmRequest(BaseModel):
    """Answer to the currently-pending human_confirm question for a segment.
    `entity_name` must match the segment's live pending_confirmation payload —
    a safety check against confirming a stale/wrong question if the pipeline
    already advanced to the next ambiguous name."""

    entity_name: str = Field(..., min_length=1)
    same_as_existing: bool
    candidate_uuid: Optional[str] = None


class PendingConfirmationResponse(BaseModel):
    segment_id: str
    interview_session_id: str
    question_asked: str
    pending_confirmation: Dict[str, Any]


class InterviewSessionState(BaseModel):
    """Everything the /record page needs to render or resume: the session,
    the fixed question list for the producer's recording_language, and any
    segments already recorded this session (so already-answered questions
    show as complete and can be re-recorded instead of re-asked blank)."""

    session: InterviewSessionResponse
    questions: List[InterviewQuestion]
    segments: List[RawSegmentResponse]


class InterviewSessionUpdate(BaseModel):
    current_question_index: int = Field(..., ge=0)


class SegmentPresignRequest(BaseModel):
    question_index: int = Field(..., ge=0)
    content_type: str = Field(default="video/webm", max_length=100)


class SegmentPresignResponse(BaseModel):
    upload_url: str
    video_key: str
    method: str = "PUT"
    content_type: str


class SegmentIngestRequest(BaseModel):
    interview_session_id: str
    question_index: int = Field(..., ge=0)
    question_asked: str = Field(..., min_length=1, max_length=2000)
    video_key: str = Field(..., min_length=1)
