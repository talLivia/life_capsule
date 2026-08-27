"""Derived-rows redesign (BULK_IMPORT_PLAN UI round, 2026-08-28): the batch
stores the raw parsed CSV pairs + producer exclusions; every row status is
derived per read, and the runner's plan is compiled at start time.

Revision ID: 0031
Revises: 0030
"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bulk_import_batches", sa.Column("mapping_rows", sa.JSON(), nullable=True))
    op.add_column(
        "bulk_import_batches",
        sa.Column("excluded", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("bulk_import_batches", "excluded")
    op.drop_column("bulk_import_batches", "mapping_rows")
