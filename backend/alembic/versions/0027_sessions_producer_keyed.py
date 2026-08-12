"""sessions become producer-keyed; avatar optional; v2 is the default mode

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-13

docs/V2_PRIMARY_AVATAR_DORMANT_PLAN.md §3.1/§3.5. Three related changes:

1. `sessions.producer_id` — whose ARCHIVE a conversation is against. Every
   consumer that used to reach the producer THROUGH the avatar
   (session → avatar → user) reads this column instead. Backfilled from
   exactly that join, then NOT NULL: the join is the current truth, the
   column is its permanent home.

2. `sessions.avatar_id` becomes nullable with ON DELETE SET NULL (was
   NOT NULL + CASCADE). A v2 session involves no avatar at all, and
   deleting an avatar must not destroy conversation history — under the
   old CASCADE, deleting a photo nobody sees (v2 plays real footage)
   erased the family's chats and the shown-unit memory inside them.

3. `users.chat_mode` defaults to 'video_clips_v2'. Existing 'avatar' rows
   are flipped ONLY where the account owns no ready avatar — i.e. where
   avatar mode is already non-functional (talk-availability returns
   unavailable) — so nobody's working experience changes. An account with
   a ready avatar keeps whatever mode it has.
"""
from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres-only DDL, same reasoning as migration 0003: SQLite (tests,
    # ad-hoc local DBs) is built by Base.metadata.create_all from the final
    # models and never runs migrations.
    if op.get_bind().dialect.name == "sqlite":
        return

    # 1. producer_id: add nullable → backfill from the avatar join → NOT NULL.
    op.add_column("sessions", sa.Column("producer_id", sa.String(), nullable=True))
    op.execute(
        "UPDATE sessions SET producer_id = avatars.user_id "
        "FROM avatars WHERE sessions.avatar_id = avatars.id"
    )
    # avatar_id is NOT NULL + ON DELETE CASCADE up to this migration, so the
    # join above covers every row; this is belt-and-braces so a surprise
    # orphan fails HERE, loudly, rather than at SET NOT NULL with less context.
    op.execute("DELETE FROM sessions WHERE producer_id IS NULL")
    op.alter_column("sessions", "producer_id", nullable=False)
    op.create_index("ix_sessions_producer_id", "sessions", ["producer_id"])
    op.create_foreign_key(
        "sessions_producer_id_fkey",
        "sessions",
        "users",
        ["producer_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 2. avatar_id: nullable, and the FK flips CASCADE → SET NULL.
    op.alter_column("sessions", "avatar_id", nullable=True)
    op.drop_constraint("sessions_avatar_id_fkey", "sessions", type_="foreignkey")
    op.create_foreign_key(
        "sessions_avatar_id_fkey",
        "sessions",
        "avatars",
        ["avatar_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # 3. chat_mode: v2 becomes the default; flip only rows where avatar mode
    #    cannot function anyway (no ready avatar).
    op.alter_column("users", "chat_mode", server_default="video_clips_v2")
    op.execute(
        "UPDATE users SET chat_mode = 'video_clips_v2' "
        "WHERE chat_mode = 'avatar' AND NOT EXISTS ("
        "  SELECT 1 FROM avatars"
        "  WHERE avatars.user_id = users.id AND avatars.status = 'ready'"
        ")"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return

    op.alter_column("users", "chat_mode", server_default="avatar")
    # The chat_mode data flip is not reversed: which rows were flipped is no
    # longer knowable, and 'video_clips_v2' remains a valid value under the
    # old default.

    op.drop_constraint("sessions_avatar_id_fkey", "sessions", type_="foreignkey")
    op.create_foreign_key(
        "sessions_avatar_id_fkey",
        "sessions",
        "avatars",
        ["avatar_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # Rows whose avatar was deleted (avatar_id nulled) cannot satisfy the old
    # NOT NULL; they are conversation history with no avatar left to attach
    # to, which the old schema could not represent at all.
    op.execute("DELETE FROM sessions WHERE avatar_id IS NULL")
    op.alter_column("sessions", "avatar_id", nullable=False)

    op.drop_constraint("sessions_producer_id_fkey", "sessions", type_="foreignkey")
    op.drop_index("ix_sessions_producer_id", table_name="sessions")
    op.drop_column("sessions", "producer_id")
