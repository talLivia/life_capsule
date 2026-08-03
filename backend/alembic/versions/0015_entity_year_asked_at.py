"""record that a year was already asked for, so it is never asked twice

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-03

Phase 3 (widened) of docs/FAMILY_TREE_TIMELINE.md.

Year capture now covers person, place, organisation and event rather than
event alone, which makes "do not re-ask" load-bearing: without it every future
recording mentioning ניר would ask for his year again, forever, and the
producer would learn to click past the whole confirmation screen.

`year_start IS NULL` cannot express this on its own. It is true both for an
entity nobody has been asked about and for one the producer was asked about
and deliberately skipped — and those must behave differently. Skipping is a
real answer meaning "I do not know", and re-asking would ignore it.

Nullable timestamp rather than a boolean because "when" costs nothing extra
and answers the obvious follow-up question. Set once, when the question is
put to the producer, regardless of whether they answer it.

Deliberately NOT backfilled. Every existing entity has year_asked_at NULL, so
each will be offered a year question exactly once the next time a recording
mentions it — which is the correct outcome for entities that predate the
feature, rather than silently excluding them forever.
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "entities",
        sa.Column("year_asked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("entities", "year_asked_at")
