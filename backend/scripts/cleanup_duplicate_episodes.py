"""One-off: remove duplicate Graphiti episodes.

Re-recording a question used to ADD an episode rather than replace it
(add_episode always mints a fresh uuid), so a segment could end up with
several episodes and have its transcript counted more than once by entity
extraction. add_episode now removes existing episodes first, but graphs
created before that fix still carry the duplicates.

Keeps the NEWEST episode per segment and removes the rest, via the same
remove_episode path used everywhere else — so an entity another recording
still references is never touched.

Prints the entity map before and after so any distortion is visible.

Usage:
    python scripts/cleanup_duplicate_episodes.py            # dry run
    python scripts/cleanup_duplicate_episodes.py --apply    # actually delete
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.graph_memory import get_graphiti  # noqa: E402

GROUP_ID = "79820a49-b07d-41fe-941b-f5ceba09f7b5"
APPLY = "--apply" in sys.argv


async def _entity_map(graphiti, group_id: str) -> dict[str, int]:
    """entity name -> how many episodes mention it."""
    result = await graphiti.driver.execute_query(
        """
        MATCH (e:Episodic)-[:MENTIONS]->(n:Entity)
        WHERE e.group_id = $gid
        RETURN n.name AS name, count(DISTINCT e) AS episodes
        ORDER BY n.name
        """,
        gid=group_id,
        routing_="r",
    )
    return {r["name"]: r["episodes"] for r in result.records}


async def _episodes(graphiti, group_id: str):
    result = await graphiti.driver.execute_query(
        """
        MATCH (e:Episodic) WHERE e.group_id = $gid
        RETURN e.uuid AS uuid, e.name AS name, e.created_at AS created_at
        ORDER BY e.name, e.created_at
        """,
        gid=group_id,
        routing_="r",
    )
    return result.records


async def main() -> None:
    graphiti = get_graphiti()

    before_map = await _entity_map(graphiti, GROUP_ID)
    eps = await _episodes(graphiti, GROUP_ID)

    by_name: dict[str, list] = defaultdict(list)
    for r in eps:
        by_name[r["name"]].append(r)

    dupes = {name: rows for name, rows in by_name.items() if len(rows) > 1}

    print("=" * 78)
    print(f"BEFORE: {len(eps)} episodes across {len(by_name)} segments, "
          f"{len(before_map)} entities")
    print("=" * 78)
    if not dupes:
        print("  No duplicates found — nothing to do.")
        return
    for name, rows in dupes.items():
        print(f"  {name}  has {len(rows)} episodes:")
        for r in rows:
            print(f"     uuid={r['uuid'][:8]}…  created_at={r['created_at']}")

    # Keep the newest; remove the rest.
    to_remove = []
    for name, rows in dupes.items():
        ordered = sorted(rows, key=lambda r: r["created_at"] or "")
        to_remove.extend(ordered[:-1])
    print()
    print(f"  Would remove {len(to_remove)} older episode(s), keeping the newest of each.")

    if not APPLY:
        print()
        print("  DRY RUN — re-run with --apply to delete.")
        return

    print()
    print("=" * 78)
    print("REMOVING")
    print("=" * 78)
    for r in to_remove:
        try:
            await graphiti.remove_episode(r["uuid"])
            print(f"  removed {r['name']} uuid={r['uuid'][:8]}…")
        except Exception as e:
            print(f"  FAILED {r['uuid'][:8]}…: {e}")

    after_map = await _entity_map(graphiti, GROUP_ID)
    eps_after = await _episodes(graphiti, GROUP_ID)

    print()
    print("=" * 78)
    print(f"AFTER: {len(eps_after)} episodes, {len(after_map)} entities")
    print("=" * 78)
    lost = sorted(set(before_map) - set(after_map))
    gained = sorted(set(after_map) - set(before_map))
    changed = {
        k: (before_map[k], after_map[k])
        for k in set(before_map) & set(after_map)
        if before_map[k] != after_map[k]
    }
    print(f"  entities removed : {lost or 'none'}")
    print(f"  entities added   : {gained or 'none'}")
    if changed:
        print("  mention-count changes (this is the double-counting being corrected):")
        for k, (b, a) in sorted(changed.items()):
            print(f"     {k}: {b} -> {a}")
    else:
        print("  mention-count changes: none")

    remaining = {n: rows for n, rows in
                 __import__("collections").defaultdict(list).items()}
    by_name_after: dict[str, int] = defaultdict(int)
    for r in eps_after:
        by_name_after[r["name"]] += 1
    still_dupe = {n: c for n, c in by_name_after.items() if c > 1}
    print(f"  duplicates remaining: {still_dupe or 'none'}")


asyncio.run(main())
