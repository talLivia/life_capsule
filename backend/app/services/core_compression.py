"""Conversation-sizing an oversized core selection — three-rule redesign
(2026-08-30, producer-approved; supersedes the 2026-08-29 duration-target
version after the live 188-unit/3:52 family answer showed the soft
"roughly N seconds" instruction is not enforced by the model when the
input contains long continuous narratives).

A CODE-gated, isolated post-processing step — the main archive-read prompt
is untouched. Under the duration threshold nothing here runs and the flow
is byte-identical to before. Over it, ONE bounded extra LLM call splits
the ALREADY-SELECTED core under three rules, all ENFORCED IN CODE on
whatever the model returns:

  RULE 1 — category match. Every unit is annotated with its recording's
  interview-question category (from RawSegment.question_id). The model
  names the category that directly matches the asked question,
  constrained to the categories actually present in the input; code then
  keeps in core only units whose recording category matches. Everything
  else routes to the follow-up offer by default.

  RULE 2 — passage expansion is untouched. _expand_about_passages runs
  BEFORE this step (see select_units) exactly as it always has; nothing
  here changes when or how it applies.

  RULE 3 — close-entity linkage, UNIT-scoped not recording-scoped. A unit
  whose own text explicitly names an entity related to the producer by a
  FIRST-DEGREE family edge (parent / sibling / spouse / child in
  entity_relations, either direction to the is_self entity) stays
  core-ELIGIBLE regardless of its recording's category. Deliberately
  first-degree only: grandparent / aunt_uncle / cousin edges do NOT
  qualify, so a grandparent's story cannot ride back into core through
  the entity link (the "great-grandfather safeguard"). Unit-scoping means
  the flag never admits the rest of the recording — only the naming units
  themselves.

The never-invent contract is unchanged and code-enforced: ids in, subset
of those ids out, archive order preserved; the model can narrow the
served answer, never add to it. Units the model kept but the rules
demote go to the FRONT of the offer's unit list, so demoted material is
deferred, never destroyed.

Fail-open everywhere, in the same shapes as before: any error, empty or
unparseable reply, an all-foreign id list, a kept set the rules empty
out, a failed close-family lookup, or a unit with no category annotation
— each degrades toward serving the ORIGINAL selection (or, per unit,
toward eligibility). The one thing this must never do is turn an answer
into silence.

SIZE BUDGET (2026-08-31, code-enforced): the three rules decide what is
ELIGIBLE; CORE_COMPRESSION_TARGET_SEC decides how much PLAYS. After rule
enforcement the kept units are truncated at a unit boundary once the
playable span (intra-run pauses included, matching what
resolve_units_to_clips actually plays) crosses the budget; the unit that
crosses the line still plays in full, and everything cut LEADS the offer.
Added on live evidence: the rules alone passed a same-category
188-unit/601s family core through untouched (category=childhood matched
every unit), so without a numeric bound the original failure ships
whenever the read selects recordings of one category.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, Iterable, List, Optional, Set, Tuple

from app.config import settings
from app.services.llm import llm_service

logger = logging.getLogger(__name__)

#: entity_relations types that make an entity "close/immediate family" for
#: RULE 3. First-degree ONLY — see the module docstring for why grandparent
#: et al. are excluded on purpose.
CLOSE_FAMILY_RELATION_TYPES = ("parent", "sibling", "spouse", "child")

_SYSTEM_PROMPT = """\
You split an OVERSIZED answer from a person's life-story video archive into
the part to play NOW and the part to OFFER next.

You are given the question and the currently selected units. Each unit line
carries the CATEGORY of the interview question its recording answered, and,
where present, a FAMILY tag naming the immediate-family members (parent,
sibling, spouse, child of the storyteller) that the unit itself explicitly
mentions.

Apply these rules:
1. Name the single interview CATEGORY (from the categories that appear in
   the unit lines) that most directly matches what the listener asked.
   Units from recordings of that category belong to the answer.
2. A unit tagged FAMILY belongs to the answer regardless of its category —
   but only that unit, never its whole recording.
3. EVERY other unit routes to the follow-up offer, not the answer.
Then phrase ONE short follow-up question in the storyteller's own
first-person voice inviting the listener to hear the offered material.

