"""add interview_sessions and raw_segments

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-15

Prompt 2: app-level tables for the guided-interview recording flow
(`/record`, Prompt 4) and its analysis pipeline (Prompt 5). Distinct from
the base project's `sessions`/`messages` tables, which model a family
member's conversation with the finished avatar (`/talk`), not the
producer's recording pass.

Foreign keys use ON DELETE CASCADE from creation (see migration 0003 for
why the original schema had to be patched to add this after the fact).
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interview_sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(), server_default="active"),
        sa.Column("current_question_index", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), onupdate=sa.func.now()),
    )
    op.create_index(
        "ix_interview_sessions_user_id", "interview_sessions", ["user_id"]
    )
    op.create_index(
        "ix_interview_sessions_status", "interview_sessions", ["status"]
    )
    op.create_index(
        "ix_interview_sessions_user_created",
        "interview_sessions",
        ["user_id", "created_at"],
    )

    op.create_table(
        "raw_segments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "interview_session_id",
            sa.String(),
            sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question_asked", sa.Text(), nullable=False),
        sa.Column("question_index", sa.Integer(), nullable=False),
        sa.Column("video_url", sa.String(), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), server_default="pending_upload"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), onupdate=sa.func.now()),
    )
    op.create_index(
        "ix_raw_segments_interview_session_id", "raw_segments", ["interview_session_id"]
    )
    op.create_index("ix_raw_segments_status", "raw_segments", ["status"])
    op.create_index(
        "ix_raw_segments_session_created",
        "raw_segments",
        ["interview_session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("raw_segments")
    op.drop_table("interview_sessions")
