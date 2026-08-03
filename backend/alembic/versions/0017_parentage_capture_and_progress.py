"""ask-once parentage, honest relation provenance, and extraction progress

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-03

Phase 6 of docs/FAMILY_TREE_TIMELINE.md. Three columns, three reasons.

`entities.parentage_asked_at` — exactly parallel to `year_asked_at` (0015) and
for the same reason. "This sibling has no parent edges" cannot tell
never-asked apart from asked-and-skipped, and without that distinction every
future recording re-asks whose child ניר is until the producer learns to click
past the whole screen. Skipping is a real answer and has to be recorded as one.

`entity_relations.origin` — a relation learned from a recording and a relation
given as an answer on a confirmation screen are not the same kind of fact.
`source_segment_id` means "the recording that established this", and the family
tree offers to play it. A parentage answer is given while confirming a
recording that may never mention that parent, so pointing at it would make the
offer a lie. Existing rows are `recording`, which is what they all are.

`raw_segments.progress_stage` — which node of the analysis graph is running, so
the producer sees progress rather than a spinner. A column rather than a
WebSocket: the app already polls, a column survives a reload, and the graph
resumes from a Postgres checkpoint that a socket would have to re-derive.
NULL once the run is over — it is a liveness signal, not history.
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "entities",
        sa.Column("parentage_asked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "entity_relations",
        sa.Column(
            "origin",
            sa.String(),
            nullable=False,
            server_default="recording",
        ),
    )
    op.add_column(
        "raw_segments",
        sa.Column("progress_stage", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("raw_segments", "progress_stage")
    op.drop_column("entity_relations", "origin")
    op.drop_column("entities", "parentage_asked_at")
