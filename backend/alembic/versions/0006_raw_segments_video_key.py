"""add video_key to raw_segments

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-20

Prompt 4: store the raw object-storage key alongside video_url so the
transcription Celery task can fetch the object directly rather than
reverse-parsing a key back out of a public/CDN URL.
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("raw_segments", sa.Column("video_key", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("raw_segments", "video_key")
