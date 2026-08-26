"""Auto-extraction toggle + bulk-import batch marker (BULK_IMPORT_PLAN).

users.auto_extraction: per-producer preference — regular /record uploads
skip the confirmation interrupt when true.
raw_segments.import_batch_id: stamped by the bulk-import orchestrator;
its presence auto-confirms the segment UNCONDITIONALLY (independent of
the toggle) and ties it to its batch for per-file state/reporting.

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("auto_extraction", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "raw_segments",
        sa.Column("import_batch_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_raw_segments_import_batch_id", "raw_segments", ["import_batch_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_raw_segments_import_batch_id", table_name="raw_segments")
    op.drop_column("raw_segments", "import_batch_id")
    op.drop_column("users", "auto_extraction")
