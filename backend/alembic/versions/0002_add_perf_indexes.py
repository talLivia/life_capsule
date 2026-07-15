"""add foreign-key + composite indexes for hot list queries

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-19

The initial schema created the tables without explicit indexes on
foreign-key columns. As the row counts grew, PostgreSQL was forced into
sequential scans for our most frequent list queries
(`SELECT ... WHERE user_id = $1 ORDER BY ... DESC`). This migration adds:

  * Single-column indexes on every FK that participates in a hot WHERE
    clause (user_id, avatar_id, session_id, voice_id, status).
  * Composite indexes covering the predicate + sort columns of the three
    hottest list queries:
        avatars      WHERE user_id ORDER BY created_at DESC
        sessions     WHERE user_id ORDER BY started_at DESC
        messages     WHERE session_id ORDER BY created_at

CREATE INDEX CONCURRENTLY would be safer on a live table, but Alembic
runs inside a transaction by default — so we use the plain form and
expect this migration to be applied during a maintenance window. For
empty/small tables the lock is negligible.
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Single-column FK indexes
    op.create_index("ix_avatars_user_id", "avatars", ["user_id"])
    op.create_index("ix_avatars_voice_id", "avatars", ["voice_id"])
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_avatar_id", "sessions", ["avatar_id"])
    op.create_index("ix_sessions_status", "sessions", ["status"])
    op.create_index("ix_messages_session_id", "messages", ["session_id"])
    op.create_index("ix_conversations_session_id", "conversations", ["session_id"])

    # Composite indexes for list-and-sort queries
    op.create_index("ix_avatars_user_created", "avatars", ["user_id", "created_at"])
    op.create_index("ix_sessions_user_started", "sessions", ["user_id", "started_at"])
    op.create_index("ix_messages_session_created", "messages", ["session_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_messages_session_created", "messages")
    op.drop_index("ix_sessions_user_started", "sessions")
    op.drop_index("ix_avatars_user_created", "avatars")

    op.drop_index("ix_conversations_session_id", "conversations")
    op.drop_index("ix_messages_session_id", "messages")
    op.drop_index("ix_sessions_status", "sessions")
    op.drop_index("ix_sessions_avatar_id", "sessions")
    op.drop_index("ix_sessions_user_id", "sessions")
    op.drop_index("ix_avatars_voice_id", "avatars")
    op.drop_index("ix_avatars_user_id", "avatars")
