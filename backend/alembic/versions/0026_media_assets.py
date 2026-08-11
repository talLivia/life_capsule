"""media_assets — photos on entities and life periods

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-11

Phase 2 of docs/MEDIA_GALLERY.md: the shared foundation both photo features
hang from. A photo attaches to an ENTITY (its portrait on the tree, the
extraction panel, the entity list) or to a CATEGORY (the gallery beside a
timeline period, later surfaced in /talk per §9.4) — exactly one of the two,
enforced by CHECK.

ONE table, not entity_photos + category_photos: they would share every column
and every code path except the owning id, and "two places hold one kind of
fact" is the failure relation_types was built to avoid.

`category` is a bare string, not an FK — categories live in
`interview_questions.json` (same reasoning as raw_segments.question_id and
period_summaries.category). The API validates it against interview_config;
a renamed category orphans its photos, which is the known, accepted risk
that rule already carries.

`is_primary` + the partial unique index: one face per entity for tree nodes
and cards (same pattern as the one-self-per-producer index from 0012).
Declared now rather than in Phase 3 so the table does not move twice.

`entity_id` cascades: the orphan sweep removing a person takes their photos
too — a photo of nobody is not worth keeping. The stored FILE is outside the
transaction (nothing spans Postgres and object storage); a cascaded row
leaves an orphaned file, the accepted direction of that trade.

⚠️ Any manual entity-merge tool must repoint media_assets.entity_id before
deleting the losing row, exactly as it repoints entity_mentions (§2.3).
"""
from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("producer_id", sa.String(), nullable=False),
        sa.Column("storage_key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), server_default="photo", nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("taken_year", sa.Integer(), nullable=True),
        sa.Column(
            "is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column("entity_id", sa.String(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["producer_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(entity_id IS NOT NULL AND category IS NULL) OR "
            "(entity_id IS NULL AND category IS NOT NULL)",
            name="ck_media_one_owner",
        ),
    )
    op.create_index("ix_media_assets_producer_id", "media_assets", ["producer_id"])
    op.create_index("ix_media_assets_entity_id", "media_assets", ["entity_id"])
    op.create_index(
        "ix_media_assets_producer_category",
        "media_assets",
        ["producer_id", "category"],
    )
    # One primary photo per entity — the face the tree and cards show.
    op.create_index(
        "uq_media_primary_per_entity",
        "media_assets",
        ["entity_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )


def downgrade() -> None:
    # Drops producer-uploaded rows, not derived data — but the FILES survive
    # in object storage under photos/, so a re-upgrade loses the row metadata
    # (captions, years, primary flags) and nothing else. Same asymmetry as
    # every storage-backed table here.
    op.drop_table("media_assets")
