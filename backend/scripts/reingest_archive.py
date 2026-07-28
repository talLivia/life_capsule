"""Re-ingest an existing archive with the currently-configured STT provider.

Why: a garbled transcript is permanent damage. This archive lost the entity
"חיל האוויר" because a re-transcription rendered "שירתתי בחיל האוויר" as
"שראתתי בחלה הריון". Switching INGESTION_STT_PROVIDER only affects NEW
recordings; existing ones keep whatever text they were ingested with.

What it does per ready segment:
  1. CLEARS segment.transcript — transcribe_node deliberately short-circuits
     when a transcript already exists, so without this the whole run would be
     a silent no-op reusing the bad text.
  2. re-transcribes from the stored video, rebuilds chunks (that node deletes
     existing ones first), re-embeds, re-tags topics, re-scores importance
  3. re-runs finalize_ingest, which writes entities and mentions to Postgres
     via entity_store, REPLACING the segment's previous mentions rather than
     appending to them

CAUTION until the entity import (chunk 2 of the Postgres migration) has run:
finalize_ingest no longer writes to Graphiti, so re-ingesting a segment does
NOT refresh its graph episode. The graph still holds the only copy of the
existing entity summaries — this script will not destroy them (nothing removes
episodes any more), but the two stores will disagree for any segment it
touches. Import first, re-ingest after.

Calls the graph nodes directly rather than re-invoking the LangGraph thread:
a completed thread would resume oddly, and human_confirm would interrupt a
batch run waiting for input. Segments that WOULD have asked a disambiguation
question are reported at the end so they can be confirmed manually.

Safety: the first segment is verified to have actually changed before the rest
are touched, so a broken provider config can't quietly rewrite the archive
with identical (or empty) text.

Usage:
    python scripts/reingest_archive.py           # dry run: snapshot only
    python scripts/reingest_archive.py --apply
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.disable(logging.INFO)  # SQL echo would bury the report

from sqlalchemy import select  # noqa: E402

from app import analysis_graph as ag  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.models import InterviewSession, RawSegment, TranscriptChunk  # noqa: E402
from app.services import full_archive_retrieval as ar, graph_memory  # noqa: E402

GROUP_ID = "79820a49-b07d-41fe-941b-f5ceba09f7b5"
APPLY = "--apply" in sys.argv


async def _entity_counts() -> dict[str, int]:
    g = graph_memory.get_graphiti()
    res = await g.driver.execute_query(
        """
        MATCH (e:Episodic)-[:MENTIONS]->(n:Entity)
        WHERE e.group_id = $gid
        RETURN n.name AS name, count(DISTINCT e) AS episodes
        """,
        gid=GROUP_ID,
        routing_="r",
    )
    return {r["name"]: r["episodes"] for r in res.records}


async def _snapshot() -> dict:
    """Transcripts, chunk counts and UNIT boundaries as they stand now."""
    ar.invalidate_archive_cache(GROUP_ID)
    archive, entity_map, units = await ar._archive_bundle(GROUP_ID)
    by_seg: dict[str, list] = {}
    for u in units:
        by_seg.setdefault(u.segment_id, []).append(u)

    async with AsyncSessionLocal() as db:
        segs = (await db.execute(
            select(RawSegment)
            .join(InterviewSession, RawSegment.interview_session_id == InterviewSession.id)
            .where(InterviewSession.user_id == GROUP_ID, RawSegment.status == "ready")
            .order_by(RawSegment.question_index)
        )).scalars().all()
        chunk_counts = {}
        for s in segs:
            n = (await db.execute(
                select(TranscriptChunk).where(TranscriptChunk.raw_segment_id == s.id)
            )).scalars().all()
            chunk_counts[s.id] = len(n)

    return {
        "segments": [
            {
                "id": s.id,
                "q": (s.question_asked or "")[:44],
                "transcript": s.transcript or "",
                "chunks": chunk_counts.get(s.id, 0),
                "units": [
                    (u.unit_id, round(u.start_sec, 2), round(u.end_sec, 2), u.text)
                    for u in by_seg.get(s.id, [])
                ],
            }
            for s in segs
        ],
        "entities": await _entity_counts(),
    }


def _print_snapshot(label: str, snap: dict) -> None:
    print("=" * 78)
    print(f"{label}: {len(snap['segments'])} ready segments, "
          f"{len(snap['entities'])} entities")
    print("=" * 78)
    for s in snap["segments"]:
        print(f"  {s['id'][:8]} ({s['q']})  chunks={s['chunks']} units={len(s['units'])}")
        print(f"     {s['transcript'][:100]}")
    print(f"  entities: {sorted(snap['entities'])}")


async def _reingest_one(segment_id: str) -> dict:
    """Run the ingestion nodes directly. Returns the accumulated state."""
    # 1) Clear the transcript so transcribe_node actually re-runs.
    async with AsyncSessionLocal() as db:
        seg = (await db.execute(
            select(RawSegment).where(RawSegment.id == segment_id)
        )).scalar_one_or_none()
        if seg is None:
            return {"error": "segment not found"}
        old_transcript = seg.transcript or ""
        seg.transcript = None
        await db.commit()

    state: dict = {"segment_id": segment_id, "group_id": GROUP_ID}
    state.update(await ag.transcribe_node(state))
    if state.get("error"):
        return {**state, "old_transcript": old_transcript}

    state.update(await ag.create_transcript_chunks_node(state) or {})
    state.update(await ag.embed_transcript_node(state) or {})
    state.update(await ag.extract_topics_node(state) or {})

    # check_entities returns auto-resolutions plus anything a human would be
    # asked about. We do NOT pause; the pending names are reported instead.
    ent = await ag.check_entities_node(state) or {}
    state.update(ent)

    state.update(await ag.score_importance_node(state) or {})
    state.update(await ag.finalize_ingest_node(state) or {})
    state["old_transcript"] = old_transcript
    return state


async def main() -> None:
    print(f"INGESTION_STT_PROVIDER = {settings.INGESTION_STT_PROVIDER}")
    print(f"DEEPGRAM_MODEL         = {settings.DEEPGRAM_MODEL}")
    print()

    before = await _snapshot()
    _print_snapshot("BEFORE", before)

    if not APPLY:
        print()
        print("DRY RUN — re-run with --apply to re-ingest.")
        return

    order = [s["id"] for s in before["segments"]]
    pending_confirmations: list[tuple[str, list]] = []
    failures: list[tuple[str, str]] = []

    print()
    print("=" * 78)
    print("RE-INGESTING")
    print("=" * 78)
    for i, seg_id in enumerate(order, 1):
        state = await _reingest_one(seg_id)
        old = (state.get("old_transcript") or "").strip()
        new = (state.get("transcript") or "").strip()
        err = state.get("error")
        changed = new and new != old
        print(f"  [{i}/{len(order)}] {seg_id[:8]}  "
              f"{'CHANGED' if changed else ('SAME' if new else 'NO TRANSCRIPT')}"
              f"{f'  ERROR={err}' if err else ''}")
        if new and changed:
            print(f"        old: {old[:90]}")
            print(f"        new: {new[:90]}")
        if err:
            failures.append((seg_id, str(err)))
        names = state.get("names_to_check") or []
        if names:
            pending_confirmations.append((seg_id, names))

        # SAFETY GATE: prove the first segment really re-transcribed before
        # rewriting the rest. A misconfigured provider would otherwise quietly
        # overwrite the whole archive with identical or empty text.
        if i == 1:
            if err or not new:
                print()
                print("  ABORTING — first segment produced no transcript. "
                      "Nothing else touched.")
                return
            if not changed:
                print()
                print("  ABORTING — first segment's transcript is UNCHANGED, so the "
                      "provider switch had no effect. Nothing else touched.")
                return
            print("        ^ first-segment check passed; continuing")

    after = await _snapshot()
    print()
    _print_snapshot("AFTER", after)

    # ── comparison ──
    print()
    print("=" * 78)
    print("UNIT BOUNDARIES: before -> after")
    print("=" * 78)
    b_by = {s["id"]: s for s in before["segments"]}
    for s in after["segments"]:
        b = b_by.get(s["id"], {"units": [], "chunks": 0})
        print(f"  {s['id'][:8]} ({s['q']})")
        print(f"     chunks {b['chunks']} -> {s['chunks']} | "
              f"units {len(b['units'])} -> {len(s['units'])}")
        if [u[1:3] for u in b["units"]] != [u[1:3] for u in s["units"]]:
            print(f"       OLD: {[(u[1], u[2]) for u in b['units']]}")
            print(f"       NEW: {[(u[1], u[2]) for u in s['units']]}")

    print()
    print("=" * 78)
    print("ENTITIES: before -> after")
    print("=" * 78)
    lost = sorted(set(before["entities"]) - set(after["entities"]))
    gained = sorted(set(after["entities"]) - set(before["entities"]))
    print(f"  count : {len(before['entities'])} -> {len(after['entities'])}")
    print(f"  gained: {gained or 'none'}")
    print(f"  lost  : {lost or 'none'}")
    print(f"  חיל האוויר present after: {'חיל האוויר' in after['entities']}")

    print()
    print("=" * 78)
    print("DISAMBIGUATION that would have been asked (confirm manually)")
    print("=" * 78)
    if pending_confirmations:
        for seg_id, names in pending_confirmations:
            print(f"  {seg_id[:8]}: {names}")
    else:
        print("  none — no segment needed a human identity decision")

    if failures:
        print()
        print("FAILURES:")
        for seg_id, err in failures:
            print(f"  {seg_id[:8]}: {err}")


asyncio.run(main())
