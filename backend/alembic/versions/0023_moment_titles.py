"""content titles for recordings — raw questions never render by default

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-11

docs/MEDIA_GALLERY.md §1.7. The only name a recording has ever had is its
interview question, and the timeline principle now bans raw question text
from every default surface at any archive size. So a recording gains a
generated content title — "הבית הראשון בטבריה", not "מה אהבת לעשות בתור
ילד?" — written once per recording by the same lazy, batched, stored seam as
period summaries and subtypes (period_insights).

Two columns rather than one: the title plus the language it was written in.
NULL title means never generated (retry next read — an unparseable model
reply must not permanently cost a moment its name); a language mismatch is
staleness, same rule as period_summaries.language. The transcript a title is
generated from never changes after ingest, so unlike summaries there is no
content watermark to store — existence + language IS the freshness rule.

On raw_segments rather than a side table: a title is a property of one
recording exactly as transcript and topic_tags are, and both of those
already live here as generated columns.
"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("raw_segments", sa.Column("moment_title", sa.Text(), nullable=True))
    op.add_column(
        "raw_segments", sa.Column("moment_title_language", sa.String(), nullable=True)
    )


def downgrade() -> None:
    # Derived data — dropping it costs one regeneration pass, never an answer.
    op.drop_column("raw_segments", "moment_title_language")
    op.drop_column("raw_segments", "moment_title")
