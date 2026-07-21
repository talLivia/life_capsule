"""add family_invites table and users.producer_id

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-21

Prompt 9: family access control. A family account's producer_id links it
to the storyteller whose archive it may query via /talk; family_invites is
the token-based redemption flow a producer uses to grant that access.
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("producer_id", sa.String(), nullable=True))
    op.create_index("ix_users_producer_id", "users", ["producer_id"])
    op.create_foreign_key(
        "fk_users_producer_id", "users", "users", ["producer_id"], ["id"]
    )

    op.create_table(
        "family_invites",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "producer_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending"),
        sa.Column(
            "redeemed_by_user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_family_invites_producer_id", "family_invites", ["producer_id"])
    op.create_index("ix_family_invites_token", "family_invites", ["token"], unique=True)
    op.create_index(
        "ix_family_invites_producer_status", "family_invites", ["producer_id", "status"]
    )


def downgrade() -> None:
    op.drop_table("family_invites")
    op.drop_constraint("fk_users_producer_id", "users", type_="foreignkey")
    op.drop_index("ix_users_producer_id", table_name="users")
    op.drop_column("users", "producer_id")
