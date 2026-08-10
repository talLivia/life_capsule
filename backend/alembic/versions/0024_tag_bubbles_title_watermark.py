"""bubbles come from topic_tags; titles get a transcript watermark

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-11

docs/MEDIA_GALLERY.md §1.8. Two reversals of 0022/0023 decisions, both
directed after seeing the shipped result:

**`entities.subtype` is DROPPED, with its CHECK.** Grouping bubbles now come
from the `topic_tags` ingestion already writes per segment — the school
segment is tagged 'בתי ספר', the army segment 'שירות צבאי', so the label IS
the tag and the classification pass bought nothing the archive did not
already know. The five classified rows are derived display data with no
remaining consumer; regenerable from names and summaries if ever wanted.

**`raw_segments.moment_title_source`** — the sha256 of the transcript each
title was generated from. 0023's freshness rule was "a title exists", which
cannot see an in-place transcript change (re-analysis): the title would
outlive the words it named. Same watermark pattern as period_summaries.

Backfilled from the current transcript for rows already titled: every
existing title WAS generated from the transcript now in the row, so stamping
it is recording a fact, not inventing one — and it saves regenerating the
whole archive's titles on the next read for no content change.
"""
import hashlib

from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_entities_subtype_vocabulary", "entities", type_="check")
    op.drop_column("entities", "subtype")

    op.add_column(
        "raw_segments", sa.Column("moment_title_source", sa.String(), nullable=True)
    )
    # Must match period_insights._transcript_hash exactly — a mismatched
    # algorithm here would silently regenerate every title once.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, transcript FROM raw_segments "
            "WHERE moment_title IS NOT NULL AND transcript IS NOT NULL"
        )
    ).fetchall()
    for row in rows:
        digest = hashlib.sha256(row.transcript.encode("utf-8")).hexdigest()
        bind.execute(
            sa.text("UPDATE raw_segments SET moment_title_source = :h WHERE id = :id"),
            {"h": digest, "id": row.id},
        )


def downgrade() -> None:
    op.drop_column("raw_segments", "moment_title_source")
    op.add_column("entities", sa.Column("subtype", sa.String(), nullable=True))
    op.create_check_constraint(
        "ck_entities_subtype_vocabulary",
        "entities",
        "subtype IS NULL OR subtype IN "
        "('school', 'higher_education', 'military', 'workplace', 'community', 'other')",
    )
