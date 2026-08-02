"""
Recover the stable `question_id` for recordings made before it was stored.

Phase 1b of docs/FAMILY_TREE_TIMELINE.md.

⏳ THIS HAS A DEADLINE. Recovery works by matching `raw_segments.question_asked`
against the question TEXT in interview_questions.json. That match only holds
while the JSON still contains the wording those recordings were made with —
reword or remove a question and its recordings become unattributable to a life
period, permanently. **Run this before editing interview_questions.json.**

Exact match, never fuzzy. A near-match that guessed wrong would file a
recording under the wrong life period, which is worse than leaving it NULL:
NULL is visibly missing, a wrong category silently lies.

Unmatched rows are reported and left NULL rather than failing the run — an
uploaded video answering something outside the guided set legitimately has no
question id.

    python scripts/backfill_question_ids.py            # report only
    python scripts/backfill_question_ids.py --apply    # write
"""

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app import interview_config  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.models import RawSegment  # noqa: E402


async def main(apply: bool) -> int:
    async with AsyncSessionLocal() as db:
        segments = (
            await db.execute(
                select(RawSegment).where(RawSegment.question_id.is_(None))
            )
        ).scalars().all()

        total = (await db.execute(select(RawSegment))).scalars().all()
        print(f"segments total          : {len(total)}")
        print(f"already have question_id: {len(total) - len(segments)}")
        print(f"needing recovery        : {len(segments)}\n")
        if not segments:
            print("nothing to do.")
            return 0

        matched: Counter = Counter()
        unmatched = []
        for seg in segments:
            qid = interview_config.question_id_for_text(seg.question_asked)
            if qid:
                seg.question_id = qid
                matched[qid] += 1
            else:
                unmatched.append(seg)

        print("recovered by exact text match:")
        for qid, n in sorted(matched.items()):
            cat = interview_config.category_for_question_id(qid)
            print(f"  {qid:<28} {cat:<18} {n} recording(s)")

        if unmatched:
            print(f"\nUNMATCHED — left NULL ({len(unmatched)}):")
            for seg in unmatched:
                print(f"  {seg.id}  q{seg.question_index}  {(seg.question_asked or '')[:60]!r}")
            print("\n  These answer something outside the guided set, or their")
            print("  wording no longer matches the JSON. They will have no life")
            print("  period on the timeline, which is the honest outcome.")

        recovered = sum(matched.values())
        if apply:
            await db.commit()
            print(f"\napplied: {recovered} recovered, {len(unmatched)} left NULL")
        else:
            await db.rollback()
            print(f"\nDRY RUN — would recover {recovered}, leave {len(unmatched)} NULL."
                  f" Re-run with --apply")
        return len(unmatched)


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
