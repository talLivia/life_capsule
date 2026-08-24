"""Synthetic large-archive generator (PREFILTER_PLAN gate §3.2, 2026-08-25).

A wholly fictional producer — דוד ברקוביץ', born 1951 in Beer Sheva — whose
archive comfortably exceeds the pre-filter budget, constructed to carry
every edge case the project has identified:

  * a same-named pair (שמעון the army commander vs שמעון the cousin), with
    real Entity/EntityMention rows so the disambiguation machinery fires;
  * a broad-career core with a beekeeping digression (core/offer split);
  * a roots side-branch (the grandfather's journey from Poland) for
    follow-up-offer material;
  * far more recordings than fit one filtered read;
  * a genuinely absent topic (sports — never mentioned anywhere);
  * two recordings deliberately left WITHOUT embeddings (fail-soft
    force-include path);
  * enough per-person material for shown-state / exhaustion scenarios.

REPEATABLE: content is LLM-generated once into
scripts/synthetic_archive_content.json (committed); `--regen` rewrites it,
otherwise the fixture is reused byte-for-byte. `--build` (re)creates the
producer + rows in the DB from the fixture; `--delete` removes the
producer entirely (cascade). The synthetic producer NEVER mixes with the
real archive — everything hangs off its own user row.

    python scripts/generate_synthetic_archive.py --regen   # LLM content
    python scripts/generate_synthetic_archive.py --build   # DB rows
    python scripts/generate_synthetic_archive.py --delete
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("DEBUG", "false")
sys.stdout.reconfigure(encoding="utf-8")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

settings.GEMINI_CONTEXT_CACHE = "off"  # generation must not touch caches

from app.database import AsyncSessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Entity, EntityMention, InterviewSession, RawSegment, TranscriptChunk, User,
)
from app.services import embeddings, entity_store  # noqa: E402
from app.services.llm import llm_service  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

FIXTURE = Path(__file__).parent / "synthetic_archive_content.json"
EMAIL = "synthetic-archive@test.local"

PERSONA = (
    "אתה דוד ברקוביץ', יליד 1951 בבאר שבע, אגרונום בדימוס. אשתך מרים, "
    "הבנים יואב ותמר, אח אלי ואחות רינה. סבא שלך, זאב, עלה מפולין. "
    "בצבא שירתת בתותחנים תחת המפקד שמעון אדלר; יש לך גם בן דוד בשם "
    "שמעון ברקוביץ'. עבדת שלושים שנה במכון וולקני, ותחביב הדבורים ליווה "
    "אותך לצד הקריירה. אסור להזכיר ספורט בשום הקלטה."
)

#: (question_id, brief for the LLM, ~words, embed?)
SPEC = [
    ("childhood_q01", "הילדות המוקדמת בבאר שבע, הבית והשכונה", 950, True),
    ("childhood_q02", "הזיכרון הראשון — יום החול הגדול בשכונה", 900, True),
    ("childhood_q03", "אבא שלך, עבודתו בסולל בונה, דמותו", 1000, True),
    ("childhood_q04", "אמא שלך, הבישולים והבית", 950, True),
    ("childhood_q05", "האח אלי — משחקים ותעלולים משותפים", 900, True),
    ("childhood_q06", "האחות רינה והיחסים איתה", 900, True),
    ("childhood_q07", "בית הספר היסודי, מורה אהוב", 950, True),
    ("childhood_q08", "חגים בבית ההורים", 900, True),
    ("youth_q01", "התיכון והמגמה הריאלית", 950, True),
    ("youth_q02", "תנועת הנוער והטיולים", 950, True),
    ("youth_q03", "החברים הקרובים מהשכונה", 900, True),
    ("youth_q04", "עבודת הקיץ הראשונה", 850, True),
    ("army_q01", "הגיוס לתותחנים והטירונות", 1000, True),
    ("army_q02", "המפקד שמעון אדלר — דמותו והשפעתו עליך", 950, True),
    ("army_q03", "תרגיל גדול בסיני בפיקוד שמעון אדלר", 950, True),
    ("army_q04", "מלחמת יום כיפור — הקרבות בדרום", 1050, True),
    ("army_q05", "החברים מהסוללה ומה עלה בגורלם", 950, True),
    ("army_q06", "השחרור והמעבר לאזרחות", 850, True),
    ("studies_q01", "הפקולטה לחקלאות ברחובות — ההתחלה", 950, True),
    ("studies_q02", "המרצה שהשפיע עליך ביותר", 900, True),
    ("studies_q03", "עבודת הגמר על גידולי שלחין בנגב", 950, True),
    ("studies_q04", "החיים כסטודנט, המעונות והחברים", 900, True),
    ("career_q01", "ההתקבלות למכון וולקני והשנים הראשונות", 1000, True),
    ("career_q02", "מסלול הקריירה כולו — שלושים שנה במכון, התחנות המרכזיות; ובתוך זה סטייה צדדית: איך התגלגלת לגידול דבורים כתחביב", 1150, True),
    ("career_q03", "פרויקט ההשקיה בערבה — ההצלחה הגדולה", 1000, True),
    ("career_q04", "כישלון מקצועי שלמדת ממנו", 950, True),
    ("career_q05", "הדבורים — הכוורות בחצר, דבש ראשון", 950, True),
    ("career_q06", "עמיתים לדרך במכון", 900, True),
    ("career_q07", "הפרישה ומה שאחריה", 900, True),
    ("career_q08", "ההרצאות בפני חקלאים צעירים", 850, True),
    ("family_q01", "איך הכרת את מרים בחתונה של חברים", 1000, True),
    ("family_q02", "החתונה שלכם ב-1976", 950, True),
    ("family_q03", "לידת יואב והשנים הראשונות כהורים", 950, True),
    ("family_q04", "תמר — הילדות שלה והקשר ביניכם", 950, True),
    ("family_q05", "המשפחה המורחבת — הדודים והבני דודים", 950, True),
    ("family_q06", "הנכדים — מה אתה רוצה שידעו", 900, True),
    ("family_q07", "בן הדוד שמעון ברקוביץ' — הקשר המיוחד ביניכם", 950, True),
    ("family_q08", "הקיץ שבילית עם בן הדוד שמעון בחיפה ב-1963", 900, True),
    ("roots_q01", "סבא זאב — המסע מפולין ארצה ב-1934", 1050, True),
    ("roots_q02", "סבתא לאה ובית סבא בחדרה", 950, True),
    ("roots_q03", "מה נשאר מהמשפחה בפולין — הסיפורים ששמעת", 950, True),
    ("travels_q01", "הטיול הגדול לדרום אמריקה אחרי הפרישה", 1000, True),
    ("travels_q02", "המסע לפולין בעקבות סבא זאב", 1000, True),
    ("travels_q03", "טיולי הג'יפים בנגב", 900, False),  # no embedding (edge)
    ("travels_q04", "החופשות השנתיות בכנרת עם הילדים", 900, True),
    ("community_q01", "ההתנדבות במועצה החקלאית", 900, True),
    ("community_q02", "חוג הדבוראים שהקמת בעיר", 850, False),  # no embedding
    ("community_q03", "בית הכנסת והקהילה", 850, True),
    ("community_q04", "השכנים ברחוב לאורך השנים", 850, True),
    ("later_q01", "החיים היום — שגרת בוקר וגינה", 900, True),
    ("later_q02", "מה למדת על זוגיות אחרי חמישים שנה עם מרים", 950, True),
    ("later_q03", "ההספד שנשאת לאח אלי", 900, True),
    ("later_q04", "מה היית אומר לעצמך בן העשרים", 900, True),
    ("later_q05", "הדברים שעוד חשוב לך להספיק", 850, True),
]

QUESTION_TEXT = {qid: f"ספר לי על {brief.split(' — ')[0].split(',')[0]}" for qid, brief, _, _ in SPEC}


async def regen() -> None:
    content = {}
    if FIXTURE.exists():
        content = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for i, (qid, brief, words, _) in enumerate(SPEC):
        if qid in content and len(content[qid]) > 1500:
            continue
        prompt = (
            f"{PERSONA}\n\nכתוב את תשובתך המדוברת לשאלת הראיון: \"{brief}\". "
            f"דבר בגוף ראשון, בשפה מדוברת וטבעית, כ-{words} מילים, בפסקאות "
            "רציפות ללא כותרות. ספר סיפור חי עם פרטים, שמות ותאריכים עקביים."
        )
        for attempt in range(3):
            try:
                text = await llm_service.generate_response(
                    messages=[{"role": "user", "content": prompt}],
                    system_prompt="אתה מספר סיפור חיים בגוף ראשון. פלט: טקסט רציף בלבד.",
                    temperature=0.9,
                )
                if len(text) > 1500:
                    content[qid] = text.strip()
                    break
            except Exception as e:
                print(f"  {qid}: attempt {attempt+1} failed ({e})")
                await asyncio.sleep(20)
        else:
            raise RuntimeError(f"could not generate {qid}")
        FIXTURE.write_text(json.dumps(content, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  [{i+1}/{len(SPEC)}] {qid}: {len(content[qid])} chars")
    total = sum(len(v) for v in content.values())
    print(f"fixture complete: {len(content)} recordings, {total} chars")


def _word_timestamps(text: str) -> list:
    """Synthetic Whisper-shaped timings: constant small gaps, a long pause
    every third sentence end — the 90th-percentile splitter then yields
    natural multi-sentence units."""
    out, t, sent_ends = [], 0.5, 0
    for w in text.split():
        dur = 0.30 + 0.015 * len(w)
        out.append({"word": w, "start_sec": round(t, 2), "end_sec": round(t + dur, 2)})
        t += dur
        if w and w[-1] in ".!?":
            sent_ends += 1
            t += 1.5 if sent_ends % 3 == 0 else 0.08
        else:
            t += 0.08
    return out


async def delete_producer() -> None:
    async with AsyncSessionLocal() as db:
        u = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none()
        if u is None:
            print("no synthetic producer to delete")
            return
        await db.execute(delete(User).where(User.id == u.id))
        await db.commit()
        print(f"deleted synthetic producer {u.id} (cascade)")


async def build() -> None:
    content = json.loads(FIXTURE.read_text(encoding="utf-8"))
    await delete_producer()
    async with AsyncSessionLocal() as db:
        user = User(
            id=str(uuid.uuid4()), email=EMAIL, username="synthetic_archive",
            hashed_password="!synthetic-not-loginable!", full_name="דוד ברקוביץ' (סינתטי)",
        )
        db.add(user)
        session = InterviewSession(id=str(uuid.uuid4()), user_id=user.id, status="completed")
        db.add(session)
        await db.commit()

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        seg_ids = {}
        for i, (qid, brief, _, embed) in enumerate(SPEC):
            text = content[qid]
            wts = _word_timestamps(text)
            emb = None
            if embed:
                try:
                    emb = await embeddings.embed_text(text[:6000])
                except Exception as e:
                    print(f"  embed failed for {qid} ({e}); leaving None")
            seg = RawSegment(
                id=str(uuid.uuid4()),
                interview_session_id=session.id,
                question_id=qid,
                question_index=i,
                question_asked=QUESTION_TEXT[qid],
                video_key=f"segments/{user.id}/{session.id}/{i}/synthetic.webm",
                transcript=text,
                status="ready",
                recording_no=i + 1,
                embedding=emb,
                created_at=base + timedelta(hours=i),
            )
            db.add(seg)
            db.add(TranscriptChunk(
                id=str(uuid.uuid4()), raw_segment_id=seg.id,
                start_sec=wts[0]["start_sec"], end_sec=wts[-1]["end_sec"],
                text=text, word_timestamps=wts, sequence_index=0,
            ))
            seg_ids[qid] = seg.id
            if (i + 1) % 10 == 0:
                await db.commit()
                print(f"  built {i+1}/{len(SPEC)}")
        user_row = await db.get(User, user.id)
        user_row.recording_seq = len(SPEC)
        await db.commit()

        # The same-name pair + a few anchor entities, with mentions whose
        # summaries feed the disambiguation labels.
        def ent(name, etype="person"):
            return Entity(
                id=str(uuid.uuid4()), producer_id=user.id, name=name,
                normalized_name=entity_store.normalize_entity_name(name), type=etype,
            )
        # The confusable rule is subset-of-name-tokens (like the real
        # archive's אמנון / אמנון נחום) — the cousin goes by the BARE name
        # so a listener saying "שמעון" could mean either.
        shimon_cmdr = ent("שמעון אדלר")
        shimon_cousin = ent("שמעון")
        miriam = ent("מרים")
        zeev = ent("זאב")
        db.add_all([shimon_cmdr, shimon_cousin, miriam, zeev])
        await db.commit()
        mentions = [
            (shimon_cmdr, "army_q02", "שמעון אדלר: המפקד של הדובר בתותחנים"),
            (shimon_cmdr, "army_q03", "שמעון אדלר: המפקד של הדובר בתותחנים"),
            (shimon_cousin, "family_q07", "שמעון ברקוביץ': בן דוד של הדובר"),
            (shimon_cousin, "family_q08", "שמעון ברקוביץ': בן דוד של הדובר"),
            (miriam, "family_q01", "מרים: אשתו של הדובר"),
            (miriam, "family_q02", "מרים: אשתו של הדובר"),
            (miriam, "later_q02", "מרים: אשתו של הדובר"),
            (zeev, "roots_q01", "זאב: סבא של הדובר שעלה מפולין"),
            (zeev, "travels_q02", "זאב: סבא של הדובר שעלה מפולין"),
        ]
        for e, qid, summary in mentions:
            db.add(EntityMention(
                id=str(uuid.uuid4()), entity_id=e.id,
                raw_segment_id=seg_ids[qid], summary=summary,
            ))
        await db.commit()
        total = sum(len(content[qid]) for qid, *_ in SPEC)
        print(f"BUILT synthetic producer {user.id}: {len(SPEC)} recordings, "
              f"{total} raw chars, entities: 2x שמעון + מרים + זאב")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--regen", action="store_true")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--delete", action="store_true")
    args = ap.parse_args()
    if args.regen:
        await regen()
    if args.build:
        await build()
    if args.delete and not args.build:
        await delete_producer()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
