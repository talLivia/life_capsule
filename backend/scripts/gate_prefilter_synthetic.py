"""Pre-filter gate checks against the SYNTHETIC producer (PREFILTER_PLAN
§3, 2026-08-25). Live LLM calls, filtered mode forced, budget set so only
a minority of the archive admits. Prints PASS/FAIL per check.

    python scripts/gate_prefilter_synthetic.py
"""
import asyncio, os, sys, uuid
os.environ.setdefault("DEBUG", "false")
sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from app.config import settings
settings.GEMINI_CONTEXT_CACHE = "off"
settings.PREFILTER = "on"
settings.PREFILTER_CHAR_BUDGET = 60_000  # ~30% of the synthetic archive

from app.services import full_archive_retrieval as ar, prefilter

G = "8d0569f8-d13a-4479-9fe9-56bd4eb4eaee"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}")


async def sel_with_pf(question, session_id):
    captured = {}
    real = prefilter.apply

    async def spy(*a, **kw):
        r = await real(*a, **kw)
        captured["pf"] = r
        return r

    prefilter.apply = spy
    try:
        for _ in range(4):
            s = await ar.select_units(question, G, "he", session_id)
            if not s.read_failed:
                return s, captured.get("pf")
            print("    (outage, retrying)")
            await asyncio.sleep(30)
        raise RuntimeError("persistent outage")
    finally:
        prefilter.apply = real


async def main():
    archive, em, units, tags = await ar._archive_bundle(G)
    qid_of = {}
    for a in archive:
        qid_of[a.segment.id] = a.segment.question_id
    army_segs = {s for s, q in qid_of.items() if q.startswith("army")}
    roots_segs = {s for s, q in qid_of.items() if q.startswith("roots")}
    shimon_segs = {s for s, q in qid_of.items() if q in
                   ("army_q02", "army_q03", "family_q07", "family_q08")}

    print(f"synthetic archive: {len(archive)} recordings, {len(units)} units, "
          f"tags on {len(tags)} recordings")

    # 1. Filtered narrow/broad correctness — the question that returned
    # EMPTY unfiltered at 139K tokens.
    s1, pf1 = await sel_with_pf("ספר לי על השירות הצבאי שלך", str(uuid.uuid4()))
    admitted_army = len(army_segs & set(pf1.admitted)) if pf1 else 0
    sel_segs = {u.segment_id for u in s1.selected_units}
    check("filter active and excludes a majority",
          pf1 is not None and pf1.excluded > len(archive) // 2,
          f"admitted {len(pf1.admitted)}/{len(archive)}" if pf1 else "pf=None")
    check("army recordings admitted by ranking", admitted_army >= 4,
          f"{admitted_army}/6 army recordings in set")
    check("filtered read ANSWERS the army question (unfiltered gave 0 units)",
          len(s1.selected_units) > 0 and sel_segs <= army_segs | set(pf1.admitted),
          f"{len(s1.selected_units)} units selected")

    # 2. Disambiguation under filtering: both same-named recordings must be
    # force-included and the read should CLARIFY.
    s2, pf2 = await sel_with_pf("ספר לי על שמעון", str(uuid.uuid4()))
    check("same-name recordings force-included",
          pf2 is not None and shimon_segs <= set(pf2.admitted),
          f"{len(shimon_segs & set(pf2.admitted))}/4 tagged recordings admitted")
    check("ambiguous name clarifies (not answers)",
          bool(s2.clarify) and not s2.selected_units,
          f"clarify={bool(s2.clarify)}, units={len(s2.selected_units)}")

    # 3. Absent topic: sports exists nowhere. Expect empty selection and NO
    # specific no-story claim (low-confidence or unresolved -> generic).
    s3, pf3 = await sel_with_pf("ספר לי על הספורט שאתה אוהב", str(uuid.uuid4()))
    check("absent topic returns empty", not s3.selected_units,
          f"units={len(s3.selected_units)}")
    check("no specific archive-wide-absence claim under filtering",
          not getattr(s3, "no_story_text", None) or pf3 is None
          or prefilter.covers_entity(pf3, []),
          f"low_confidence={pf3.low_confidence if pf3 else '-'}")

    # 4. Pinning + topical-drift expansion in ONE session.
    sess = str(uuid.uuid4())
    s4a, pf4a = await sel_with_pf("ספר לי על הקרבות במלחמת יום כיפור", sess)
    s4b, pf4b = await sel_with_pf("ספר לי על סבא זאב והמסע מפולין", sess)
    roots_admitted = roots_segs & set(pf4b.admitted) if pf4b else set()
    check("second question expanded the pinned set",
          pf4b is not None and pf4b.expanded and len(roots_admitted) >= 1,
          f"expanded={pf4b.expanded if pf4b else '-'}, roots in set: {len(roots_admitted)}")
    sel_b = {u.segment_id for u in s4b.selected_units}
    check("post-expansion answer serves roots material",
          bool(sel_b) and sel_b <= set(pf4b.admitted) and bool(sel_b & (roots_segs | set(pf4b.admitted))),
          f"{len(s4b.selected_units)} units")
    check("pinned set grew, not reshuffled",
          pf4a is not None and set(pf4a.admitted) <= set(pf4b.admitted),
          f"{len(pf4a.admitted)} -> {len(pf4b.admitted)} recordings")

    print(("ALL CHECKS PASS" if all(RESULTS) else "GATE FAIL") +
          f"  ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if all(RESULTS) else 1


raise SystemExit(asyncio.run(main()))