Use ONLY unit ids that appear in the list. Output ONLY JSON, exactly:
{"category": "...", "unit_ids": ["..."], "follow_up": {"question": "...", "unit_ids": ["..."]}}
"""

# Hebrew proclitics that can prefix a name (ו/ב/ל/מ/ש/ה/כ). Up to two, so
# "ושל..." style stacks still match. Bounded on both sides so a short name
# cannot match inside an unrelated word.
_PREFIXES = "ובלמשהכ"


def core_duration_sec(units: List) -> float:
    """Exact playable length of a selection — the trigger metric. Duration,
    not unit count: 188 short units and 30 long ones are different answers."""
    return sum(max(0.0, u.end_sec - u.start_sec) for u in units)


def _mentions(text: str, name: str) -> bool:
    """Does `text` explicitly mention `name`? Token-bounded, allowing up to
    two Hebrew prefix letters, so "לאברהם" matches אברהם but רן never
    matches inside ורני. Known limitation, accepted: a real name that IS
    another name plus a prefix letter (הרן vs רן) can cross-match; for
    RULE 3 both sides of every such pair in live data are first-degree
    anyway."""
    if not name or not text:
        return False
    pattern = (
        r"(?<![\w׳״])[" + _PREFIXES + r"]{0,2}" + re.escape(name) + r"(?![\w׳״])"
    )
    return re.search(pattern, text) is not None


async def _close_family_names(group_id: str) -> Set[str]:
    """Names of entities joined to the producer's is_self entity by a
    first-degree edge. Fail-open: any error returns an empty set (RULE 3
    simply keeps nothing extra, RULE 1 still applies)."""
    try:
        from sqlalchemy import and_, or_, select

        from app.database import AsyncSessionLocal
        from app.models import Entity, EntityRelation

        async with AsyncSessionLocal() as db:
            self_id = (
                await db.execute(
                    select(Entity.id).where(
                        Entity.producer_id == group_id, Entity.is_self
                    )
                )
            ).scalar_one_or_none()
            if self_id is None:
                return set()
            rows = (
                (
                    await db.execute(
                        select(Entity.name)
                        .select_from(EntityRelation)
                        .join(
                            Entity,
                            or_(
                                and_(
                                    EntityRelation.from_entity_id == Entity.id,
                                    EntityRelation.to_entity_id == self_id,
                                ),
                                and_(
                                    EntityRelation.to_entity_id == Entity.id,
                                    EntityRelation.from_entity_id == self_id,
                                ),
                            ),
                        )
                        .where(
                            EntityRelation.relation_type.in_(
                                CLOSE_FAMILY_RELATION_TYPES
                            ),
                            ~Entity.is_self,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return {n for n in rows if n}
    except Exception as e:  # pragma: no cover - exercised via live runs
        logger.warning(f"close-family lookup failed (RULE 3 inert): {e}")
        return set()


def _parse(raw: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except Exception:
        return None
    return out if isinstance(out, dict) else None


def _family_tag(text: str, close_names: Iterable[str]) -> List[str]:
    return [n for n in close_names if _mentions(text, n)]


async def maybe_compress(
    question: str,
    units: List,
    language: str,
    categories: Optional[Dict[str, Optional[str]]] = None,
    group_id: Optional[str] = None,
) -> Tuple[List, Optional[dict], bool]:
    """(served_units, raw_follow_up_or_None, compressed?).

    `categories` maps segment_id -> interview category (RULE 1's data);
    `group_id` enables the RULE 3 close-family lookup. Either may be
    omitted (tests, callers without the data): a unit with no category
    annotation is core-eligible by default — a rule with no data to apply
    must not delete footage.

    Under CORE_COMPRESSION_THRESHOLD_SEC (or with the feature off) this
    returns the input list UNTOUCHED with no LLM call and no DB read."""
    threshold = settings.CORE_COMPRESSION_THRESHOLD_SEC
    if not threshold or not units:
        return units, None, False
    total = core_duration_sec(units)
    if total <= threshold:
        return units, None, False

    close_names: Set[str] = (
        await _close_family_names(group_id) if group_id else set()
    )
    categories = categories or {}
    by_id = {u.unit_id: u for u in units}
    cat_of = {u.unit_id: categories.get(u.segment_id) for u in units}
    fam_of = {u.unit_id: _family_tag(u.text, close_names) for u in units}
    present_categories = sorted({c for c in cat_of.values() if c})

    lines = []
    for u in units:
        tags = f"[{cat_of[u.unit_id] or 'uncategorised'}]"
        if fam_of[u.unit_id]:
            tags += f" [FAMILY: {', '.join(fam_of[u.unit_id])}]"
        lines.append(
            f'{u.unit_id} {tags} [{u.end_sec - u.start_sec:.0f}s] "{u.text}"'
        )
    user_msg = (
        f"Question:\n{question}\n\n"
        f"Categories present: {', '.join(present_categories) or 'none'}\n\n"
        f"Selected units ({total:.0f}s total, far too much for one reply):\n"
        + "\n".join(lines)
    )
    try:
        raw = await llm_service.generate_response(
            messages=[{"role": "user", "content": user_msg}],
            system_prompt=_SYSTEM_PROMPT,
            temperature=0,
        )
        parsed = _parse(raw)
    except Exception as e:
        logger.warning(f"core compression call failed (serving full core): {e}")
        return units, None, False
    if not parsed:
        logger.warning("core compression reply unparseable (serving full core)")
        return units, None, False

    # NEVER-INVENT ENFORCEMENT (unchanged): only ids from the input survive,
    # order is the ARCHIVE's (input order), and an empty result fails open.
    kept_ids = {i for i in (parsed.get("unit_ids") or []) if i in by_id}

    # RULE ENFORCEMENT (code, not trust): the model's declared category must
    # be one actually present; otherwise RULE 1 cannot be applied and is
    # skipped (fail-open direction — never silently empty an answer over a
    # malformed declaration). A unit with no category annotation is exempt
    # from RULE 1. RULE 3 eligibility comes from the code-computed FAMILY
    # tags, never from anything the model asserts.
    declared = parsed.get("category")
    if declared not in present_categories:
        if categories:
            logger.warning(
                f"core compression declared category {declared!r} not in "
                f"{present_categories}; category rule skipped this turn"
            )
        declared = None

    def _eligible(uid: str) -> bool:
        cat = cat_of.get(uid)
        if cat is None or declared is None:
            return True  # no data for RULE 1 → cannot demote by category
        return cat == declared or bool(fam_of.get(uid))

    demoted = [
        u.unit_id
        for u in units
        if u.unit_id in kept_ids and not _eligible(u.unit_id)
    ]
    kept = [u for u in units if u.unit_id in kept_ids and _eligible(u.unit_id)]
    if not kept:
        logger.warning("core compression kept nothing (serving full core)")
        return units, None, False

    # SIZE BUDGET — see module docstring. Span accounting mirrors
    # resolve_units_to_clips: a unit contiguous with the previous one (same
    # segment, adjacent global index) plays from the previous unit's end, so
    # the pause between them counts; a new run starts at its own start. The
    # crossing unit is kept whole (never an orphaned cut two words short),
    # and the first unit is always kept, so this can never empty the core.
    budget = settings.CORE_COMPRESSION_TARGET_SEC
    truncated: List[str] = []
    if budget:
        span = 0.0
        cut_at = len(kept)
        prev = None
        for i, u in enumerate(kept):
            if span >= budget:
                cut_at = i
                break
            contiguous = (
                prev is not None
                and u.segment_id == prev.segment_id
                and getattr(u, "index", None) is not None
                and getattr(prev, "index", None) is not None
                and u.index == prev.index + 1
            )
            span += (u.end_sec - prev.end_sec) if contiguous else (
                u.end_sec - u.start_sec
            )
            prev = u
        truncated = [u.unit_id for u in kept[cut_at:]]
        kept = kept[:cut_at]
    kept_final = {u.unit_id for u in kept}

    raw_fu = parsed.get("follow_up") or None
    fu_question = raw_fu.get("question") if isinstance(raw_fu, dict) else None
    model_fu_ids = (
        (raw_fu.get("unit_ids") or []) if isinstance(raw_fu, dict) else []
    )
    # Budget-truncated units lead the offer (they are the direct
    # continuation of the answer), then rule-demoted units, then the
    # model's own offer picks; deduped, core overlap dropped — deferred,
    # never destroyed.
    fu_ids = [
        i
        for i in dict.fromkeys([*truncated, *demoted, *model_fu_ids])
        if i in by_id and i not in kept_final
    ]
    raw_fu = (
        {"question": fu_question, "unit_ids": fu_ids}
        if fu_ids and fu_question
        else None
    )

    logger.info(
        f"core compressed: {len(units)} units/{total:.0f}s -> "
        f"{len(kept)} units/{core_duration_sec(kept):.0f}s "
        f"(category={declared or 'n/a'}, demoted {len(demoted)}, "
        f"budget-cut {len(truncated)}), "
        f"offer {len((raw_fu or {}).get('unit_ids', []))} units"
    )
    return kept, raw_fu, True
