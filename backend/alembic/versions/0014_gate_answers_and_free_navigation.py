"""interview gate answers, and the free-navigation setting

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-03

Step 3 of docs/INTERVIEW_RESTRUCTURE.md. Both halves are prerequisites of the
accordion panel, so they land together.

## interview_gate_answers

A gate answer is not a recording, and it cannot be inferred. "Skipped because
the producer answered no" and "not reached yet" produce exactly the same thing
in raw_segments — nothing — so without storing the answer the flow cannot tell
a finished category from an untouched one.

A table rather than a JSON blob on interview_sessions: the accordion asks "is
this category settled" per category, changing one answer must not rewrite the
others, and re-answering is then a plain upsert on the unique key rather than
a read-modify-write of a document.

**No FK or CHECK on `value`, deliberately.** The vocabulary of a gate lives in
interview_questions.json, which is the single source for anything the question
set defines (the Phase 1b property). A database constraint would be a second
copy that needs a migration every time a screening question gains an option —
exactly the coupling this design exists to avoid. Validation is at the
application edge instead, against interview_config.gate_option_values(), which
reads that same file.

Contrast entity_relations, where a FK to relation_types IS right: there the
vocabulary is a database table, so the constraint and the source are the same
thing. Here they would not be.

No separate index on interview_session_id: the unique constraint's index is
already prefixed by it, which serves "every answer for this session" — the
only read the accordion makes.

## users.free_navigation

The accordion is locked and sequential by default. With this off, every
category becomes openable regardless of progress, so the producer can record
or upload out of order. It is the escape hatch two other decisions depend on
(rehoming the post_military recordings, and adding content to a category that
was previously screened out), so it is not optional polish.

Defaults to false: a new producer gets the guided experience.
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interview_gate_answers",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "interview_session_id",
            sa.String(),
            # Cascades: an answer describes one pass through the interview and
            # is meaningless once that session is gone.
            sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The gate's stable id from interview_questions.json — same identity
        # rule as raw_segments.question_id (see migration 0013). Positional
        # references would break the moment the question set is edited.
        sa.Column("gate_id", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), onupdate=sa.func.now()),
        sa.UniqueConstraint(
            "interview_session_id", "gate_id", name="uq_gate_answer_per_session"
        ),
    )

    op.add_column(
        "users",
        sa.Column(
            "free_navigation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "free_navigation")
    op.drop_table("interview_gate_answers")
