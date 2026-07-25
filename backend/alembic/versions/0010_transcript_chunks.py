"""add transcript_chunks table

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-23

Prompt 11 (first of four): data foundation for the original-video-clip
chat mode, built alongside the existing avatar path, not replacing it.
One row per Whisper-detected phrase/sentence in a RawSegment's recording,
with word-level timestamps so Prompt 13 can pinpoint a sub-phrase answer
inside a longer, multi-topic recording.
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transcript_chunks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "raw_segment_id",
            sa.String(),
            sa.ForeignKey("raw_segments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("start_sec", sa.Float(), nullable=False),
        sa.Column("end_sec", sa.Float(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("word_timestamps", sa.JSON(), nullable=True),
        sa.Column("embedding", sa.JSON(), nullable=True),
        sa.Column("topic_tags", sa.JSON(), nullable=True),
        sa.Column("sequence_index", sa.Integer(), nullable=False),
        sa.Column("mentioned_entities", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_transcript_chunks_raw_segment_id", "transcript_chunks", ["raw_segment_id"]
    )
    op.create_index(
        "ix_transcript_chunks_segment_sequence",
        "transcript_chunks",
        ["raw_segment_id", "sequence_index"],
    )


def downgrade() -> None:
    op.drop_index("ix_transcript_chunks_segment_sequence", table_name="transcript_chunks")
    op.drop_index("ix_transcript_chunks_raw_segment_id", table_name="transcript_chunks")
    op.drop_table("transcript_chunks")
