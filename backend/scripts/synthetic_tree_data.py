"""TEMPORARY test data for stress-testing the family tree layout.

NOT real content. Every row this writes is tagged with an id starting
`synthtree-`, and `--clean` deletes exactly those rows and nothing else.

## What it touches, and what it deliberately does not

Writes to TWO tables only: `entities` and `entity_relations`.

It does **not** create recordings. No `raw_segments`, no `entity_mentions`, no
`interview_sessions`, no `messages`. That matters because a synthetic recording
would count as a real interview answer — "answered" is a DISTINCT
`question_index` over `raw_segments` — and because an entity reaches the
archive and the timeline through `entity_mentions`, which is exactly what is
not written here. Synthetic people therefore appear in the family tree and
nowhere else, and clicking one honestly says no recordings mention them.

`entity_relations.source_segment_id` is NOT NULL with a foreign key to
`raw_segments`, so a relation cannot exist without naming a recording. Rather
than fabricate one, every synthetic relation points at an EXISTING real
segment. Nothing in the UI surfaces that provenance, the rows are deleted
afterwards, and the alternative — inventing a recording — is the thing we are
avoiding.

Both directions of the run print a census of the tables this must not touch,
so "nothing else changed" is demonstrated rather than asserted.

    python scripts/synthetic_tree_data.py --status
    python scripts/synthetic_tree_data.py --seed            # wide tree
    python scripts/synthetic_tree_data.py --seed-siblings   # sibling parentage
    python scripts/synthetic_tree_data.py --clean
"""

import argparse
import asyncio
import logging
import sys
from uuid import uuid4

sys.path.insert(0, ".")
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

from sqlalchemy import func, select  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Entity,
    EntityMention,
    EntityRelation,
    InterviewSession,
    Message,
    RawSegment,
)
from app.services import family_tree  # noqa: E402

SYNTH_PREFIX = "synthtree-"

AUNTS_UNCLES = [
    "מרים", "יעקב", "שושנה", "אברהם", "זהבה", "מרדכי",
    "פנינה", "שמעון", "אסתר", "בנימין", "לאה", "נחום",
]
NIECES_NEPHEWS = [
    "איתי", "נועה", "יובל", "שירה", "אורי",
    "מאיה", "איתן", "טליה", "עומר", "אביגיל",
    "רועי", "הילה", "אלון", "דניאלה", "יונתן",
]

# Tables that must be identical before and after. If any of these move, the
# script did something it promised not to.
UNTOUCHED = {
    "raw_segments": RawSegment,
    "entity_mentions": EntityMention,
    "interview_sessions": InterviewSession,
    "messages": Message,
}


def synth_id() -> str:
    return f"{SYNTH_PREFIX}{uuid4().hex[:12]}"


async def census(db) -> dict:
    out = {}
    for name, model in UNTOUCHED.items():
        out[name] = (await db.execute(select(func.count()).select_from(model))).scalar()
    return out


async def pick_producer(db) -> str:
    return (
        await db.execute(
            select(Entity.producer_id)
            .where(Entity.type == "person")
            .group_by(Entity.producer_id)
            .order_by(func.count().desc())
        )
    ).scalars().first()


async def counts(db, producer_id) -> tuple[int, int]:
    people = (
        await db.execute(
            select(func.count())
            .select_from(Entity)
            .where(Entity.producer_id == producer_id, Entity.id.startswith(SYNTH_PREFIX))
        )
    ).scalar()
    relations = (
        await db.execute(
            select(func.count())
            .select_from(EntityRelation)
            .where(EntityRelation.id.startswith(SYNTH_PREFIX))
        )
    ).scalar()
    return people, relations


async def status(db, producer_id):
    people, relations = await counts(db, producer_id)
    print(f"synthetic entities : {people}")
    print(f"synthetic relations: {relations}")
    for name, n in (await census(db)).items():
        print(f"  {name:<20} {n}")


