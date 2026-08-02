"""persist the stable question id on raw_segments

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-02

Phase 1b of docs/FAMILY_TREE_TIMELINE.md.

`raw_segments` has always stored `question_index` (positional) and
`question_asked` (a text snapshot), but never the stable `id` that
`interview_questions.json` has carried all along ("childhood_home",
"military_service", ...).

That is fine while the question set never changes, and silently wrong the
moment it does. Insert a question near the front and every later index points
at a DIFFERENT question — so a timeline grouping recordings into life periods
by `question_index` would quietly refile history under the wrong milestone.
No error, no exception, just a wrong tree.

Nullable on purpose: an uploaded video answering something outside the guided
set has no question id, and inventing one would be worse than admitting it.
The column is a better key WHERE IT EXISTS, not a claim that it always does.

`question_index` keeps its existing jobs (ordering the record flow, letting a
re-record replace the right segment) and is deliberately NOT removed.

The data backfill lives in scripts/backfill_question_ids.py rather than here:
it matches on question TEXT against the JSON, which is application data this
migration cannot read, and it must be able to report what it could not match
instead of failing a deploy.
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("raw_segments", sa.Column("question_id", sa.String(), nullable=True))
    # The timeline's grouping query filters on this for one producer's
    # recordings; without the index that is a scan of every segment.
    op.create_index("ix_raw_segments_question_id", "raw_segments", ["question_id"])


def downgrade() -> None:
    op.drop_index("ix_raw_segments_question_id", table_name="raw_segments")
    op.drop_column("raw_segments", "question_id")
