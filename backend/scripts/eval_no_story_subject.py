"""
Does the tailored no-story line name the right subject, and stay quiet
when there isn't one?

THE RISK IS ASYMMETRIC, so this measures the negative case harder than the
positive one. "I don't have another story about ××ž× ×•×Ÿ" is a small win when
right. "I don't have another story about ×ª×œ ××‘×™×‘" in answer to "what pets did
you have?" is a confident wrong claim about the archive, and it would read as
the system inventing a subject. The generic line is a perfectly good answer
for a question about nobody in particular.

So the panel is the four questions that currently return no-story, three of
which have NO subject at all and must come back with the generic text
untouched, plus the follow-up case this was built for.

Hard-fails on an exhausted retry: the archive read is fail-soft, so an outage
returns an empty selection AND no subject â€” which is indistinguishable from
"correctly declined to name one". Both of the broken measurements taken while
building the same-name feature were exactly this.

Usage: python scripts/eval_no_story_subject.py [--runs 4]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

os.environ.setdefault("DEBUG", "false")
sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import eval_common as crm  # noqa: E402
from app.services import full_archive_retrieval as ar  # noqa: E402
from app.services import retrieval_service  # noqa: E402
from app.services.llm import llm_service  # noqa: E402
from app.services.response_assembler import NO_STORY_FALLBACK  # noqa: E402


async def _seed_session_with_shown(group_id: str, segment_prefixes: List[str]) -> str:
    """A session that has ALREADY played this person's recordings.

    THE CASE ONLY EXISTS ONCE THE MATERIAL IS SPENT. "What else did you do
    together?" is only a no-story question when there is nothing left â€” and
    "already shown" lives in the assistant Message's metadata, keyed by
    session, not in the conversation TEXT. Patching `_recent_turns` alone (the
    obvious way to inject history) leaves the shown-unit record empty, so the
    archive still has unplayed material and correctly plays it. That is what
    made the first version of this case report `<played a clip>`.
    """
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models import Avatar, Message, Session

    archive, _emap, units, _tags = await ar._archive_bundle(group_id)
    shown = [
        {"key": ar._unit_key(u.segment_id, u.start_sec), "unit_id": u.unit_id, "text": u.text}
        for u in units
        if any(u.segment_id.startswith(p) for p in segment_prefixes)
    ]
    if not shown:
        raise RuntimeError(f"no units found for {segment_prefixes}")

    async with AsyncSessionLocal() as db:
        avatar_id = (
            await db.execute(select(Avatar.id).where(Avatar.user_id == group_id).limit(1))
        ).scalars().first()
        if avatar_id is None:
            raise RuntimeError("producer has no avatar row; cannot seed a session")
        session = Session(user_id=group_id, producer_id=group_id, avatar_id=avatar_id, status="active")
        db.add(session)
        await db.flush()
        db.add(Message(session_id=session.id, role="assistant",
                       content="seeded", message_metadata={"shown_units": shown}))
        await db.commit()
        return session.id


async def _drop_session(session_id: str) -> None:
    from app.database import AsyncSessionLocal
    from app.models import Session
    from sqlalchemy import delete

    async with AsyncSessionLocal() as db:
        await db.execute(delete(Session).where(Session.id == session_id))
        await db.commit()

#: (label, question, history, expectation)
#:   "generic" -> must return the untouched NO_STORY_FALLBACK
#:   "<name>"  -> must name exactly this entity
#:   "answer"  -> not a no-story question at all; must still play something
CASES: List[Tuple[str, str, List[dict], str]] = [
    # THE CASE THIS WAS BUILT FOR. The question names nobody â€” the subject has
    # to be resolved from the previous turn, which is the whole reason a
    # lexical match on the question text could not do this.
    (
        "followup-about-amnon", "×ž×” ×¢×•×“ ×¢×©×™×ª× ×‘×™×—×“?",
        # The assistant turn carries the CLIP'S SPOKEN TEXT, because that is
        # what _persist_message stores for v2 ("spoken_text or video_url").
        # It used to be a bare URL here, which is what v1 stores â€” and with a
        # URL as the only antecedent the subject cannot be resolved, so this
        # case failed for a reason that exists nowhere in production. Second
        # time this fixture has misrepresented a real session; the first was
        # not seeding shown_units at all.
        [{"role": "user", "content": "×¡×¤×¨ ×œ×™ ×¢×œ ××ž× ×•×Ÿ ×”×—×‘×¨ ×©×œ×š ×ž×”×¦×‘×"},
         {"role": "assistant",
          "content": "×”×™×™×ª×™ ×”×•×œ×š ×œ×ž×›×œ×œ×ª ×¢×ž×§ ×”×™×¨×“×Ÿ ×‘×™×—×“ ×¢× ×—×‘×¨ ×©×œ×™ ××ž× ×•×Ÿ"}],
        "××ž× ×•×Ÿ",
    ),  # runs against a session where BOTH of ××ž× ×•×Ÿ's recordings are spent
    # The three existing no-story questions. None has a subject; all three
    # must be left exactly as they are today.
    ("no-answer", "××™×–×” ×—×™×•×ª ×ž×—×ž×“ ×”×™×• ×œ×š?", [], "generic"),
    ("montreal", "×ž×” ×œ×š ×•×œ×¢×™×¨ ×ž×•× ×˜×¨×™××•×œ?", [], "generic"),
    ("influence-1", "×ž×™ ×”×“×ž×•×ª ×”×›×™ ×ž×©×¤×™×¢×” ×‘×™×œ×“×•×ª ×©×œ×š?", [], "generic"),
    (
        "influence-2 (followup)", "×”×•× ×¢×“×™×™×Ÿ ×‘×—×™×™×?",
        [{"role": "user", "content": "×ž×™ ×”×“×ž×•×ª ×”×›×™ ×ž×©×¤×™×¢×” ×‘×™×œ×“×•×ª ×©×œ×š?"},
         {"role": "assistant", "content": "http://localhost:8000/uploads/x.mp4"}],
        "generic",
    ),
    # A control: a question that HAS an answer must be unaffected. If naming a
    # subject started suppressing answers, that would show up here first.
    ("brothers (control)", "×ž×™ ×”××—×™× ×©×œ×š?", [], "answer"),
]


class ExhaustedAPI(RuntimeError):
    pass


def _install_hard_failing_llm(retries: int = 6) -> None:
    real = llm_service.generate_response

    async def wrapper(messages, system_prompt=None, thinking=False, temperature=None, **kw):
        last: Optional[Exception] = None
        for attempt in range(retries):
            try:
                return await real(messages=messages, system_prompt=system_prompt,
                                  thinking=thinking, temperature=temperature, **kw)
            except Exception as e:  # noqa: BLE001
                last = e
                await asyncio.sleep(4 * (attempt + 1))
        raise ExhaustedAPI(f"{retries} attempts failed; last: {last}")

    llm_service.generate_response = wrapper


async def run(question: str, group_id: str, history: List[dict], session_id: Optional[str] = None):
    # PRODUCTION'S WINDOW, not an approximation of it. `_recent_turns` runs
    # AFTER the user's question is persisted and takes the last
    # COREFERENCE_HISTORY_TURNS *message rows* â€” so the real window is the
    # previous assistant reply plus the question being asked, never the
    # previous exchange. Fixtures that got this wrong have now produced one
    # false negative and one false positive in this file alone.
    window = (history + [{"role": "user", "content": question}])[
        -retrieval_service.COREFERENCE_HISTORY_TURNS:
    ]

    async def turns(_s, _n):
        return window

    original = retrieval_service._recent_turns
    retrieval_service._recent_turns = turns
    try:
        result = await ar.assemble_video_clip_response_v2(
            question, group_id, "he", session_id or str(uuid.uuid4())
        )
    finally:
        retrieval_service._recent_turns = original
    if result.read_failed:
        # NOT a result. Checked on the flag rather than on an exception,
        # because _read_archive_for_ranges catches everything â€” the retry
        # wrapper's raise never reached this harness, so the "hard fail" was
        # decorative until ArchiveRead.failed existed.
        raise ExhaustedAPI("the archive read failed; not recording it as an answer")
    return result


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=4)
    parser.add_argument("--group-id", default=crm.DEFAULT_GROUP_ID)
    args = parser.parse_args()

    _install_hard_failing_llm()
    ar.invalidate_archive_cache(args.group_id)

    print(f"{len(CASES)} cases x {args.runs} runs")
    print("=" * 78)
    failures = 0
    try:
        for label, question, history, expect in CASES:
            # The follow-up case needs the person's material already spent.
            seeded = (
                await _seed_session_with_shown(args.group_id, ["037cece4", "7479a3bf"])
                if label == "followup-about-amnon"
                else None
            )
            try:
                rows = [
                    await run(question, args.group_id, history, seeded)
                    for _ in range(args.runs)
                ]
            finally:
                if seeded:
                    await _drop_session(seeded)
            texts = [r.fallback_text if r.no_story else "<played a clip>" for r in rows]
            distinct = sorted(set(texts))

            if expect == "answer":
                ok = all(not r.no_story for r in rows)
            elif expect == "generic":
                ok = all(r.no_story and r.fallback_text == NO_STORY_FALLBACK for r in rows)
            else:
                ok = all(r.no_story and expect in (r.fallback_text or "") for r in rows)

            failures += not ok
            print(f"  {label:24} expect {expect:9} {'PASS' if ok else 'FAIL'}")
            for t in distinct:
                print(f"        {t}")
    except ExhaustedAPI as e:
        print(f"\nABORTED â€” {e}")
        print("Not reported as a result: fail-soft makes an outage look exactly "
              "like 'correctly declined to name a subject'.")
        return 3

    print("\n" + "=" * 78)
    print("PASS" if not failures else f"{failures} case(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
