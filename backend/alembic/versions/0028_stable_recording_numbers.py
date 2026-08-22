"""Stable recording numbers (UNIT_ID_STABILITY_PLAN §1, §3 step 1).

Adds raw_segments.recording_no (the anchor for the scoped unit-id scheme
r<recording_no>u<local>) and users.recording_seq (the per-producer
high-water counter that makes assignment strictly monotonic — a naive
MAX+1 would reuse the number of a deleted-then-re-recorded newest
recording, silently re-pointing anything that referenced it).

Backfill numbers existing recordings 1..N per producer in (created_at,
id) order — deliberately NOT the archive's presentation order (which
sibling-groups by question_index, a per-category index with cross-
category collisions; a discovered pre-existing quirk recorded in
PROJECT_STATUS): recording_no is an ANCHOR, not an ordering, and simple
chronological numbering is deterministic and auditable. recording_seq
backfills to each producer's segment count.
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("raw_segments", sa.Column("recording_no", sa.Integer(), nullable=True))
    op.add_column(
        "users",
        sa.Column("recording_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        """
        WITH numbered AS (
            SELECT rs.id AS seg_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY i.user_id
                       ORDER BY rs.created_at, rs.id
                   ) AS rn
            FROM raw_segments rs
            JOIN interview_sessions i ON rs.interview_session_id = i.id
        )
        UPDATE raw_segments
        SET recording_no = numbered.rn
        FROM numbered
        WHERE raw_segments.id = numbered.seg_id
        """
    )
    op.execute(
        """
        WITH counts AS (
            SELECT i.user_id AS uid, COUNT(*) AS n
            FROM raw_segments rs
            JOIN interview_sessions i ON rs.interview_session_id = i.id
            GROUP BY i.user_id
        )
        UPDATE users
        SET recording_seq = counts.n
        FROM counts
        WHERE users.id = counts.uid
        """
    )


def downgrade() -> None:
    op.drop_column("raw_segments", "recording_no")
    op.drop_column("users", "recording_seq")
