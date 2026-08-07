"""ask before merging a name that matches verbatim

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-08

Step 1 of docs/ENTITY_DISAMBIGUATION.md, and the half that §2.1 could not
reach. That section fixed the answer the screen ACCEPTED but could not honour
("someone new" with no distinguishing name). This fixes the case where the
screen never appeared: `check_entities_node` auto-resolved whenever exactly one
candidate matched the extracted name VERBATIM, on the assumption that one name
means one person. It does not. Two people called אמנון — an uncle and an army
friend — landed on one row with no question ever asked, which is the defect
this whole document exists for.

So a verbatim match now asks. `identity_asked_at` is what stops it asking
FOREVER: without it, every recording naming a brother already in the archive
would re-ask whether he is the same brother. Fourth column of this exact shape,
after `year_asked_at`, `parentage_asked_at` and `side_asked_at`, and for the
same reason each time — "the archive holds a row with this name" cannot tell
never-asked from asked-and-confirmed, and only the second is an answer.

NOT BACKFILLED, deliberately, and this is the one judgement in the migration
worth disagreeing with knowingly.

Stamping the existing rows would make this change invisible: no producer would
ever be asked about anyone they have already talked about, and the אמנון row
that already holds two people would go on quietly absorbing a third. Leaving
them unstamped costs a one-time question per person — asked the next time each
one is mentioned, then never again — and that pass over the people already in
the archive is the only thing that can surface a conflation that has already
happened. The cost is bounded and falls to zero; the alternative is a feature
that only protects people who do not exist yet.

Nothing is rewritten and nothing moves: the column is added null, and a null
stamp means exactly what it says.
"""
from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "entities",
        sa.Column("identity_asked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Safe to drop, unlike 0020's: the column holds only "this was asked", and
    # losing it re-asks a question rather than destroying an answer the
    # producer gave about a relationship.
    op.drop_column("entities", "identity_asked_at")
