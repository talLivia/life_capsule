"""an aunt or uncle belongs in the tree, one generation up

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-03

Found by a real recording: "יש לי דודים אמנון ועדל" extracted correctly as two
`aunt_uncle` relations, and both people then landed in "Mentioned, not yet
placed" — because that type was marked `is_tree_edge=False` with no
`generation_delta`.

That was right when the column was seeded, in the sense that the tree could
not have placed them: a NULL delta is refused rather than guessed (migration
0016). But the delta is not actually unknown. An aunt or uncle is a sibling of
a parent, so they sit in the parents' row: one generation up, exactly like a
parent. Leaving them unplaced while knowing where they belong is the tree
being coy, not careful.

Note this is a GENERATION offset, not a claim about parentage. Nothing here
says an uncle is anybody's parent — the row is shared, the edges are not, and
the tree only ever draws recorded parent-child relations.
"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE relation_types "
            "SET is_tree_edge = true, generation_delta = -1 "
            "WHERE relation_type = 'aunt_uncle'"
        )
    )
    # The other side, if the vocabulary carries it: a niece or nephew is one
    # generation down. Harmless when the row does not exist.
    op.execute(
        sa.text(
            "UPDATE relation_types "
            "SET is_tree_edge = true, generation_delta = 1 "
            "WHERE relation_type = 'niece_nephew'"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE relation_types "
            "SET is_tree_edge = false, generation_delta = NULL "
            "WHERE relation_type IN ('aunt_uncle', 'niece_nephew')"
        )
    )
