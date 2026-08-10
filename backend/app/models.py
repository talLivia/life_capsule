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
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import expression, func

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
    # family: invited viewer, /talk-only, scoped to a producer's archive via
    # producer_id below (Prompt 9's invite/redeem flow, family_invites
    # table). Every account is "producer" by default since this POC has one
    # storyteller per deployment; redeeming an invite flips role to "family".
    role = Column(String, nullable=False, default="producer", server_default="producer")
    # Set only for role="family" accounts — which producer's archive this
    # family member is scoped to (retrieval_service's group_id, Prompt 6-8).
    # Populated by redeeming a family_invites token, never set directly by
    # the user. Self-referential FK, so no ondelete=CASCADE here (a deleted
    # producer would need real cleanup semantics beyond this POC's scope) —
    # left NULL to unlink is nullable already, so it fails soft either way.
    producer_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    # The language the storyteller records in (BCP-47-ish short code, e.g.
    # "he", "en"). Stamped onto segments/transcripts at ingest time so
    # entity extraction and storage always stay in the storyteller's own
    # language — never translated. A future retrieval-time translation
    # layer (Prompt 9+) uses this to know what it's translating *from* when
    # a viewer's preferred language differs.
    recording_language = Column(String, nullable=False, default="he", server_default="he")
    # Which chat mode /talk renders for anyone talking to THIS user's
    # archive — "avatar" (TTS + MuseTalk, the original/default experience),
    # "video_clips" (real recorded footage via chunk retrieval, Prompt
    # 11-14), or "video_clips_v2" (Prompt 15's experimental full-archive-
    # reading alternative, A/B'd against video_clips; same response shape,
    # different range-decision backend). Producer-level only: a family
    # account's own row never reads its own chat_mode, since /talk always
    # renders based on the linked PRODUCER's setting (see
    # TalkAvailabilityResponse) — all modes keep working independently,
    # this just picks which one a given producer's viewers see.
    chat_mode = Column(String, nullable=False, default="avatar", server_default="avatar")
    # /record's accordion is locked and sequential by default. Turning this on
    # makes every category openable regardless of progress, so the producer can
    # record or upload out of order — the escape hatch for rehoming footage and
    # for adding content to a category that was previously screened out.
    # See docs/INTERVIEW_RESTRUCTURE.md §7A.
    free_navigation = Column(
        Boolean, nullable=False, default=False, server_default=expression.false()
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    avatars = relationship("Avatar", back_populates="user", cascade="all, delete-orphan")
    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")
    interview_sessions = relationship(
        "InterviewSession", back_populates="user", cascade="all, delete-orphan"
    )
    producer = relationship("User", remote_side=[id], foreign_keys=[producer_id])


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
    gate_answers = relationship(
        "InterviewGateAnswer",
        back_populates="interview_session",
        cascade="all, delete-orphan",
    )

    __table_args__ = (Index("ix_interview_sessions_user_created", "user_id", "created_at"),)


class InterviewGateAnswer(Base):
    """One answer to one screening/branching question, for one interview pass.

    A gate answer is not a recording and cannot be inferred: "skipped because
    the producer said no" and "not reached yet" both leave raw_segments empty,
    so without this row the flow cannot tell a finished category from an
    untouched one.

    `value` has no FK or CHECK on purpose. A gate's options live in
    interview_questions.json, which is the single source for anything the
    question set defines; a database constraint would be a second copy needing
    a migration every time a screening question gains an option. Validated at
    the application edge against interview_config.gate_option_values() instead.
    (EntityRelation.relation_type DOES carry a FK — there the vocabulary is a
    table, so the constraint and the source are the same thing. Here they
    would not be.)
    """

    __tablename__ = "interview_gate_answers"

    id = Column(String, primary_key=True, default=generate_uuid)
    interview_session_id = Column(
        String,
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The gate's STABLE id — same identity rule as RawSegment.question_id.
    gate_id = Column(String, nullable=False)
    value = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    interview_session = relationship("InterviewSession", back_populates="gate_answers")

    __table_args__ = (
        # Re-answering is an upsert on this key. No separate index on
        # interview_session_id — this one is already prefixed by it, which
        # serves the only read the accordion makes ("every answer for this
        # session").
        UniqueConstraint(
            "interview_session_id", "gate_id", name="uq_gate_answer_per_session"
        ),
    )


class RawSegment(Base):
    """
    One recorded answer to one guided-interview question. Starts as just
    the raw upload; `status` tracks it through the Prompt 5 analysis
    pipeline (transcription -> entity resolution -> importance scoring ->
    entity write).
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
    #
    # POSITIONAL, and therefore NOT a stable identifier: editing the question
    # set moves it. Use question_id below for anything that must still mean the
    # same thing after the set changes.
    #
    # ⚠️ Since the accordion landed it is also NO LONGER GLOBALLY UNIQUE across
    # an interview — it is the step's position WITHIN ITS CATEGORY, so two
    # categories both have a question_index 0. Nothing reads it as a global
    # position (its remaining job is separating takes of one question, and
    # full_archive_retrieval groups takes per question, not across the set),
    # but the name oversells what it now means. Worth renaming to
    # question_position_in_category when something else touches this column.
    question_index = Column(Integer, nullable=False)
    # The STABLE id from interview_questions.json ("childhood_home", ...) —
    # what a life period must be derived from, since question_index silently
    # points elsewhere once questions are added or reordered. See migration
    # 0013 and docs/FAMILY_TREE_TIMELINE.md §2A.
    #
    # Nullable: an uploaded video answering something outside the guided set
    # genuinely has no question id, and inventing one would be worse.
    question_id = Column(String, nullable=True, index=True)
    video_url = Column(String, nullable=True)  # set once the R2 upload completes
    # Raw storage key (e.g. "segments/{user_id}/{session_id}/{q_index}/{uuid}.webm"),
    # kept alongside video_url so the transcription task can fetch the object
    # directly instead of reverse-parsing a key out of a public/CDN URL.
    video_key = Column(String, nullable=True)
    transcript = Column(Text, nullable=True)  # set by Prompt 5's transcribe step
    # Set by extract_topics (Prompt 5) — actual-content classification,
    # independent of question_asked. Prompt 6's primary_match queries this.
    topic_tags = Column(JSON, nullable=True)
    # A generated CONTENT title ("הבית הראשון בטבריה") — the recording's only
    # rendered name, on the extraction screen and the timeline alike; raw
    # question text never renders (docs/MEDIA_GALLERY.md §1.10). Written ONCE,
    # at save time, by extract_topics_node in the same run that writes
    # topic_tags — there is no read-time generation and no staleness check
    # (migration 0025 removed the watermark columns). NULL means generation
    # failed at save; the screen falls back to the take label.
    moment_title = Column(Text, nullable=True)
    # Set by score_importance (Prompt 5), 0-10, Generative Agents style.
    # Reused at retrieval time (Prompt 7) with no additional LLM call.
    importance_score = Column(Float, nullable=True)
    # Transcript embedding (analysis_graph.py's embed_transcript node,
    # Prompt 7) — a JSON list of floats, same vector space as
    # services/embeddings.py's question embeddings so Prompt 7's
    # relevance_score (cosine similarity) is computed once at ingestion
    # time, not recomputed per candidate on every retrieval turn.
    embedding = Column(JSON, nullable=True)
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
    # Which node of the analysis graph is running right now, so the producer
    # watching the extraction screen sees movement instead of a spinner.
    # Deliberately NOT part of `status`: status is the lifecycle other code
    # branches on, and adding half a dozen transient values to it would mean
    # every `status == "..."` check has a new way to be wrong. NULL when
    # nothing is running — this is a liveness signal, not history.
    progress_stage = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    interview_session = relationship("InterviewSession", back_populates="segments")
    transcript_chunks = relationship(
        "TranscriptChunk", back_populates="raw_segment", cascade="all, delete-orphan"
    )
    # Cascaded at BOTH layers, exactly like transcript_chunks above: the FK
    # carries ondelete="CASCADE" for raw SQL, and this carries it for the ORM.
    # Without the ORM half, deleting a recording through the session would
    # leave its mentions behind on any backend not enforcing the FK — which
    # includes SQLite unless PRAGMA foreign_keys is on, i.e. the entire test
    # suite. The deletion path would then look correct in tests and orphan
    # rows in production, or the reverse.
    entity_mentions = relationship(
        "EntityMention", back_populates="raw_segment", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_raw_segments_session_created", "interview_session_id", "created_at"),
    )


class TranscriptChunk(Base):
    """
    One Whisper-detected phrase/sentence from a `RawSegment`'s recording —
    the data foundation for the original-video-clip chat mode (Prompts
    11-14, alongside the existing avatar path, not replacing it). One row
    per natural phrase boundary Whisper found, NOT grouped into
    fixed-duration windows: a long, multi-topic recording needs retrieval
    precise enough to isolate the few seconds that actually answer a
    question, not just "somewhere in this segment" (RawSegment's own
    whole-recording embedding/topic_tags, which the avatar path still uses
    unchanged).
    """

    __tablename__ = "transcript_chunks"

    id = Column(String, primary_key=True, default=generate_uuid)
    raw_segment_id = Column(
        String, ForeignKey("raw_segments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # This chunk's OWN timing/text — never the contextual window used to
    # compute its embedding (see `embedding` below). What gets returned/
    # played back to a family member must be exactly this phrase, not the
    # neighboring context that helped find it.
    start_sec = Column(Float, nullable=False)
    end_sec = Column(Float, nullable=False)
    text = Column(Text, nullable=False)
    # List of {"word": str, "start_sec": float, "end_sec": float}, from
    # stt.py's transcribe_with_timestamps — lets Prompt 13 pinpoint a
    # sub-phrase answer's exact start/end, narrower than this chunk's own
    # start_sec/end_sec, instead of returning the whole phrase verbatim.
    word_timestamps = Column(JSON, nullable=True)
    # Computed from this chunk's text PLUS a small window of neighboring
    # phrases (via sequence_index) for better semantic recall on a short,
    # otherwise-ambiguous phrase — the embedded text is NOT what's stored
    # above or ever played back, only what's compared against a question's
    # embedding at retrieval time (Prompt 12).
    embedding = Column(JSON, nullable=True)
    topic_tags = Column(JSON, nullable=True)
    # Position among this segment's own chunks in chronological order —
    # lets Prompt 13's boundary expansion (and this table's own contextual-
    # embedding window) look up immediate neighbors with an indexed
    # equality lookup instead of a timestamp range query.
    sequence_index = Column(Integer, nullable=False)
    # Entity names (from analysis_graph.py's existing whole-segment
    # check_entities_node) that textually appear in THIS chunk specifically
    # — lets a matched entity/topic be traced back to an exact moment
    # rather than just "mentioned somewhere in this recording". Populated
    # by simple substring matching against already-extracted names, not a
    # second LLM call, and not a change to check_entities_node's own
    # disambiguation behavior.
    mentioned_entities = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    raw_segment = relationship("RawSegment", back_populates="transcript_chunks")

    __table_args__ = (
        # Prompt 13's neighbor-walk (boundary expansion from a pinpointed
        # sub-range) and this table's own contextual-embedding window both
        # look up "this segment's chunks around sequence_index N".
        Index("ix_transcript_chunks_segment_sequence", "raw_segment_id", "sequence_index"),
    )


class FamilyInvite(Base):
    """
    A producer-issued invite token (Prompt 9) — a family member redeems it
    to link their account's `User.producer_id` to this producer, unlocking
    /talk scoped to that producer's archive. Deliberately simple (one
    producer per family account, no multi-producer sharing) to match this
    POC's single-storyteller-per-deployment design.
    """

    __tablename__ = "family_invites"

    id = Column(String, primary_key=True, default=generate_uuid)
    producer_id = Column(
        String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token = Column(String, unique=True, nullable=False, index=True)
    # pending -> redeemed | revoked | expired. "expired" is derived at read
    # time from expires_at, never written — see family.py's is_expired check.
    status = Column(String, nullable=False, default="pending", server_default="pending")
    redeemed_by_user_id = Column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    redeemed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_family_invites_producer_status", "producer_id", "status"),)


class Entity(Base):
    """A person, place, organisation or event named in this producer's
    archive — the Postgres replacement for Graphiti's Entity nodes.

    Holds only what is true of the THING, independent of any one recording:
    its name, what kind of thing it is, and (for events) when it happened.
    What a particular recording SAID about it lives on EntityMention, one row
    per recording — see that class for why.
    """

    __tablename__ = "entities"

    id = Column(String, primary_key=True, default=generate_uuid)
    producer_id = Column(
        String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # What the storyteller actually said, verbatim — this is what gets shown
    # back to them.
    name = Column(String, nullable=False)
    # The MERGE KEY: two names normalising to the same value are one entity,
    # enforced by the unique constraint below. Always produced by
    # entity_names.normalize_entity_name, never hand-built — the constraint
    # cannot tell a correctly-derived key from a careless one.
    normalized_name = Column(String, nullable=False)
    # person | place | organisation | event | other. A CHECK constraint in
    # migration 0012 enforces the vocabulary; this column is deliberately not
    # an Enum so adding a type is a migration, not a code deploy.
    type = Column(String, nullable=False, default="other", server_default="other")
    # Mainly meaningful on events. Nullable everywhere: most entities have no
    # year, and guessing one is worse than leaving it open.
    year_start = Column(Integer, nullable=True)
    year_end = Column(Integer, nullable=True)
    # When the producer was ASKED for a year — set whether or not they gave
    # one. `year_start IS NULL` cannot distinguish "nobody has asked" from
    # "asked, and they said they do not know", and those must behave
    # differently: the second is a real answer, and re-asking on every future
    # recording that mentions the entity would ignore it. See migration 0015.
    year_asked_at = Column(DateTime(timezone=True), nullable=True)
    # Same shape as year_asked_at, same reason: "has no parent edges" cannot
    # distinguish never-asked from asked-and-skipped, and only the second is
    # an answer. Set whether or not any parent was named. See migration 0017.
    parentage_asked_at = Column(DateTime(timezone=True), nullable=True)
    # Which side of the family an aunt or uncle is on — asked once. Same shape
    # and same reason as the two above: "no sibling edge to a parent" cannot
    # distinguish never-asked from asked-and-skipped. See migration 0019.
    side_asked_at = Column(DateTime(timezone=True), nullable=True)
    # When the producer last confirmed WHO this row is — that a recording
    # naming this name meant this person and not a second one who happens to
    # share it. Fourth of the same shape, and the reason is sharper than the
    # others: the merge key IS the name, so a name matching verbatim used to
    # auto-merge on the assumption that one name means one person. It does not
    # — one אמנון row ended up holding both an uncle and an army friend. Now a
    # verbatim match ASKS, and this stamp is what stops it asking again on
    # every later recording that mentions the same confirmed person.
    #
    # Deliberately NOT backfilled (migration 0021): stamping the entities that
    # already exist would declare exactly the conflations this is meant to
    # catch already settled.
    identity_asked_at = Column(DateTime(timezone=True), nullable=True)
    # The producer themselves. Extracted summaries are phrased relative to
    # "the speaker", so relations need a node for that person to point at;
    # the family tree roots here. One per producer (partial unique index).
    is_self = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    mentions = relationship(
        "EntityMention", back_populates="entity", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # THE merge rule, declared here as well as in migration 0012 (which
        # remains the authority for the real database, along with the CHECK
        # constraints and the one-self-per-producer partial index, neither of
        # which SQLAlchemy can express portably). Declared so the test
        # database enforces it too: without it SQLite would happily accept the
        # duplicate row the write path exists to prevent, and the merge would
        # be untested exactly where it matters.
        UniqueConstraint(
            "producer_id", "normalized_name", name="uq_entities_producer_normalized"
        ),
        Index("ix_entities_producer_type", "producer_id", "type"),
    )


class EntityMention(Base):
    """One recording naming one entity, and what THAT recording said about it.

    The summary lives here rather than on Entity so that it can never go
    stale: it describes exactly one recording, so adding a recording inserts a
    row and deleting one drops a row, and no existing row is ever rewritten.
    Where something needs "a summary for Gila", it lists these in the
    recordings' chronological order — no LLM call, no merge step, correct by
    construction. A single summary on Entity would have needed regenerating on
    every ingest AND every delete to stay honest.

    This table is also both halves of the load-bearing work the graph used to
    do: the entity map ("which recordings mention this name") and the deletion
    safety check ("is any other recording still mentioning it"). Both are
    joins on raw_segment_id.
    """

    __tablename__ = "entity_mentions"

    id = Column(String, primary_key=True, default=generate_uuid)
    entity_id = Column(
        String, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # REQUIRED, and cascading — this is what makes deleting a recording a
    # single Postgres transaction rather than a two-database dance.
    raw_segment_id = Column(
        String,
        ForeignKey("raw_segments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # OPTIONAL, and repopulated on re-ingest rather than preserved: moving to
    # Deepgram turned one chunk into eight, invalidating every stored chunk
    # id. Losing chunk-level precision on re-ingest is acceptable; losing the
    # entity-to-recording link is not.
    chunk_id = Column(
        String, ForeignKey("transcript_chunks.id", ondelete="SET NULL"), nullable=True
    )
    # What THIS recording said about the entity — "a fellow soldier in her
    # unit", not a merged portrait.
    summary = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    entity = relationship("Entity", back_populates="mentions")
    raw_segment = relationship("RawSegment", back_populates="entity_mentions")


class RelationType(Base):
    """The vocabulary of entity relations, and everything true of each type.

    A lookup table rather than a `category` column on EntityRelation, because
    category is a FUNCTION of the type — 'sibling' is always family — and a
    per-row column could contradict it. It also constrains the vocabulary via
    FK (an LLM inventing "brother-ish" fails loudly), and is where symmetry
    and inverses live instead of a hardcoded dict.

    Seeded by migration 0012, not by application code: the FK means rows must
    exist before any relation can be written.
    """

    __tablename__ = "relation_types"

    relation_type = Column(String, primary_key=True)
    # family | social | professional | other — for display grouping.
    category = Column(String, nullable=False)
    # AUTHORITATIVE for the family tree, and deliberately allowed to disagree
    # with `category`: in-laws and cousins are family but a tree that drew
    # them stops being readable. The tree page never guesses.
    is_tree_edge = Column(Boolean, nullable=False)
    # NULL for symmetric types — 'sibling' inverted is still 'sibling'.
    inverse_type = Column(String, nullable=True)
    is_symmetric = Column(Boolean, nullable=False)
    # How many generation rows this relation moves: -1 for parent, -2 for
    # grandparent, 0 for sibling and spouse. Not derivable from the columns
    # above — parent and grandparent are both directional and differ — so the
    # tree reads it from here rather than a map in the layout code. NULL for
    # non-tree types, and a tree type left NULL is reported as unplaceable
    # rather than guessed. See migration 0016.
    generation_delta = Column(Integer, nullable=True)
    label_en = Column(String, nullable=False)
    label_he = Column(String, nullable=False)


class EntityRelation(Base):
    """A relationship between two entities, learned from one recording.

    NOT populated yet — the capture flow is separate, later work. The table
    exists now so the schema does not have to move twice.

    A relation rather than a column on the person, because "uncle" is a
    property of a PAIR: the same person is a sibling to one and a parent to
    another. ONE directed row per relation, never two — storing both
    directions means every edit and delete has to keep a pair in sync, and
    they will eventually disagree. The inverse is derived at read time from
    RelationType.
    """

    __tablename__ = "entity_relations"

    id = Column(String, primary_key=True, default=generate_uuid)
    from_entity_id = Column(
        String, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    to_entity_id = Column(
        String, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type = Column(
        String, ForeignKey("relation_types.relation_type"), nullable=False
    )
    # The recording this was learned from, cascading — a relation must never
    # outlive the recording that established it. Same ghost problem as
    # חיל האוויר, and worse here: a wrong edge in a family tree is highly
    # visible.
    # NULL for a relation the producer set by hand from the tree — there is no
    # recording behind it. Such an edge is therefore PERMANENT in a way no
    # other is: it survives deleting every recording about that person. That
    # breaks the invariant the cascade exists for, deliberately, because a
    # statement somebody made directly is not owned by any recording. See
    # migration 0020.
    source_segment_id = Column(
        String,
        ForeignKey("raw_segments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # "recording" — the words in source_segment_id stated this relation.
    # "confirmation" — the producer gave it as an answer while confirming that
    # recording, which may never mention the person at all. The tree offers to
    # play the recording a relation came from; that offer is only honest for
    # the first kind. See migration 0017.
    # "manual" — the producer set it from the family tree, with no recording
    # behind it at all. See migration 0020.
    origin = Column(String, nullable=False, server_default="recording", default="recording")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PeriodSummary(Base):
    """One generated sentence describing a life period — derived data.

    The timeline's compact card shows a one-sentence summary of what a period's
    recordings cover. Generated by an LLM, so it must not run on every page
    view: this row is the store, and `source_segment_ids` is the watermark —
    regenerate exactly when the set of recordings behind the category (or the
    language) changes, serve from here otherwise. Deleting a recording changes
    the watermark too, so a summary can never describe footage that is gone.

    Postgres rather than cache_service, deliberately: without Redis the cache
    silently no-ops (CLAUDE.md), and a summary that quietly regenerates on
    every view in dev is a cost bug nothing surfaces.

    `category` is a bare string, not an FK — categories live in
    `interview_questions.json`, same reasoning as RawSegment.question_id.
    """

    __tablename__ = "period_summaries"

    producer_id = Column(
        String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    category = Column(String, primary_key=True)
    summary = Column(Text, nullable=False)
    # The language the sentence was written in. A producer switching their
    # recording language must not keep reading last month's Hebrew sentence
    # under an English page — a language mismatch is staleness.
    language = Column(String, nullable=False)
    # The recording ids the sentence was generated from — THE staleness rule.
    source_segment_ids = Column(JSON, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
