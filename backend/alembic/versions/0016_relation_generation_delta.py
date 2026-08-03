"""how many generations a relation moves, for the family tree

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-03

Phase 4 of docs/FAMILY_TREE_TIMELINE.md.

The tree places people in generation rows by walking edges out from the
producer's self-entity. That needs to know how far each relation moves:
`parent` is one row up, `grandparent` two, `sibling` and `spouse` none.

That fact is NOT derivable from the columns already present. `is_symmetric`
gets sibling and spouse to zero, but parent and grandparent are both
directional and differ, so the number has to be stored.

It goes in `relation_types` rather than a dict in the tree code for the same
reason `is_tree_edge` already lives there: the vocabulary of relations is
data, and adding a tree-bearing type must not require editing a layout module
to make it draw. A tree type left with a NULL delta is not guessed at — the
page reports those people as "not yet placed", which is visible and correct
rather than quietly wrong by one row.

NULL for every non-tree type, where the question does not arise.
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

# Only the six tree-bearing types. Negative is UP the tree (an ancestor),
# positive is down, zero is the same row.
_DELTAS = {
    "parent": -1,
    "grandparent": -2,
    "child": 1,
    "grandchild": 2,
    "sibling": 0,
    "spouse": 0,
}


def upgrade() -> None:
    op.add_column(
        "relation_types", sa.Column("generation_delta", sa.Integer(), nullable=True)
    )
    for relation_type, delta in _DELTAS.items():
        op.execute(
            sa.text(
                "UPDATE relation_types SET generation_delta = :d "
                "WHERE relation_type = :t"
            ).bindparams(d=delta, t=relation_type)
        )


def downgrade() -> None:
    op.drop_column("relation_types", "generation_delta")
