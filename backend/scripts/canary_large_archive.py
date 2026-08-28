"""Large-archive canary (core-size recalibration round, 2026-08-29).

YOSI's family question — the live case that produced a 188-unit "core".
PASS: core stays conversation-sized (<= BOUND units) in every run AND the
offer engages in most runs (the material must remain reachable).

    python scripts/canary_large_archive.py [--runs 5]
"""
import argparse, asyncio, os, sys, uuid
os.environ.setdefault("DEBUG", "false")
sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
settings.GEMINI_CONTEXT_CACHE = "off"  # measurement isolation

from sqlalchemy import text
from app.database import AsyncSessionLocal
from app.services import full_archive_retrieval as ar

BOUND = 60  # "one conversational turn" ceiling; 188 was the failure


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()
    async with AsyncSessionLocal() as db:
        uid = (await db.execute(text("SELECT id FROM users WHERE username ILIKE '%yosi%'"))).scalar()
    cores, fus = [], []
    for n in range(args.runs):
        sel = await ar.select_units("ספר לי על המשפחה שלך", uid, "he", str(uuid.uuid4()))
        if sel.read_failed:
            print(f"  run {n+1}: OUTAGE (not recorded)")
            return 2
        fu = sel.follow_up or {}
        cores.append(len(sel.selected_units))
        fus.append(len(fu.get("unit_ids", [])))
        print(f"  run {n+1}: core={cores[-1]} units, offer={fus[-1]} units", flush=True)
    ok = all(c <= BOUND for c in cores) and sum(1 for f in fus if f) >= args.runs - 1
    print(f"cores={cores} offers={fus}")
    print("CANARY", "PASS" if ok else "FAIL",
          f"(bound {BOUND}, offer in {sum(1 for f in fus if f)}/{args.runs})")
    return 0 if ok else 1


raise SystemExit(asyncio.run(main()))
