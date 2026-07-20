"""
Live smoke test for Prompt 5's analysis_graph.py — run against REAL Neo4j
AuraDB, a real Postgres (Neon, also backing LangGraph's AsyncPostgresSaver
checkpointer), and a real Gemini API key. Not part of the pytest suite
(tests/test_analysis_graph.py covers the pipeline with mocks); this is a
one-off, human-readable validation that the whole live stack actually works
together, including the human_confirm interrupt()/resume pause.

Synthetic content only — three short Hebrew story segments in the same
style as Prompt 3's integration test (test_graph_memory_int.py):
  1. Army story introducing an entity, "גילה" (Gila) — first mention, no
     existing graph data, so check_entities should auto-continue with no
     interrupt.
  2. An unrelated career story — shares no entities with segment 1, sanity
     check that independent segments ingest cleanly.
  3. A marriage story mentioning "גילה כהן" (Gila Cohen) — a fuzzy-but-not-
     exact match against the "גילה" node segment 1 already created, which
     should trigger human_confirm's interrupt(). The script answers "yes,
     same person" (this Gila Cohen IS the army commander from segment 1),
     resumes the paused pipeline, and confirms it reaches 'ready'.

Everything is cleaned up in a finally block: the throwaway Postgres rows
(user/interview_session/raw_segments) and the Neo4j group_id's data.

Usage: python scripts/smoke_test_prompt5.py   (run from backend/, with a
real .env in the repo root providing DATABASE_URL/NEO4J_*/GEMINI_API_KEY).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

# Windows consoles default to cp1252, which can't print Hebrew — force UTF-8
# so this script's own output doesn't crash on the content it's testing.
sys.stdout.reconfigure(encoding="utf-8")

# psycopg3's async mode (LangGraph's AsyncPostgresSaver) can't run under
# Windows' default ProactorEventLoop — only matters when running this
# script bare on Windows, since the real deployment (Fly.io/Docker) runs
# Linux, where this isn't an issue and the default loop already works.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Let this run as `python scripts/smoke_test_prompt5.py` from backend/ (or
# anywhere) without needing `python -m` or a manually-set PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.analysis_graph import resume_segment_analysis, run_segment_analysis  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.models import InterviewSession, RawSegment, User  # noqa: E402
from app.services import graph_memory as gm  # noqa: E402

SEG_ARMY_TRANSCRIPT = (
    "שירתתי בצבא במשך שלוש שנים. הכרתי שם את גילה, שהייתה מפקדת הכיתה שלי. "
    "היא לימדה אותי המון על אחריות ומנהיגות."
)
SEG_CAREER_TRANSCRIPT = (
    "התחלתי לעבוד כמהנדס בחברת הייטק בתל אביב. המנהל שלי היה דן כהן, "
    "ולמדתי ממנו איך לבנות מוצרים טובים."
)
SEG_MARRIAGE_TRANSCRIPT = (
    "כעבור כמה שנים התחתנתי עם גילה כהן, אותה גילה שהייתה המפקדת שלי בצבא. "
    "זו הייתה החתונה הכי משמחת שהייתה לי."
)


async def _run_until_settled(segment_id: str, label: str, attempts: int = 3) -> str:
    """Run the pipeline, retrying the whole segment (cheap — transcribe_node
    short-circuits once transcript is set) if a transient provider error
    (Gemini 503s were observed during this exact validation pass) left the
    segment 'failed' rather than 'ready'/'pending_confirmation'."""
    for attempt in range(1, attempts + 1):
        await run_segment_analysis(segment_id)
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(RawSegment).where(RawSegment.id == segment_id))
            segment = result.scalar_one()
        print(f"  [{label}] attempt {attempt}: status={segment.status}")
        if segment.status != "failed":
            return segment.status
        if attempt < attempts:
            print(f"  [{label}] retrying after transient failure…")
            await asyncio.sleep(8)
    return "failed"


async def main() -> None:
    print("=" * 70)
    print("Prompt 5 live smoke test — analysis_graph.py against real")
    print("Neo4j + Gemini + Postgres (incl. LangGraph's AsyncPostgresSaver)")
    print("=" * 70)

    group_id = f"smoke-test-{uuid.uuid4()}"
    user_id = None
    interview_session_id = None
    segment_ids: dict[str, str] = {}
    failures: list[str] = []

    try:
        # ── Setup: throwaway producer + interview session + 3 segments ──
        async with AsyncSessionLocal() as db:
            user = User(
                id=group_id,  # group_id IS the producer's user_id (Prompt 5 design)
                email=f"{group_id}@smoke-test.invalid",
                username=group_id,
                hashed_password="",
                role="producer",
                recording_language="he",
            )
            db.add(user)
            await db.flush()
            user_id = user.id

            session = InterviewSession(user_id=user.id, status="active")
            db.add(session)
            await db.flush()
            interview_session_id = session.id

            segments = {
                "army": RawSegment(
                    interview_session_id=session.id,
                    question_asked="ספר לי על השירות הצבאי שלך",
                    question_index=0,
                    transcript=SEG_ARMY_TRANSCRIPT,
                    status="pending_analysis",
                ),
                "career": RawSegment(
                    interview_session_id=session.id,
                    question_asked="איך התחלת את הדרך המקצועית שלך",
                    question_index=1,
                    transcript=SEG_CAREER_TRANSCRIPT,
                    status="pending_analysis",
                ),
                "marriage": RawSegment(
                    interview_session_id=session.id,
                    question_asked="איך הכרת את בן/בת הזוג שלך",
                    question_index=2,
                    transcript=SEG_MARRIAGE_TRANSCRIPT,
                    status="pending_analysis",
                ),
            }
            for seg in segments.values():
                db.add(seg)
            await db.flush()
            segment_ids = {label: seg.id for label, seg in segments.items()}
            await db.commit()

        print(f"\nSeeded throwaway producer {user_id} / session {interview_session_id}")
        print(f"group_id (== user_id, per Prompt 5's per-producer graph partitioning): {group_id}\n")

        # ── Segment 1: army story, first mention of גילה — no ambiguity ──
        print("--- Segment 1: army story (introduces 'גילה') ---")
        status = await _run_until_settled(segment_ids["army"], "army")
        if status != "ready":
            failures.append(f"army segment ended in status={status!r}, expected 'ready'")
        else:
            print("  PASS: reached 'ready' with no interrupt (as expected — first mention)\n")

        # ── Segment 2: unrelated career story ──
        print("--- Segment 2: career story (unrelated entities) ---")
        status = await _run_until_settled(segment_ids["career"], "career")
        if status != "ready":
            failures.append(f"career segment ended in status={status!r}, expected 'ready'")
        else:
            print("  PASS: reached 'ready' with no interrupt\n")

        # ── Segment 3: marriage story, 'גילה כהן' fuzzy-matches 'גילה' ──
        print("--- Segment 3: marriage story ('גילה כהן' — should trigger human_confirm) ---")
        marriage_id = segment_ids["marriage"]
        await run_segment_analysis(marriage_id)
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(RawSegment).where(RawSegment.id == marriage_id))
            segment = result.scalar_one()

        if segment.status != "pending_confirmation":
            failures.append(
                f"marriage segment ended in status={segment.status!r} instead of pausing "
                "on 'pending_confirmation' — the interrupt()/ambiguity path did not trigger"
            )
            print(f"  FAIL: status={segment.status!r} (expected 'pending_confirmation')")
        else:
            pc = segment.pending_confirmation
            print("  PASS: pipeline paused on human_confirm. Live interrupt payload:")
            print(f"    entity_name:       {pc['entity_name']}")
            print(f"    candidate_name:    {pc['candidate_name']}")
            print(f"    candidate_summary: {pc['candidate_summary']}")
            print(f"    question:          {pc['question']}")

            print("\n  Answering: yes, same person (as designed in this synthetic story) …")
            await resume_segment_analysis(
                marriage_id,
                {"same_as_existing": True, "candidate_uuid": pc["candidate_uuid"]},
            )
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(RawSegment).where(RawSegment.id == marriage_id))
                segment = result.scalar_one()

            if segment.status == "ready":
                print("  PASS: resumed successfully, pipeline reached 'ready'\n")
            else:
                failures.append(
                    f"marriage segment after resume ended in status={segment.status!r}, "
                    "expected 'ready'"
                )
                print(f"  FAIL: status={segment.status!r} after resume (expected 'ready')\n")

        # ── Verify the graph actually linked army <-> marriage via גילה ──
        print("--- Verifying graph_memory reflects the confirmed entity link ---")
        related = await gm.find_related_episodes(
            entity_names=["גילה"], exclude_ids=[segment_ids["army"]], group_id=group_id
        )
        if segment_ids["marriage"] in related:
            print(f"  PASS: find_related_episodes('גילה') includes the marriage segment: {related}")
        else:
            failures.append(
                f"find_related_episodes('גילה') did not include the marriage segment "
                f"(got {related}) — custom_extraction_instructions may not have linked them"
            )
            print(f"  FAIL: marriage segment missing from related episodes: {related}")
        if segment_ids["career"] in related:
            failures.append("career segment (unrelated) unexpectedly showed up as related to 'גילה'")

    finally:
        print("\n--- Cleanup ---")
        try:
            async with AsyncSessionLocal() as db:
                if interview_session_id:
                    result = await db.execute(
                        select(InterviewSession).where(InterviewSession.id == interview_session_id)
                    )
                    session = result.scalar_one_or_none()
                    if session:
                        await db.delete(session)  # cascades to raw_segments
                if user_id:
                    result = await db.execute(select(User).where(User.id == user_id))
                    user = result.scalar_one_or_none()
                    if user:
                        await db.delete(user)
                await db.commit()
            print("  Postgres rows deleted (user/session/segments).")
        except Exception as e:
            print(f"  WARNING: Postgres cleanup failed: {e}")

        try:
            from graphiti_core.utils.maintenance.graph_data_operations import clear_data

            await clear_data(gm.get_graphiti().driver, group_ids=[group_id])
            print("  Neo4j group_id data cleared.")
        except Exception as e:
            print(f"  WARNING: Neo4j cleanup failed: {e}")

    print("\n" + "=" * 70)
    if failures:
        print(f"RESULT: FAILED ({len(failures)} issue(s))")
        for f in failures:
            print(f"  - {f}")
        print("=" * 70)
        sys.exit(1)
    else:
        print("RESULT: ALL CHECKS PASSED")
        print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
