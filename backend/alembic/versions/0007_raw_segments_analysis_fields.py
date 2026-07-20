"""add analysis pipeline fields to raw_segments

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-20

Prompt 5: fields the analysis_graph.py pipeline (transcribe -> extract_topics
-> check_entities -> human_confirm -> score_importance -> finalize_ingest)
writes to as it works through a segment.
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("raw_segments", sa.Column("topic_tags", sa.JSON(), nullable=True))
    op.add_column("raw_segments", sa.Column("importance_score", sa.Float(), nullable=True))
    op.add_column("raw_segments", sa.Column("pending_confirmation", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("raw_segments", "pending_confirmation")
    op.drop_column("raw_segments", "importance_score")
    op.drop_column("raw_segments", "topic_tags")
