import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


def generate_uuid():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=generate_uuid)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    # producer: owns a story archive, records segments via /record.
    # family: invited viewer, /talk-only, scoped to a producer's archive
    # (real invite/scoping lands in Prompt 9 — every account is "producer"
    # by default since this POC has one storyteller per deployment).
    role = Column(String, nullable=False, default="producer", server_default="producer")
    # The language the storyteller records in (BCP-47-ish short code, e.g.
    # "he", "en"). Stamped onto segments/transcripts at ingest time so
    # entity extraction and storage always stay in the storyteller's own
    # language — never translated. A future retrieval-time translation
    # layer (Prompt 9+) uses this to know what it's translating *from* when
    # a viewer's preferred language differs.
    recording_language = Column(String, nullable=False, default="he", server_default="he")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    avatars = relationship("Avatar", back_populates="user", cascade="all, delete-orphan")
    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")
    interview_sessions = relationship(
        "InterviewSession", back_populates="user", cascade="all, delete-orphan"
    )


class Avatar(Base):
    __tablename__ = "avatars"

    id = Column(String, primary_key=True, default=generate_uuid)
    # index=True is required for the hot "list my avatars" query — without it
    # PostgreSQL falls back to a sequential scan once the table grows past a
    # few thousand rows, turning a 5 ms lookup into a 500 ms one.
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    image_url = Column(String, nullable=False)
    thumbnail_url = Column(String, nullable=True)
    s3_key = Column(String, nullable=False)
    status = Column(String, default="processing")  # processing, ready, failed
    voice_id = Column(
        String, nullable=True, index=True
    )  # so voice-deletion clears references quickly
    avatar_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    user = relationship("User", back_populates="avatars")
    # Deleting an avatar deletes its sessions (and, transitively, their
    # messages/conversations). Without the cascade, deleting an avatar that
    # had ever been chatted with raised a NOT NULL/FK violation → HTTP 500.
    sessions = relationship("Session", back_populates="avatar", cascade="all, delete-orphan")

    __table_args__ = (
        # `ORDER BY created_at DESC LIMIT N` is the list-avatars query —
        # the composite covers both predicate columns for a single index scan.
        Index("ix_avatars_user_created", "user_id", "created_at"),
    )


class Session(Base):
    __tablename__ = "sessions"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    avatar_id = Column(
        String, ForeignKey("avatars.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status = Column(String, default="active", index=True)  # active/paused/ended filters by this
    settings = Column(JSON, nullable=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    ended_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", back_populates="sessions")
    avatar = relationship("Avatar", back_populates="sessions")
    messages = relationship("Message", back_populates="session", cascade="all, delete-orphan")
    # Conversations hang off sessions too — without this cascade, deleting a
    # session that had been auto-titled (i.e. any session with at least one
    # turn) raised an FK violation → HTTP 500 from the delete endpoint.
    conversations = relationship(
        "Conversation", back_populates="session", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # HistoryPanel's "list my sessions ordered by recency" query.
        Index("ix_sessions_user_started", "user_id", "started_at"),
    )


class Message(Base):
    __tablename__ = "messages"

    id = Column(String, primary_key=True, default=generate_uuid)
    session_id = Column(
        String, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = Column(String, nullable=False)  # user, assistant, system
    content = Column(Text, nullable=False)
    content_type = Column(String, default="text")  # text, audio, video
    audio_url = Column(String, nullable=True)
    video_url = Column(String, nullable=True)
    message_metadata = Column(JSON, nullable=True)
    tokens = Column(Integer, nullable=True)
    latency = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    session = relationship("Session", back_populates="messages")

    __table_args__ = (
        # Covers the chat-history + WS rehydration query
        # `WHERE session_id=? ORDER BY created_at`. The DESC variant uses the
        # same index because Postgres can scan it backwards.
        Index("ix_messages_session_created", "session_id", "created_at"),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(String, primary_key=True, default=generate_uuid)
    session_id = Column(
        String, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(String, nullable=True)
    summary = Column(Text, nullable=True)
    message_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    session = relationship("Session", back_populates="conversations")


class InterviewSession(Base):
    """
    One producer's pass through the fixed guided-interview question
    sequence (`/record`, Prompt 4) — distinct from `Session`, which is a
    family member's conversation with the finished avatar (`/talk`).
    """

    __tablename__ = "interview_sessions"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String, default="active", index=True)  # active/completed/abandoned
    # Tracks position in the fixed question sequence so a browser refresh or
    # dropped connection mid-interview resumes at the right question instead
    # of restarting (Prompt 4's resumability requirement).
    current_question_index = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    user = relationship("User", back_populates="interview_sessions")
    segments = relationship(
        "RawSegment", back_populates="interview_session", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_interview_sessions_user_created", "user_id", "created_at"),)


class RawSegment(Base):
    """
    One recorded answer to one guided-interview question. Starts as just
    the raw upload; `status` tracks it through the Prompt 5 analysis
    pipeline (transcription -> entity resolution -> importance scoring ->
    Graphiti ingest).
    """

    __tablename__ = "raw_segments"

    id = Column(String, primary_key=True, default=generate_uuid)
    interview_session_id = Column(
        String,
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question_asked = Column(Text, nullable=False)
    # Position in the fixed question sequence (config.json in Prompt 4) —
    # lets a re-record replace the right segment instead of appending.
    question_index = Column(Integer, nullable=False)
    video_url = Column(String, nullable=True)  # set once the R2 upload completes
    # Raw storage key (e.g. "segments/{user_id}/{session_id}/{q_index}/{uuid}.webm"),
    # kept alongside video_url so the transcription task can fetch the object
    # directly instead of reverse-parsing a key out of a public/CDN URL.
    video_key = Column(String, nullable=True)
    transcript = Column(Text, nullable=True)  # set by Prompt 5's transcribe step
    # Set by extract_topics (Prompt 5) — actual-content classification,
    # independent of question_asked. Prompt 6's primary_match queries this.
    topic_tags = Column(JSON, nullable=True)
    # Set by score_importance (Prompt 5), 0-10, Generative Agents style.
    # Reused at retrieval time (Prompt 7) with no additional LLM call.
    importance_score = Column(Float, nullable=True)
    # The live human_confirm interrupt payload while status='pending_confirmation'
    # ({"entity_name", "candidate_uuid", "candidate_name", "candidate_summary",
    # "question"}) — mirrors analysis_graph.py's LangGraph checkpoint so the
    # polling endpoint doesn't need to touch the checkpointer on every request.
    # Cleared once the pipeline moves past confirmation.
    pending_confirmation = Column(JSON, nullable=True)
    # pending_upload -> pending_transcription -> pending_analysis ->
    # pending_confirmation -> ready | failed. Prompt 5 owns the full
    # lifecycle; this column just needs to exist and be indexed for the
    # polling queries Prompts 4-5 run against it.
    status = Column(String, default="pending_upload", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    interview_session = relationship("InterviewSession", back_populates="segments")

    __table_args__ = (
        Index("ix_raw_segments_session_created", "interview_session_id", "created_at"),
    )
