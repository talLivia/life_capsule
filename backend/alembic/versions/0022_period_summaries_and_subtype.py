"""period summaries and organisation subtypes for the compact timeline

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-10

The Phase 1 timeline (docs/MEDIA_GALLERY.md §1.5) rendered every recording and
every entity chip by default — correct data, wrong altitude. On a real archive
every category would arrive fully expanded. The default becomes a summary:
category title, one generated sentence, and a handful of GROUPED bubbles
(משפחה, בתי ספר, מקומות…), with the full Phase 1 view behind an expand.

Two pieces of derived data need a home:

`period_summaries` — one generated sentence per (producer, category), plus the
segment ids and language it was built from. The watermark is what makes the
refresh honest: regenerate exactly when the underlying set of recordings (or
the language) changes, serve from the table otherwise. Stored in Postgres and
not a cache: cache_service silently no-ops without Redis (CLAUDE.md), and a
summary that silently regenerates on every view in dev is a cost bug nobody
sees. `category` is a bare string, not an FK — categories live in
`interview_questions.json`, the same reasoning as `RawSegment.question_id`.

`entities.subtype` — the display grouping for organisations. `type` alone
cannot say "school": measured on the live archive, all five organisations
(three schools, the air force, a college) share type='organisation'. The
vocabulary is closed (school | higher_education | military | workplace |
community | other) and CHECK-enforced, mirroring `type`'s rule from 0012:
adding a value is a migration, not a code deploy. NULL means never classified;
'other' means classified and nothing fit — the same never-asked vs
asked-and-unknown distinction as the *_asked_at columns, and what stops a
hard-to-classify organisation being re-sent to the model on every load.

Nothing is backfilled: summaries and subtypes are derived data, and the
timeline derives them on first read.
"""
from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "entities",
        sa.Column("subtype", sa.String(), nullable=True),
    )
    op.create_check_constraint(
        "ck_entities_subtype_vocabulary",
        "entities",
        "subtype IS NULL OR subtype IN "
        "('school', 'higher_education', 'military', 'workplace', 'community', 'other')",
    )

    op.create_table(
        "period_summaries",
        sa.Column("producer_id", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("language", sa.String(), nullable=False),
        sa.Column("source_segment_ids", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["producer_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("producer_id", "category"),
    )


def downgrade() -> None:
    # Safe both ways: everything here is derived data. Dropping it costs one
    # regeneration pass on the next timeline read, never an answer a producer
    # gave.
    op.drop_table("period_summaries")
    op.drop_constraint("ck_entities_subtype_vocabulary", "entities", type_="check")
    op.drop_column("entities", "subtype")
