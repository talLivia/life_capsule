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
    # Unlocks /record's accordion so any category can be opened out of order.
    # See docs/INTERVIEW_RESTRUCTURE.md §7A.
    free_navigation: Optional[bool] = None


class UserResponse(UserBase):
    id: str
    is_active: bool
    role: str
    recording_language: str
    producer_id: Optional[str] = None
    chat_mode: str
    free_navigation: bool
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


# ── Flow (docs/INTERVIEW_RESTRUCTURE.md step 4) ──────────────────────────
# The accordion renders this and recomputes none of it. Position, reachability
# and completeness are all DERIVED server-side from the question set, the gate
# answers and the recordings — there is no stored cursor to drift.


class GateOption(BaseModel):
    value: str
    label: str


class FlowStep(BaseModel):
    kind: str  # "question" | "gate"
    id: str
    text: str
    done: bool
    # question steps only — a question can hold several takes
    takes: Optional[int] = None
    # gate steps only. `options` is data-driven: the client renders one control
    # per option and must never assume a yes/no pair.
    options: Optional[List[GateOption]] = None
    answer: Optional[str] = None


class FlowCategory(BaseModel):
    id: str
    label: str
    # Only the REACHABLE steps: an unanswered gate ends the list, because
    # nothing behind it is knowable yet.
    steps: List[FlowStep]
    # True when no reachable gate is still unanswered, i.e. the category's
    # shape is known. Distinct from `complete` — a settled category can still
    # have questions left to record.
    settled: bool
    # 1-based position of the step being worked on; None when complete.
    position: Optional[int] = None
    # Total steps in the category, INCLUDING gate steps. None until `settled`,
    # because the count genuinely depends on an answer not yet given — the UI
    # must not substitute a guess (§8.4).
    total: Optional[int] = None
    done_count: int
    current_step_id: Optional[str] = None
    complete: bool
    current: bool
    # Whether the accordion may open it. Completed categories reopen for
    # review; everything past the current one is inert unless free navigation
    # is on. Decided here, not by hiding a click handler.
    reachable: bool


class InterviewFlow(BaseModel):
    interview_session_id: str
    free_navigation: bool
    current_category_id: Optional[str] = None
    complete: bool
    categories: List[FlowCategory]


class GateAnswerRequest(BaseModel):
    gate_id: str = Field(..., min_length=1, max_length=200)
    # Validated against the gate's own options in services/gate_answers.py —
    # not a Literal here, because the vocabulary lives in the question file and
    # a new option must never require a code change.
    value: str = Field(..., min_length=1, max_length=200)


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
    # The stable question id (migration 0013). The accordion groups takes by
    # THIS, not by question_index, which is positional and moves when the
    # question set is edited. Nullable for uploads outside the guided set.
    question_id: Optional[str] = None
    video_url: Optional[str] = None
    video_key: Optional[str] = None
    transcript: Optional[str] = None
    topic_tags: Optional[List[str]] = None
    importance_score: Optional[float] = None
    pending_confirmation: Optional[Dict[str, Any]] = None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AppliedTypeChange(BaseModel):
    """An entity type the producer's answer changed. Returned so the
    confirmation visibly took effect — an answer that silently does nothing is
    worse than not asking (observed live on הכפר הירוק)."""

    name: str
    was: str
    now: str


class RejectedYear(BaseModel):
    """A year the producer typed that could not be resolved to one number.

    Returned rather than swallowed: silently dropping it would repeat the
    type-answer bug, and silently GUESSING at it would put a wrong date on a
    life story where nothing would look broken."""

    name: str
    given: str
    reason: str


class ConfirmEntitiesResponse(BaseModel):
    segment: RawSegmentResponse
    applied_type_changes: List[AppliedTypeChange] = Field(default_factory=list)
    rejected_years: List[RejectedYear] = Field(default_factory=list)


# ── Family tree (docs/FAMILY_TREE_TIMELINE.md Phase 4) ───────────────────


class TreePerson(BaseModel):
    id: str
    name: str
    is_self: bool
    year_start: Optional[int] = None
    year_end: Optional[int] = None
    # None for anyone with no family path to the root — see TreeResponse.
    generation: Optional[int] = None


class TreeGeneration(BaseModel):
    """One row. Negative is up the tree (ancestors), 0 is the producer."""

    generation: int
    people: List[TreePerson]


class TreeEdge(BaseModel):
    from_id: str
    to_id: str
    relation_type: str
    label_en: str
    label_he: str
    # The recording that established this relation, so "brother" can play the
    # producer saying so.
    source_segment_id: str


class TreeContradiction(BaseModel):
    """Two recordings that disagree about where somebody sits.

    Surfaced rather than resolved: the first (shortest-path) placement stands,
    and this says which edge was not drawn and what it implied instead."""

    from_id: str
    to_id: str
    relation_type: str
    source_segment_id: str
    kept_generation: int
    implied_generation: int


class TreeResponse(BaseModel):
    root_id: Optional[str] = None
    generations: List[TreeGeneration] = Field(default_factory=list)
    # Real people with no family path to the root. Kept separate so nobody is
    # dropped and nobody is placed in a row they were never shown to belong to.
    unplaced: List[TreePerson] = Field(default_factory=list)
    edges: List[TreeEdge] = Field(default_factory=list)
    contradictions: List[TreeContradiction] = Field(default_factory=list)
    # Tree-bearing relation types with no generation_delta. Their people end up
    # unplaced rather than guessed into a row.
    missing_generation_delta: List[str] = Field(default_factory=list)


