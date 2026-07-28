"""Chunk 2: import the existing Graphiti/Neo4j entities into Postgres.

One-shot, idempotent, and deliberately NOT clever: it feeds the same
`entity_store.write_segment_entities` the live ingestion path uses, one call
per recording. That is the point — the merge rule, the mention dedup and the
orphan sweep are then the tested code, not a second implementation written for
the import that could disagree with it.

WHY THE CLASSIFICATION IS HARDCODED HERE
Types and summary attribution below are HUMAN decisions, recorded in
docs/PROJECT_STATUS.md and reproduced here so this script is auditable on its
own. There are eleven entities; an LLM pass would add a source of error to a
job small enough to read in one screen, and nine of the eleven state their
type outright in their own summary.

THE SUMMARY ATTRIBUTION PROBLEM
`entity_mentions.summary` is what ONE recording said. Graphiti stored ONE
summary per entity, consolidated across every recording that mentioned it, so
splitting it back apart is not always possible:

  * Nine of the ten imported entities are mentioned by exactly ONE recording.
    Their summary therefore describes that one recording and nothing else —
    attribution is exact and lossless.

  * מונטריאול is mentioned by TWO. Its stored summary reads "the place he flew
    to for a year and a half right after discharge from the air force", which
    is a restatement of 5d128933 ("right after discharge I flew to Montreal for
    a year and a half") and contains nothing at all from 097b606b ("when I was
    in Montreal I studied programming"). So the summary is not really
    consolidated — Graphiti kept one recording's account and dropped the
    other's.

    It is therefore attributed to 5d128933, and 097b606b's mention gets NULL.
    Copying it onto both would make the career recording claim something it
    never said, which is the exact failure per-mention summaries exist to
    prevent. NULL is honest: Graphiti never recorded what that recording said
    about Montreal. Re-running the segment through the normal ingestion path
    regenerates a true per-mention summary, because chunk 1b's extractor
    produces one per recording by construction.

עכבר IS SKIPPED. It is a common noun ("a mouse"), not a named entity — the old
extractor should never have produced it, and the new prompt now says so
outright. Importing it would carry a known extraction bug forward into the
table that replaces it.

Usage:
    python scripts/import_graph_entities.py            # dry run
    python scripts/import_graph_entities.py --apply
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models import Entity, EntityMention, RawSegment  # noqa: E402
from app.services import entity_store  # noqa: E402
from app.services.entity_extraction import ExtractedEntity  # noqa: E402

PRODUCER_ID = "79820a49-b07d-41fe-941b-f5ceba09f7b5"

# Segment ids, named for what they are so the table below reads.
HOME = "502fb283-8f10-4ba2-adb3-d8dc6dc16f24"  # q0  the house I grew up in
ARMY = "ab5f6318-289e-4897-8a9e-43b064d1d994"  # q3  military service
DISCHARGE = "5d128933-4000-4e97-8666-fc46131d1a86"  # q5  right after discharge
CAREER = "097b606b-ca75-47b2-a3b3-7e7eaee83b26"  # q10 professional path

# (segment, name, type, summary) — verbatim from the graph unless noted.
# Types per docs/PROJECT_STATUS.md: 6 person, 2 place, 2 organisation.
IMPORT: list[tuple[str, str, str, str | None]] = [
    (HOME, "אילנה", "person", "אמא של הדובר."),
    (HOME, "צבי", "person", "אבא של הדובר."),
    (HOME, "ניר", "person", "אח של הדובר."),
    (HOME, "חן", "person", "אח של הדובר."),
    (HOME, "עדי", "person", "אח של הדובר."),
    (HOME, "רז", "person", "אח של הדובר."),
    (HOME, "טבריה", "place", "מקום מגוריו של הדובר עד גיל 14."),
    # Confirmed a boarding school, so organisation rather than place — the
    # one classification here that the summary alone does not settle.
    (HOME, "הכפר הירוק", "organisation", "מקום לימודיו של הדובר מגיל 14."),
    (
        ARMY,
        "חיל האוויר",
        "organisation",
        "המשתמש שירת בחיל האוויר במשך שלוש שנים כטכנאי טילים, יצא הביתה פעם "
        "בשבועיים, רכש חברים רבים והתעסק בנושאים מעניינים.",
    ),
    # The one entity with two mentions. See THE SUMMARY ATTRIBUTION PROBLEM.
    (
        DISCHARGE,
        "מונטריאול",
        "place",
        "מונטריאול היא המקום אלו טס המדבר למשך שנה וחצי מיד אחרי השחרור מחיל האוויר.",
    ),
    (CAREER, "מונטריאול", "place", None),
]

# Extracted, then confirmed as not a named entity. Listed rather than merely
# absent, so a later reader can tell "decided against" from "overlooked".
SKIPPED = [("800e321e-6bf0-4a04-8c3b-648cb2228a52", "עכבר", "common noun, not a named entity")]


async def main(apply: bool) -> int:
    by_segment: dict[str, list[ExtractedEntity]] = {}
    for segment_id, name, type_, summary in IMPORT:
        by_segment.setdefault(segment_id, []).append(
            ExtractedEntity(name=name, type=type_, alternative_type=None, summary=summary)
        )

    async with AsyncSessionLocal() as db:
        # Every mention's raw_segment_id is a required FK. A segment that has
        # been deleted since the graph recorded it cannot receive a mention at
        # all, so check before writing rather than failing halfway through.
        found = set(
            (
                await db.execute(
                    select(RawSegment.id).where(RawSegment.id.in_(list(by_segment)))
                )
            )
            .scalars()
            .all()
        )
        missing = set(by_segment) - found
        if missing:
            print(f"ABORT: these segments no longer exist in Postgres: {sorted(missing)}")
            return 1

        for segment_id, entities in by_segment.items():
            print(f"\n{segment_id[:8]}  ({len(entities)} entities)")
            for e in entities:
                shown = e.summary if e.summary else "<NULL — not attributable>"
                print(f"    {e.name:14} {e.type:13} {shown}")
            if apply:
                result = await entity_store.write_segment_entities(
                    db, segment_id=segment_id, producer_id=PRODUCER_ID, entities=entities
                )
                print(
                    f"    -> created={result.entities_created} "
                    f"matched={result.entities_matched} mentions={result.mentions_written}"
                )

        for segment_id, name, why in SKIPPED:
            print(f"\n{segment_id[:8]}  SKIPPED {name} — {why}")

        if not apply:
            await db.rollback()
            print("\nDRY RUN — nothing written. Re-run with --apply.")
            return 0

        await db.commit()

        entities = (
            (
                await db.execute(
                    select(Entity)
                    .where(Entity.producer_id == PRODUCER_ID)
                    .where(~Entity.is_self)
                )
            )
            .scalars()
            .all()
        )
        mentions = (
            (await db.execute(select(EntityMention))).scalars().all()
        )
        print(f"\nAPPLIED: {len(entities)} entities, {len(mentions)} mentions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--apply" in sys.argv)))
