"""Bulk-import batch state (BULK_IMPORT_PLAN §3-§6).

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bulk_import_batches",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "producer_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("state", sa.String(), nullable=False, server_default="staging"),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("mapping", sa.JSON(), nullable=True),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("file_states", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("bulk_import_batches")
