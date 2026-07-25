"""add chat_mode to users

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-24

Prompt 14 (last of four): the producer-level settings toggle between
"avatar" (existing TTS+MuseTalk experience, default) and "video_clips"
(Prompts 11-13's real-footage chat mode). Every existing/new user defaults
to "avatar" — no one's /talk experience changes until a producer opts in.
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("chat_mode", sa.String(), nullable=False, server_default="avatar"),
    )


def downgrade() -> None:
    op.drop_column("users", "chat_mode")
