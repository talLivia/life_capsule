"""
Prompt 10 — QA harness for the retrieval pipeline (Prompts 6-8), run against
REAL Neo4j AuraDB, real Postgres (Neon), and a real Gemini API key — same
live-infra pattern as scripts/smoke_test_prompt5.py, not part of the mocked
pytest suite.

Ingests Prompt 3's exact sample segments (test_graph_memory_int.py's
SEG_ARMY / SEG_MARRIAGE / SEG_CAREER — army and marriage share the entity
"גילה", career shares nothing) through the REAL Prompt 5 pipeline
(analysis_graph.run_segment_analysis), so they land as 'ready' RawSegment
rows with real topic_tags/importance_score/embedding — the actual data
shape retrieval_service.primary_match/relevance_scorer depend on. All three
segments use "גילה" verbatim (not a fuzzy variant like the smoke test's
"גילה כהן"), so check_entities_node auto-resolves the repeated exact name
without an interrupt — this ingestion never needs resume_segment_analysis.

Then asks ~30 predefined Hebrew questions, EACH in its own fresh session_id
(so one question's visited-set/recency state never leaks into another —
every question is judged as if it were a family member's first message in a
brand new /talk conversation). For each question, reports:
  - primary match: which segment(s), if any, matched by topic
  - every candidate expand_graph considered for a related-segment bridge,
    with relevance_scorer's full score breakdown (recency/importance/
    relevance/combined) and whether it cleared RELEVANCE_THRESHOLD — using
    score_candidates(..., filter_by_threshold=False) (added for this
    harness) so REJECTED candidates are visible too, not just approved ones
  - the actual final response text (response_assembler.assemble_response —
    the exact text that would be spoken/lip-synced)
  - the verbatim source transcript(s) actually used, side by side with the
    response, plus a manual-review checkbox — per the project plan this is
    a manual review checklist, not automated hallucination detection. (In
    fact response_assembler never calls an LLM at all — every word is
    either a verbatim transcript or a fixed template string by
    construction — but the checklist stays here as the actual guard a
    human reviewer should exercise, not a claim this script pre-verifies.)

Report is written to scripts/qa_report_prompt10.md (also overwritable via
QA_REPORT_PATH env var) and a one-line pass/fail-style summary is printed
to stdout. Everything ingested is cleaned up in a finally block, matching
smoke_test_prompt5.py.

Usage: python scripts/qa_harness_prompt10.py   (run from backend/, with a
real .env in the repo root providing DATABASE_URL/NEO4J_*/GEMINI_API_KEY).
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Windows consoles default to cp1252, which can't print Hebrew.
sys.stdout.reconfigure(encoding="utf-8")

# psycopg3's async mode (LangGraph's AsyncPostgresSaver) can't run under
# Windows' default ProactorEventLoop — see smoke_test_prompt5.py.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.analysis_graph import run_segment_analysis  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.models import InterviewSession, RawSegment, User  # noqa: E402
from app.services import relevance_scorer, response_assembler, retrieval_service  # noqa: E402
from app.services import graph_memory as gm  # noqa: E402

REPORT_PATH = Path(os.environ.get("QA_REPORT_PATH", Path(__file__).parent / "qa_report_prompt10.md"))

# ── Prompt 3's exact sample segments (test_graph_memory_int.py) ─────────────
SEG_ARMY_TRANSCRIPT = (
    "שירתתי בצבא במשך שלוש שנים. הכרתי שם את גילה, שהייתה מפקדת הכיתה שלי."
)
SEG_MARRIAGE_TRANSCRIPT = "כעבור כמה שנים התחתנתי עם גילה. היא הפכה לאשתי הנפלאה."
SEG_CAREER_TRANSCRIPT = (
    "התחלתי לעבוד כמהנדס בחברת הייטק בתל אביב. המנהל שלי היה דן כהן."
)

# ── ~30 predefined Hebrew questions, spanning: direct matches to each of the
# 3 segments (several phrasings each, to exercise topic-classification
# robustness), the shared entity ("גילה") that should trigger a
# related-segment bridge between army <-> marriage, and questions with no
# matching segment at all (expect NO_STORY_FALLBACK). ─────────────────────
QUESTIONS: list[str] = [
    # army — direct
    "ספר לי על השירות הצבאי שלך",
    "מה עשית כשהיית בצבא?",
    "כמה זמן שירתת בצבא?",
    "מה למדת מהשירות הצבאי?",
    # army / גילה — shared entity, bridge candidate
    "מי הייתה המפקדת שלך בצבא?",
    "ספר לי על גילה",
    "איך הכרת את גילה?",
    # marriage — direct
    "איך הכרת את אשתך?",
    "ספר לי על החתונה שלך",
    "מתי התחתנת?",
    "מי זו אשתך?",
    "מה הרגשת ביום החתונה?",
    "מה קרה אחרי שהתחתנת?",
    "איך זה שגילה הייתה גם בצבא וגם אשתך?",
    # career — direct
    "באיזה תחום עבדת?",
    "ספר לי על הקריירה שלך",
    "מי היה המנהל שלך בעבודה?",
    "איפה עבדת כמהנדס?",
    "מה עשית בתור מהנדס בהייטק?",
    "ספר לי על דן כהן",
    # no matching segment — expect NO_STORY_FALLBACK
    "ספר לי על ילדותך",
    "מה התחביבים שלך?",
    "לאן נסעת בחופשות?",
    "מה אתה אוהב לבשל?",
    "האם היה לך חיית מחמד?",
    "איזה ספורט אתה אוהב?",
    "מה המאכל האהוב עליך?",
    "ספר לי על ההורים שלך",
    "מה עשית בסופי שבוע?",
    "איזו מוזיקה אתה אוהב?",
]


async def _seed_and_ingest(group_id: str) -> tuple[str, dict[str, str]]:
    """Seed a throwaway producer + interview session + the 3 sample segments,
    run each through the real Prompt 5 pipeline, and return
    (interview_session_id, {label: segment_id})."""
    async with AsyncSessionLocal() as db:
        user = User(
            id=group_id,
            email=f"{group_id}@qa-harness.invalid",
            username=group_id,
            hashed_password="",
            role="producer",
            recording_language="he",
        )
        db.add(user)
        await db.flush()

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
            "marriage": RawSegment(
                interview_session_id=session.id,
                question_asked="איך הכרת את בן/בת הזוג שלך",
                question_index=1,
                transcript=SEG_MARRIAGE_TRANSCRIPT,
                status="pending_analysis",
            ),
            "career": RawSegment(
                interview_session_id=session.id,
                question_asked="איך התחלת את הדרך המקצועית שלך",
                question_index=2,
                transcript=SEG_CAREER_TRANSCRIPT,
                status="pending_analysis",
            ),
        }
        for seg in segments.values():
            db.add(seg)
        await db.flush()
        segment_ids = {label: seg.id for label, seg in segments.items()}
        await db.commit()

    for label, segment_id in segment_ids.items():
        for attempt in range(1, 4):
            await run_segment_analysis(segment_id)
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(RawSegment).where(RawSegment.id == segment_id))
                seg = result.scalar_one()
            print(f"  [{label}] attempt {attempt}: status={seg.status}")
            if seg.status == "ready":
                break
            if seg.status == "pending_confirmation":
                raise RuntimeError(
                    f"{label} unexpectedly paused on human_confirm — Prompt 3's sample "
                    "segments use 'גילה' verbatim in both mentions, which should auto-resolve"
                )
            if attempt < 3:
                await asyncio.sleep(8)
        else:
            raise RuntimeError(f"{label} never reached 'ready' after 3 attempts")

    return interview_session_id, segment_ids


def _score_reason(s: relevance_scorer.ScoredSegment, threshold: float) -> str:
    verdict = "BRIDGED (passed threshold)" if s.score >= threshold else "rejected (below threshold)"
    return (
        f"{verdict} — combined={s.score:.2f} (threshold={threshold:.2f}); "
        f"recency={s.recency_score:.2f}, importance={s.importance_score:.2f}, "
        f"relevance={s.relevance_score:.2f}"
    )


async def _run_one_question(question: str, group_id: str, segment_ids: dict[str, str]) -> dict:
    session_id = f"qa-harness-{uuid.uuid4()}"  # fresh per question — no cross-question state
    label_by_id = {v: k for k, v in segment_ids.items()}

    retrieval = await retrieval_service.retrieve(question, group_id, "he", session_id)
    primary_labels = [label_by_id.get(s.segment_id, s.segment_id) for s in retrieval.primary]

    scored_all = await relevance_scorer.score_candidates(
        question,
        [c.segment_id for c in retrieval.candidates],
        session_id,
        group_id,
        filter_by_threshold=False,
    )
    candidate_report = [
        {
            "label": label_by_id.get(s.segment_id, s.segment_id),
            "reason": _score_reason(s, relevance_scorer.RELEVANCE_THRESHOLD),
        }
        for s in scored_all
    ]

    final_text = await response_assembler.assemble_response(
        question=question, group_id=group_id, recording_language="he", session_id=session_id
    )

    used_labels = list(primary_labels)
    used_labels += [c["label"] for c in candidate_report if "BRIDGED" in c["reason"]]

    return {
        "question": question,
        "primary": primary_labels,
        "candidates": candidate_report,
        "response": final_text,
        "used_labels": used_labels,
    }


def _render_report(results: list[dict], transcripts_by_label: dict[str, str]) -> str:
    no_match_count = sum(1 for r in results if not r["primary"])
    bridged_count = sum(1 for r in results if any("BRIDGED" in c["reason"] for c in r["candidates"]))

    lines = [
        "# Prompt 10 QA report — retrieval pipeline (Prompts 6-8)",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Questions: {len(results)}",
        f"Primary match found: {len(results) - no_match_count}/{len(results)}  |  "
        f"No-story fallback: {no_match_count}/{len(results)}  |  "
        f"Bridged at least one related segment: {bridged_count}/{len(results)}",
        "",
        "Manual review checklist (per the project plan — not automated): for each "
        "question below, confirm the **Response** contains ONLY text that appears "
        "verbatim in the **Source transcript(s) used** — no paraphrasing, no added "
        "detail, nothing invented.",
        "",
        "## Known limitations (intentional, not bugs — revisit only if retrieval scope expands)",
        "",
        "- **Unnamed role/relationship questions** (e.g. \"who was your commander?\", "
        "\"tell me about your boss\") get no benefit from the ENTITY signal — no proper "
        "name was mentioned, so there's nothing to resolve against the graph. If the "
        "segment's own transcript never happens to use that same role word (TOPIC) and "
        "the question's embedding doesn't independently clear SEMANTIC_MATCH_THRESHOLD, "
        "the question gets the no-story fallback even though a human reading the "
        "transcript would immediately know who's meant. This is the deliberate boundary "
        "of the project's \"never invent or guess\" principle (response_assembler.py's "
        "zero-LLM-call guarantee, NO_STORY_FALLBACK) — resolving an unnamed role to a "
        "specific real person would require exactly the kind of inference this project "
        "avoids throughout. See retrieval_service.py's module docstring for the full "
        "note.",
        "",
        "---",
        "",
    ]

    for i, r in enumerate(results, 1):
        lines.append(f"## {i}. {r['question']}")
        lines.append("")
        lines.append(f"- **Primary match:** {', '.join(r['primary']) if r['primary'] else 'none (no-story fallback)'}")
        if r["candidates"]:
            lines.append("- **Candidates considered for bridging:**")
            for c in r["candidates"]:
                lines.append(f"  - `{c['label']}`: {c['reason']}")
        else:
            lines.append("- **Candidates considered for bridging:** none")
        lines.append(f"- **Response:** {r['response']}")
        if r["used_labels"]:
            lines.append("- **Source transcript(s) used** (verify response is verbatim from these):")
            for label in r["used_labels"]:
                transcript = transcripts_by_label.get(label, "<unknown>")
                lines.append(f"  - `{label}`: {transcript}")
        lines.append("- [ ] REVIEWED — response contains no content beyond the source transcript(s) above")
        lines.append("")

    return "\n".join(lines)


async def main() -> None:
    print("=" * 70)
    print("Prompt 10 QA harness — retrieval pipeline against real Neo4j + Gemini + Postgres")
    print("=" * 70)

    group_id = f"qa-harness-{uuid.uuid4()}"
    interview_session_id = None
    segment_ids: dict[str, str] = {}
    transcripts_by_label = {"army": SEG_ARMY_TRANSCRIPT, "marriage": SEG_MARRIAGE_TRANSCRIPT, "career": SEG_CAREER_TRANSCRIPT}

    try:
        print("\n--- Ingesting Prompt 3's sample segments through the real Prompt 5 pipeline ---")
        interview_session_id, segment_ids = await _seed_and_ingest(group_id)
        print(f"\nAll 3 segments reached 'ready'. group_id={group_id}\n")

        print(f"--- Running {len(QUESTIONS)} questions (each in its own fresh session) ---")
        results = []
        for i, q in enumerate(QUESTIONS, 1):
            if i > 1:
                # Gemini's free tier rate-limits generate_content to ~5/minute
                # per model (observed directly during Prompt 5's live testing)
                # — each question makes at least one such call (topic
                # classification). Pace at ~4/minute to stay clear of it.
                await asyncio.sleep(15)
            print(f"  [{i}/{len(QUESTIONS)}] {q}")
            try:
                results.append(await _run_one_question(q, group_id, segment_ids))
            except Exception as e:
                print(f"    ERROR: {e}")
                results.append(
                    {
                        "question": q,
                        "primary": [],
                        "candidates": [],
                        "response": f"<HARNESS ERROR — question failed, not a pipeline result: {e}>",
                        "used_labels": [],
                    }
                )

        report = _render_report(results, transcripts_by_label)
        REPORT_PATH.write_text(report, encoding="utf-8")

        no_match = sum(1 for r in results if not r["primary"])
        bridged = sum(1 for r in results if any("BRIDGED" in c["reason"] for c in r["candidates"]))
        print("\n" + "=" * 70)
        print(f"RESULT: {len(results)} questions run — {len(results) - no_match} primary matches, "
              f"{no_match} no-story fallbacks, {bridged} bridged a related segment.")
        print(f"Full report written to {REPORT_PATH}")
        print("Manual review still required per the project plan — open the report and work "
              "through the checklist for each question.")
        print("=" * 70)

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
                result = await db.execute(select(User).where(User.id == group_id))
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


if __name__ == "__main__":
    asyncio.run(main())
