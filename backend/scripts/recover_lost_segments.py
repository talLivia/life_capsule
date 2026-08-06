"""Find and recover recordings whose content never reached the archive.

Two distinct failures, both SILENT — the producer records a story and nothing
anywhere shows it is missing. Found by a producer noticing absent content, one
recording at a time, which is not a strategy.

  STRANDED. The run reached `human_confirm` (progress_stage proves it) but its
  status was never synced, so it sits in neither `ready` nor
  `pending_confirmation`. It is therefore invisible to /talk, which loads
  `status == "ready"` only, AND absent from the notification bell, which
  queries `status == "pending_confirmation"`. There is no surface on which it
  appears at all.

  EXTRACTED NOTHING. The run completed and stored zero entities from a
  transcript that names people or places. Ingestion runs the weakest model in
  the config, and a transient miss leaves no trace: no entity, no tree entry,
  no confirmation question, and nothing that distinguishes it from a recording
  that genuinely mentioned nobody.

Recovery re-runs the entity portion from the STORED transcript — it never
re-transcribes, so unit boundaries are untouched and the eval's reference
ranges stay comparable (unlike reingest_archive.py, which deliberately does
re-transcribe). Re-ingest is idempotent: `entity_store` replaces a segment's
mentions rather than appending.

Calls the graph nodes directly rather than re-invoking the LangGraph thread,
for the reason reingest_archive.py records: a completed thread resumes oddly,
and `human_confirm` would interrupt a batch run waiting for input. A segment
that WOULD have asked a question is reported instead, and left for the
producer to answer through the normal screen.

Usage:
    python scripts/recover_lost_segments.py            # dry run: report only
    python scripts/recover_lost_segments.py --apply
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from app import analysis_graph as ag  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    EntityMention,
    InterviewSession,
    RawSegment,
    User,
)
from app.services.entity_extraction import extract  # noqa: E402

#: Statuses a finished run can legitimately hold. Anything else, with a
#: progress_stage set, means the run started and never landed.
SETTLED = ("ready", "analyzed", "pending_confirmation", "failed")


async def find_stranded() -> list:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(RawSegment, User.username)
                .join(InterviewSession, InterviewSession.id == RawSegment.interview_session_id)
                .join(User, User.id == InterviewSession.user_id)
                .where(RawSegment.status.notin_(SETTLED))
                .order_by(RawSegment.created_at)
            )
        ).all()
    return [(s, u) for s, u in rows]


async def find_empty_extractions() -> list:
    """Ready segments with a transcript and no entities, where re-extracting
    DOES find some. Costs one LLM call per candidate, so it only ever looks at
    segments that stored nothing."""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(RawSegment, User.username)
                .join(InterviewSession, InterviewSession.id == RawSegment.interview_session_id)
                .join(User, User.id == InterviewSession.user_id)
                .where(RawSegment.status.in_(("ready", "analyzed")))
                .where(RawSegment.transcript.isnot(None))
                .order_by(RawSegment.created_at)
            )
        ).all()
        candidates = []
        for segment, username in rows:
            mentions = (
                await db.execute(
                    select(func.count())
                    .select_from(EntityMention)
                    .where(EntityMention.raw_segment_id == segment.id)
                )
            ).scalar()
            if mentions == 0:
                candidates.append((segment, username))

    missed = []
    for segment, username in candidates:
        entities, _ = await extract(segment.transcript)
        if entities:
            missed.append((segment, username, [e.name for e in entities]))
    return missed


async def recover(segment_id: str, producer_id: str) -> dict:
    """Re-run the entity portion from the stored transcript."""
    state: dict = {"segment_id": segment_id, "group_id": producer_id}
    async with AsyncSessionLocal() as db:
        segment = (
            await db.execute(select(RawSegment).where(RawSegment.id == segment_id))
        ).scalar_one()
        state["transcript"] = segment.transcript or ""

    state.update(await ag.check_entities_node(state) or {})
    state.update(await ag.score_importance_node(state) or {})
    state.update(await ag.finalize_ingest_node(state) or {})
    return state


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually recover")
    args = parser.parse_args()

    stranded = await find_stranded()
    print(f"STRANDED (status not in {SETTLED}): {len(stranded)}")
    for segment, username in stranded:
        print(
            f"  {segment.id[:8]} user={username} status={segment.status!r} "
            f"stage={segment.progress_stage!r}"
        )
        print(f"      {(segment.transcript or '(no transcript)')[:70]}")

    print()
    print("Checking ready segments that stored no entities (one LLM call each)…")
    missed = await find_empty_extractions()
    print(f"EXTRACTED NOTHING but should have: {len(missed)}")
    for segment, username, names in missed:
        print(f"  {segment.id[:8]} user={username} would find: {names}")
        print(f"      {segment.transcript[:70]}")

    targets = [(s, u) for s, u in stranded] + [(s, u) for s, u, _ in missed]
    if not targets:
        print("\nNothing to recover.")
        return 0
    if not args.apply:
        print(f"\nDRY RUN — {len(targets)} segment(s) would be recovered. Re-run with --apply.")
        return 0

    print(f"\nRecovering {len(targets)}…")
    for segment, username in targets:
        async with AsyncSessionLocal() as db:
            session = (
                await db.execute(
                    select(InterviewSession).where(
                        InterviewSession.id == segment.interview_session_id
                    )
                )
            ).scalar_one()
        result = await recover(segment.id, session.user_id)
        async with AsyncSessionLocal() as db:
            after = (
                await db.execute(select(RawSegment).where(RawSegment.id == segment.id))
            ).scalar_one()
            mentions = (
                await db.execute(
                    select(func.count())
                    .select_from(EntityMention)
                    .where(EntityMention.raw_segment_id == segment.id)
                )
            ).scalar()
        asked = [q["name"] for q in result.get("names_to_check") or []]
        print(f"  {segment.id[:8]} -> status={after.status!r} entities={mentions}")
        if asked:
            # Reported rather than answered: this script must never decide a
            # disambiguation on the producer's behalf.
            print(f"      NEEDS CONFIRMATION for: {asked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
