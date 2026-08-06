"""relations a producer sets by hand from the family tree

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-06

docs/TREE_EDITING.md. The permanent correction path: extraction proposals are
one-shot, so until now nothing could ADD a relation after the confirmation
screen had been answered. The extraction panel could already remove one.

Two changes, and the first has a consequence worth stating rather than
discovering.

`source_segment_id` BECOMES NULLABLE. Every relation so far was learned from a
recording, and the FK cascades precisely so a relation cannot outlive the words
that established it — the ghost problem חיל האוויר demonstrated, and worse in a
family tree where a wrong edge is highly visible. A hand-made relation has no
such recording: the producer is stating it directly, looking at the tree.

So a manual edge is PERMANENT in a way no other edge is — it survives deleting
every recording about that person, because no recording is what it came from.
That is correct for a statement somebody made deliberately, and it is a real
break in the invariant that deleting a recording removes everything it
produced. Deliberate, not overlooked.

`origin` GAINS 'manual', alongside 'recording' and 'confirmation'. It already
decides whether the tree may offer to play the moment a relation came from;
'manual' is the third answer to that question, and the honest one for an edge
with no moment behind it.

No data migration: every existing row keeps its segment and its origin, and
nothing is reinterpreted.
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "entity_relations",
        "source_segment_id",
        existing_type=sa.String(),
        nullable=True,
    )


def downgrade() -> None:
    # A manual relation has no segment to restore, so it cannot be made NOT
    # NULL again while any exists. Deleting them would silently destroy
    # statements the producer made by hand — the downgrade refuses instead,
    # loudly, and leaves the operator to decide.
    connection = op.get_bind()
    orphans = connection.execute(
        sa.text("SELECT COUNT(*) FROM entity_relations WHERE source_segment_id IS NULL")
    ).scalar()
    if orphans:
        raise RuntimeError(
            f"{orphans} hand-made relation(s) have no source recording. "
            f"Downgrading would require deleting them; do that deliberately "
            f"first if that is really what you want."
        )
    op.alter_column(
        "entity_relations",
        "source_segment_id",
        existing_type=sa.String(),
        nullable=False,
    )