async def seed(db, producer_id):
    existing_people, existing_relations = await counts(db, producer_id)
    if existing_people or existing_relations:
        print(f"Already seeded ({existing_people} entities, {existing_relations} "
              f"relations). Run --clean first.")
        return

    before = await census(db)

    tree = await family_tree.build_tree(db, producer_id)
    parents = [p for row in tree["generations"] if row["generation"] == -1
               for p in row["people"]]
    siblings = [p for row in tree["generations"] if row["generation"] == 0
                for p in row["people"] if not p["is_self"]]
    if len(parents) < 2 or len(siblings) < 3:
        print(f"Need >=2 parents and >=3 siblings; found {len(parents)} and "
              f"{len(siblings)}. Nothing written.")
        return

    # The relation FK needs a real recording to point at; we create none.
    segment_id = (
        await db.execute(
            select(RawSegment.id)
            .join(InterviewSession, InterviewSession.id == RawSegment.interview_session_id)
            .where(InterviewSession.user_id == producer_id)
            .order_by(RawSegment.created_at)
        )
    ).scalars().first()
    if segment_id is None:
        print("No existing recording to reference. Nothing written.")
        return

    taken = set(
        (
            await db.execute(
                select(Entity.normalized_name).where(Entity.producer_id == producer_id)
            )
        ).scalars().all()
    )
    wanted = AUNTS_UNCLES + NIECES_NEPHEWS
    clashes = [n for n in wanted if n.lower() in taken]
    if clashes:
        # entities is unique on (producer_id, normalized_name); a clash would
        # either fail or silently merge with a real person.
        print(f"Name clash with real entities: {clashes}. Nothing written.")
        return

    def add_person(name: str) -> str:
        eid = synth_id()
        db.add(Entity(
            id=eid, producer_id=producer_id, name=name,
            normalized_name=name.lower(), type="person", is_self=False,
        ))
        return eid

    def relate(from_id: str, rel: str, to_id: str):
        db.add(EntityRelation(
            id=synth_id(), from_entity_id=from_id, to_entity_id=to_id,
            relation_type=rel, source_segment_id=segment_id,
        ))

    # 12 aunts/uncles — siblings of the two parents, six each.
    for i, name in enumerate(AUNTS_UNCLES):
        parent = parents[i % len(parents)]
        relate(add_person(name), "sibling", parent["id"])

    # 5 children each for three of the siblings.
    for i, name in enumerate(NIECES_NEPHEWS):
        sibling = siblings[i // 5]
        relate(add_person(name), "child", sibling["id"])

    await db.commit()

    after = await census(db)
    people, relations = await counts(db, producer_id)
    print(f"seeded: {people} entities, {relations} relations")
    print(f"  aunts/uncles -> {[p['name'] for p in parents]}")
    print(f"  children     -> {[s['name'] for s in siblings[:3]]}")
    print(f"  relations reference existing segment {segment_id}")
    report_census(before, after)


async def seed_siblings(db, producer_id):
    """Record parents FOR THE SIBLINGS, to show what the tree already does.

    Three cases in one picture, all of them existing behaviour:
      * two siblings given the producer's own two parents — they should join
        the producer's trunk, three drops off one bus;
      * one given a real parent plus a parent nobody else has (a half-sibling)
        — that new parent should appear in the parents' row, reached only
        through the sibling;
      * one left alone — still in the row, still with no line, because nothing
        records whose child they are.
    """
    people, relations = await counts(db, producer_id)
    if people or relations:
        print(f"Already seeded ({people} entities, {relations} relations). "
              f"Run --clean first.")
        return

    before = await census(db)
    tree = await family_tree.build_tree(db, producer_id)
    parents = [p for row in tree["generations"] if row["generation"] == -1
               for p in row["people"]]
    siblings = [p for row in tree["generations"] if row["generation"] == 0
                for p in row["people"] if not p["is_self"]]
    if len(parents) < 2 or len(siblings) < 3:
        print(f"Need >=2 parents and >=3 siblings; found {len(parents)} and "
              f"{len(siblings)}. Nothing written.")
        return

    segment_id = (
        await db.execute(
            select(RawSegment.id)
            .join(InterviewSession, InterviewSession.id == RawSegment.interview_session_id)
            .where(InterviewSession.user_id == producer_id)
            .order_by(RawSegment.created_at)
        )
    ).scalars().first()

    other_parent_name = "רבקה"
    taken = set((await db.execute(
        select(Entity.normalized_name).where(Entity.producer_id == producer_id)
    )).scalars().all())
    if other_parent_name.lower() in taken:
        print(f"'{other_parent_name}' already exists. Nothing written.")
        return

    other_id = synth_id()
    db.add(Entity(
        id=other_id, producer_id=producer_id, name=other_parent_name,
        normalized_name=other_parent_name.lower(), type="person", is_self=False,
    ))

    def relate(from_id, rel, to_id):
        db.add(EntityRelation(
            id=synth_id(), from_entity_id=from_id, to_entity_id=to_id,
            relation_type=rel, source_segment_id=segment_id,
        ))

    # "from is the PARENT of to"
    shared = siblings[:2]
    for sib in shared:
        for parent in parents[:2]:
            relate(parent["id"], "parent", sib["id"])
    half = siblings[2]
    relate(parents[1]["id"], "parent", half["id"])
    relate(other_id, "parent", half["id"])
    untouched = [s["name"] for s in siblings[3:]]

    await db.commit()
    people, relations = await counts(db, producer_id)
    print(f"seeded: {people} entity, {relations} relations")
    print(f"  shared parents  : {[s['name'] for s in shared]} "
          f"<- {[p['name'] for p in parents[:2]]}")
    print(f"  half-sibling    : {half['name']} <- "
          f"{parents[1]['name']} + {other_parent_name} (new entity)")
    print(f"  left unrecorded : {untouched or 'none'}")
    report_census(before, await census(db))


async def clean(db, producer_id):
    before = await census(db)

    relations = list((await db.execute(
        select(EntityRelation).where(EntityRelation.id.startswith(SYNTH_PREFIX))
    )).scalars().all())
    people = list((await db.execute(
        select(Entity).where(Entity.id.startswith(SYNTH_PREFIX))
    )).scalars().all())

    # Relations first: they are FK children of entities, and deleting the
    # parent row would rely on the cascade doing it quietly.
    for row in relations:
        await db.delete(row)
    for row in people:
        await db.delete(row)
    await db.commit()

    remaining_people, remaining_relations = await counts(db, producer_id)
    print(f"deleted: {len(people)} entities, {len(relations)} relations")
    print(f"remaining synthetic: {remaining_people} entities, "
          f"{remaining_relations} relations")
    report_census(before, await census(db))


def report_census(before: dict, after: dict):
    print("\ntables that must not change:")
    for name in UNTOUCHED:
        mark = "ok" if before[name] == after[name] else "CHANGED"
        print(f"  {name:<20} {before[name]} -> {after[name]}  {mark}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--seed-siblings", action="store_true")
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        producer_id = await pick_producer(db)
        print(f"producer: {producer_id}\n")
        if args.seed:
            await seed(db, producer_id)
        elif args.seed_siblings:
            await seed_siblings(db, producer_id)
        elif args.clean:
            await clean(db, producer_id)
        else:
            await status(db, producer_id)


asyncio.run(main())
