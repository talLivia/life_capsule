"""
Complete the recorded parentage of the producer's siblings — 2026-08-10.

THE RECORD OF A ONE-TIME DATA FIX, kept as a script the way
import_graph_entities.py is: readable, diffable, dry-run by default.

## What was wrong

The tree drew the producer's own generation as two separate descents:

  * Tal hung from BOTH parents (צבי + אילנה) — recorded at ingestion;
  * רז, עדי and חן hung from אילנה ALONE — each added by hand from the tree,
    where the editor sets one relation per save, so the second parent was
    simply never stated;
  * ניר had NO parent edges at all — only a sibling edge to Tal, which places
    him in the row but hangs him from nothing.

The renderer was right to draw that: children are grouped by their EXACT
recorded parent set, and these were three different parent sets. The producer
reported it as a rendering bug ("my siblings share one fork line and I am on
a stub") — but the fork is drawn per recorded family, and the recorded
families genuinely differed. The fix is the missing edges, stated by the
producer in that same report: all five are children of the same two parents.

## What this writes

child edges (the same shape the tree editor writes, X -child-> parent):

  רז   -child-> צבי
  עדי  -child-> צבי
  חן   -child-> צבי
  ניר  -child-> צבי
  ניר  -child-> אילנה

Through entity_store.set_relation_by_hand — the manual-edit primitive — so
origin='manual', contradiction replacement, and the ask-once stamps behave
exactly as if each had been saved from the tree page.

⚠️ Do NOT run this before the generation-sign fix in set_relation_by_hand
(same commit). With the flipped sign, placing ניר as a parent's child computed
his target generation as -2 and DELETED his agreeing sibling, spouse and
children edges as "contradictions".

Usage:
    python scripts/complete_sibling_parentage.py          # dry run, prints plan
    python scripts/complete_sibling_parentage.py --apply  # writes
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("DEBUG", "false")
sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models import Entity, EntityRelation  # noqa: E402
from app.services import entity_store  # noqa: E402

PRODUCER_ID = "79820a49-b07d-41fe-941b-f5ceba09f7b5"

#: (child name, parent name) — every edge this script exists to add.
EDGES: list[tuple[str, str]] = [
    ("רז", "צבי"),
    ("עדי", "צבי"),
    ("חן", "צבי"),
    ("ניר", "צבי"),
    ("ניר", "אילנה"),
]


async def main(apply: bool) -> None:
    async with AsyncSessionLocal() as db:
        people = {
            e.name: e
            for e in (
                await db.execute(
                    select(Entity).where(
                        Entity.producer_id == PRODUCER_ID, Entity.type == "person"
                    )
                )
            ).scalars().all()
        }
        missing = {n for pair in EDGES for n in pair} - set(people)
        if missing:
            raise SystemExit(f"Missing entities, refusing to run: {sorted(missing)}")

        for child_name, parent_name in EDGES:
            child, parent = people[child_name], people[parent_name]
            exists = (
                await db.execute(
                    select(EntityRelation).where(
                        EntityRelation.from_entity_id == child.id,
                        EntityRelation.to_entity_id == parent.id,
                        EntityRelation.relation_type == "child",
                    )
                )
            ).scalars().first()
            if exists:
                print(f"already recorded: {child_name} -child-> {parent_name}")
                continue
            if not apply:
                print(f"would write:      {child_name} -child-> {parent_name}")
                continue
            result = await entity_store.set_relation_by_hand(
                db,
                producer_id=PRODUCER_ID,
                from_entity_id=child.id,
                to_entity_id=parent.id,
                relation_type="child",
            )
            replaced = [r["relation_type"] for r in result["replaced"]]
            if replaced:
                # Nothing here contradicts anything — a replacement means the
                # sign fix is absent or something unexpected is in the data.
                raise SystemExit(
                    f"UNEXPECTED replacement writing {child_name} -child-> "
                    f"{parent_name}: {replaced}. Rolling back."
                )
            await entity_store.mark_placement_asked(db, [child.id, parent.id])
            print(f"written:          {child_name} -child-> {parent_name}")

        if apply:
            await db.commit()
            print("committed.")
        else:
            print("dry run — nothing written. Re-run with --apply.")


if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("--apply", action="store_true")
    asyncio.run(main(cli.parse_args().apply))
