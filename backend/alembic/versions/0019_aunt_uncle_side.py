"""which side of the family an aunt or uncle is on

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-03

Phase 7 of docs/FAMILY_TREE_TIMELINE.md.

`אמנון -aunt_uncle-> Tal` says they are the producer's uncle. It does not say
WHOSE sibling they are, so there is no edge between them and צבי, and the
parents' row stays four boxes with nothing marking the two that are parents.
`0018` placed them correctly and §8.14 stopped drawing them as parents; this is
the missing positive signal.

`side_asked_at` is the fourth column of its kind, after `year_asked_at` and
`parentage_asked_at`, and it exists for the same reason each time: "has no
sibling edge to a parent" cannot tell never-asked from asked-and-skipped, and
only the second is an answer. Entity-level, because the thing not to repeat is
asking about a PERSON.

Note what this migration does NOT do: rewrite existing `aunt_uncle` rows. They
are true — those people ARE the producer's aunts and uncles — and the answer
ADDS a sibling edge rather than replacing anything. The two agree: `aunt_uncle`
is one generation up from the producer, `sibling` is level with a parent who is
already one generation up, so both place the person in the same row and the
tree reports no contradiction. Migrating by guessing a side would be inventing
the fact the question exists to ask.
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "entities",
        sa.Column("side_asked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("entities", "side_asked_at")
