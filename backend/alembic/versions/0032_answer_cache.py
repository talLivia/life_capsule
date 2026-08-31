"""Semantic answer cache (2026-08-31): cached select_units outcomes keyed by
producer + archive-version fingerprint + question embedding. Entries are
orphaned automatically when the version fingerprint moves (same mechanism as
the gemini context cache); session_id-tagged rows are speculative follow-up
prefetches scoped to one conversation.

Revision ID: 0032
Revises: 0031
"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "answer_cache_entries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "producer_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("version_hash", sa.String(length=32), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("question_embedding", sa.JSON(), nullable=False),
        sa.Column("unit_keys", sa.JSON(), nullable=False),
        sa.Column("follow_up", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="live"),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_answer_cache_producer_version",
        "answer_cache_entries",
        ["producer_id", "version_hash"],
    )
    op.create_index(
        "ix_answer_cache_session", "answer_cache_entries", ["session_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_answer_cache_session", table_name="answer_cache_entries")
    op.drop_index(
        "ix_answer_cache_producer_version", table_name="answer_cache_entries"
    )
    op.drop_table("answer_cache_entries")
