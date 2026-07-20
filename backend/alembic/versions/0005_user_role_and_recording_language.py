"""add role and recording_language to users

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-20

Prompt 4: role-based access control for `/record` (producer-only) and a
per-storyteller recording language used to pick the right guided-interview
question set and to tag segments with their source language for later
(Prompt 9+) retrieval-time translation. Every existing/new user defaults to
role="producer" — this POC has one storyteller per deployment; real
family-invite scoping that assigns role="family" lands in Prompt 9.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("role", sa.String(), nullable=False, server_default="producer"),
    )
    op.add_column(
        "users",
        sa.Column("recording_language", sa.String(), nullable=False, server_default="he"),
    )


def downgrade() -> None:
    op.drop_column("users", "recording_language")
    op.drop_column("users", "role")
