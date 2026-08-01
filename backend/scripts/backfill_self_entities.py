"""
Give every producer the self-entity that migration 0012 created only for the
producers existing at the time.

Two populations need this:
  * anyone who registered between migration 0012 and the signup hook landing
    (Phase 1 of docs/FAMILY_TREE_TIMELINE.md) — they have no root at all;
  * anyone whose registration hit the logged-and-swallowed failure path in
    users.register_user, which deliberately does not fail a registration over
    an entity row.

Idempotent and safe to re-run: it calls the SAME `entity_store.ensure_self_entity`
the signup path uses, so there is exactly one implementation of what a
self-entity is. Dry-run by default.

    python scripts/backfill_self_entities.py            # report only
    python scripts/backfill_self_entities.py --apply    # write

A producer is reported as SKIPPED rather than failing the run when their name
key is already held by a non-person entity — see ensure_self_entity for why
that case refuses to guess.
"""

import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models import Entity, User  # noqa: E402
from app.services import entity_store  # noqa: E402


async def main(apply: bool) -> int:
    async with AsyncSessionLocal() as db:
        producers = (
            await db.execute(select(User).where(User.role == "producer"))
        ).scalars().all()

        with_self = set(
            (
                await db.execute(select(Entity.producer_id).where(Entity.is_self))
            ).scalars().all()
        )

        missing = [u for u in producers if u.id not in with_self]

        print(f"producers            : {len(producers)}")
        print(f"already have a root  : {len(producers) - len(missing)}")
        print(f"MISSING a root       : {len(missing)}")
        if not missing:
            print("\nnothing to do.")
            return 0

        created = skipped = 0
        for user in missing:
            entity, was_created = await entity_store.ensure_self_entity(db, user)
            label = (user.full_name or "").strip() or user.username
            if entity is None:
                print(f"  SKIPPED  {user.id}  {label!r} — see the logged reason above")
                skipped += 1
            else:
                print(f"  {'CREATE ' if was_created else 'EXISTS '} {user.id}  {entity.name!r}")
                created += was_created

        if apply:
            await db.commit()
            print(f"\napplied: {created} created, {skipped} skipped")
        else:
            await db.rollback()
            print(f"\nDRY RUN — would create {created}, skip {skipped}. Re-run with --apply")
        return skipped


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main("--apply" in sys.argv)) == 0 else 1)
