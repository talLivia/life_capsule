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
    # Prompt 14: producer-level /talk chat mode. Validated against
    # CHAT_MODES in app/api/v1/users.py rather than a Literal here, so the
    # 400 response can name the invalid value explicitly.
    chat_mode: Optional[str] = None


class UserResponse(UserBase):
    id: str
    is_active: bool
    role: str
    recording_language: str
    producer_id: Optional[str] = None
    chat_mode: str
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


class ExtractedEntityResponse(BaseModel):
    name: str
    summary: Optional[str] = None
    # Always null today: the graph stores one generic Entity label with no
    # person/place/organisation distinction. Present so typed entities can
    # land later without changing this shape. See segment_extraction.py.
    kind: Optional[str] = None

    model_config = {"from_attributes": True}


class SegmentExtractionResponse(BaseModel):
    """What the system understood from one recording — read-only, for the
    producer to check and catch a mistake. Deliberately says nothing about
    WHERE each piece is stored: entities are moving from Graphiti to
    Postgres and this contract must not move with them."""

    segment_id: str
    question_asked: str
    status: str
    transcript: Optional[str] = None
    topic_tags: List[str] = []
    unit_count: int = 0
    entities: List[ExtractedEntityResponse] = []
    still_processing: bool = False
    entities_unavailable: bool = False

    model_config = {"from_attributes": True}


class IdentityAnswer(BaseModel):
    """Whether a name in this recording is someone already in the archive."""

    same_as_existing: bool
    # Required when same_as_existing is true AND the question offered more
    # than one candidate. Validated against that question's own candidates —
    # a uuid from a different question is rejected, not silently applied.
    candidate_uuid: Optional[str] = None


class EntityBatchConfirmRequest(BaseModel):
    """Every answer for ONE recording, submitted together.

    Replaces the per-name request that came before it. The old shape carried a
    single `entity_name` and answered one question, so a recording with three
    ambiguities took three round trips and three modals — and each was decided
    without the producer seeing the others.

    Both maps are keyed by ENTITY NAME as it appears in the pending payload,
    which is also the staleness check: a name that is not one of this
    segment's live questions is rejected rather than ignored, so answering a
    screen the pipeline has moved past fails loudly.
    """

    identity: Dict[str, IdentityAnswer] = Field(default_factory=dict)
    # entity name -> the chosen type. Must be one of exactly the two the
    # question offered (its `type` or its `alternative_type`); anything else
    # is a 400, since a third value could only come from a client inventing
    # one and would land in a column with a CHECK constraint on it.
    types: Dict[str, str] = Field(default_factory=dict)


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


# Family access Schemas (Prompt 9)
class FamilyInviteResponse(BaseModel):
    id: str
    token: str
    status: str
    redeemed_by_user_id: Optional[str] = None
    created_at: datetime
    expires_at: datetime
    redeemed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class FamilyInviteRedeemRequest(BaseModel):
    token: str = Field(..., min_length=1)


class TalkAvailabilityResponse(BaseModel):
    producer_id: str
    producer_name: str
    available: bool
    ready_segment_count: int
    avatar_id: Optional[str] = None
    avatar_image_url: Optional[str] = None
    # Prompt 14: which chat component /talk should render — the PRODUCER's
    # own setting, never the family viewer's (there is no such thing; see
    # User.chat_mode's docstring in app/models.py).
    chat_mode: str = "avatar"


# Internal GPU-inference Schemas (Prompt 9) — /internal/gpu/*, never called
# by the frontend; see app/services/gpu_client.py.
class GpuTranscribeRequest(BaseModel):
    audio_b64: str
    language: str = "en"


class GpuTranscribeResponse(BaseModel):
    text: str


class GpuSynthesizeRequest(BaseModel):
    text: str
    language: str = "en"
    speaker_wav_b64: Optional[str] = None


class GpuSynthesizeResponse(BaseModel):
    audio_b64: str
    engine: str
    fallback: bool
    voice_cloned: bool


class GpuAnimateRequest(BaseModel):
    avatar_image_b64: str
    audio_b64: str


class GpuAnimateResponse(BaseModel):
    video_b64: str
