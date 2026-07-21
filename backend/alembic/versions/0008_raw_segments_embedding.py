"""add embedding to raw_segments

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-21

Prompt 7: transcript embedding, computed once at ingestion time
(analysis_graph.py's new embed_transcript node) so relevance scoring's
cosine-similarity term doesn't need to re-embed segment text on every
retrieval turn.
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("raw_segments", sa.Column("embedding", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("raw_segments", "embedding")