class EntityMomentResponse(BaseModel):
    """A recording that mentions a person — video, transcript, and the
    interview question as its title."""

    segment_id: str
    question_asked: str
    question_id: Optional[str] = None
    video_url: Optional[str] = None
    transcript: Optional[str] = None
    summary: Optional[str] = None


class ExtractedEntityResponse(BaseModel):
    name: str
    summary: Optional[str] = None
    # entities.type — person/place/organisation/event/other. Was always null
    # while entities lived in the graph, which had no such distinction; the
    # field was present so typed entities could land without changing this
    # shape, and they have. See segment_extraction.py.
    kind: Optional[str] = None

    model_config = {"from_attributes": True}


class SegmentExtractionResponse(BaseModel):
    """What the system understood from one recording — read-only, for the
    producer to check and catch a mistake. Deliberately says nothing about
    WHERE each piece is stored — which is why it survived entities moving
    out of Graphiti into Postgres without changing."""

    segment_id: str
    question_asked: str
    status: str
    transcript: Optional[str] = None
    topic_tags: List[str] = []
    unit_count: int = 0
    entities: List[ExtractedEntityResponse] = []
    still_processing: bool = False
    # The automatic work is finished and the pipeline is paused on a person.
    # Distinct from still_processing, which means we have not finished looking.
    awaiting_confirmation: bool = False
    entities_unavailable: bool = False
    # Which node of the analysis graph is running, and what to call it on
    # screen. Both None once the run is over. The label is resolved server-side
    # so the client never has to keep a copy of the node list in step.
    progress_stage: Optional[str] = None
    progress_label: Optional[str] = None

    model_config = {"from_attributes": True}


class IdentityAnswer(BaseModel):
    """Whether a name in this recording is someone already in the archive."""

    same_as_existing: bool
    # Required when same_as_existing is true AND the question offered more
    # than one candidate. Validated against that question's own candidates —
    # a uuid from a different question is rejected, not silently applied.
    candidate_uuid: Optional[str] = None


class ParentageAnswer(BaseModel):
    """Whose child one sibling is.

    A LIST of parents rather than a yes/no, because a half-sibling shares one
    parent — "same father, different mother" is unsayable in a binary, and it
    is exactly the case this question exists for.

    Both fields may be given together: two ticked parents plus a name covers
    "my two, and one more nobody has mentioned".
    """

    # Entity ids of the producer's own recorded parents. Validated against the
    # parents that question actually offered — an id from anywhere else is
    # rejected rather than quietly attaching a stranger as somebody's parent.
    parent_ids: List[str] = Field(default_factory=list)
    # A parent never mentioned in any recording. Becomes an ordinary entity
    # with an ordinary parent relation, exactly as any other capture would.
    new_parent_name: Optional[str] = None


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
    # Proposed relation index (as a string) -> accepted?
    #
    # SKIPPABLE, unlike the two below, and the asymmetry is deliberate: an
    # unanswered relation has a genuinely empty outcome (store nothing, leaving
    # the archive as it was), whereas both silent defaults for identity and
    # type are wrong in opposite directions. Omit the key, or send false, and
    # the relation is simply not stored.
    #
    # Keyed by INDEX because two people can hold the same relation to the
    # speaker — "ניר ורז הם אחים שלי" is two sibling proposals — so a name
    # would not identify one.
    relations: Dict[str, bool] = Field(default_factory=dict)
    # Entity name -> whatever the producer typed for its year. Free text, not
    # an int: "בערך 1973" is a perfectly good answer and the client should not
    # have to parse it. Skippable — omit the key to say nothing. Text the
    # server cannot resolve to ONE year is refused with a reason rather than
    # rounded into a number.
    years: Dict[str, str] = Field(default_factory=dict)
    # entity name -> the chosen type. Must be one of exactly the two the
    # question offered (its `type` or its `alternative_type`); anything else
    # is a 400, since a third value could only come from a client inventing
    # one and would land in a column with a CHECK constraint on it.
    types: Dict[str, str] = Field(default_factory=dict)
    # Sibling ENTITY ID -> whose child they are. Skippable like relations and
    # years: omit a sibling entirely to say nothing, and the only consequence
    # is that they are stamped as asked and never asked again.
    #
    # Keyed by entity id, not by name, because unlike every other class here
    # these questions are about people ALREADY in the archive rather than
    # names just extracted from this recording — the id is what identifies
    # them, and two siblings could share a first name.
    parentage: Dict[str, "ParentageAnswer"] = Field(default_factory=dict)
    # Extracted name -> what it should actually say. Skippable, and unlike
    # every other field here it answers no question: the extractor can be
    # CONFIDENTLY wrong, and a name it never doubted raises nothing to
    # disambiguate against. Validated against the names this recording
    # actually produced, so a correction cannot invent an entity.
    name_edits: Dict[str, str] = Field(default_factory=dict)


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
    # The stable question id (see RawSegment.question_id). Optional so an
    # older client, or an upload answering something outside the guided set,
    # still ingests — the endpoint recovers it from question_asked when it
    # can and stores NULL when it genuinely cannot.
    question_id: Optional[str] = Field(default=None, max_length=200)


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
