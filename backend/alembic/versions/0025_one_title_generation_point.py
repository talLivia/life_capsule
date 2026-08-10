"""one title generation point — the watermark columns come out

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-11

docs/MEDIA_GALLERY.md §1.10. Titles are now generated ONCE, at save time,
inside the analysis pipeline (extract_topics_node) — the same moment the
topic tags are written — and read as-is by both the extraction screen and
the timeline. A re-record is a new segment and titles itself on its own way
through the pipeline.

That removes the entire read-time staleness mechanism 0023/0024 built:

- `moment_title_source` (0024's transcript hash) existed to detect an
  in-place transcript change at read. Generation now happens at the moment
  the transcript is produced, in the same pipeline run — there is no window
  in which the two can disagree, so there is nothing to watermark.
- `moment_title_language` existed because read-time generation had to know
  whether a language switch made a title stale. With one generation point
  the title is simply in the producer's language at save time; a later
  language switch does not retitle old recordings (accepted in §1.10 —
  summaries still regenerate, titles keep their save-time language).

`moment_title` itself stays, values intact.
"""
from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("raw_segments", "moment_title_source")
    op.drop_column("raw_segments", "moment_title_language")


def downgrade() -> None:
    # Restored empty: the lazy path repopulated them on first read, so a
    # downgraded build heals itself the same way it originally filled them.
    op.add_column(
        "raw_segments", sa.Column("moment_title_language", sa.String(), nullable=True)
    )
    op.add_column(
        "raw_segments", sa.Column("moment_title_source", sa.String(), nullable=True)
    )
