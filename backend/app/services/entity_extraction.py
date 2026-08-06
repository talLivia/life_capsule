"""Structured named-entity extraction — what ONE recording says about whom.

This replaces two extractions with one, which is the point:

  * `analysis_graph`'s old names-only call, which returned a JSON array of
    bare strings and therefore could not say what kind of thing a name was;
  * Graphiti's own internal extraction, which is where entity SUMMARIES came
    from. Once the write path is Postgres, nothing else produces them, so a
    summary has to come out of this call or it does not exist at all.

Running ONE call for both jobs is not just cheaper. Two calls over the same
transcript can disagree — one extracting "טבריה" and the other not — and then
the names a human is asked to confirm are not the names that get written. The
same list does both.

The output shape is `{name, type, alternative_type, summary}`:

`type` is a property of the THING (Gila is a person regardless of which
recording names her) and lives on `entities`. `summary` is a property of the
TELLING and lives on `entity_mentions` — see that model for why that removes
summary regeneration entirely rather than relocating it.

`alternative_type` is deliberately NOT a confidence score. Self-reported
confidence is uncalibrated; "which two are you torn between" is concrete,
checkable, and populates a confirmation screen with exactly two options. It is
non-null only when the classification is genuinely unclear, and that is the
whole trigger for asking the producer (chunk 4's batched confirmation).

Note `segment_extraction.ExtractedEntity` is a different, UI-facing class for
the "extracted from this" panel — a read model. This one is what the extractor
actually returned.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.services.entity_names import normalize_entity_name
from app.services.llm import llm_service

logger = logging.getLogger(__name__)

# The vocabulary `entities.type` accepts — kept in step with the CHECK
# constraint in migration 0012, which is the authority. A type outside this
# set would be rejected by the database, so it is coerced here instead (see
# `_coerce_type`).
ENTITY_TYPES = ("person", "place", "organisation", "event", "other")

# What the PROMPT offers, which is deliberately narrower than what the column
# accepts. "other" is a schema-level fallback for a model that answered badly,
# never a category the model is invited to park things in — a transcript's
# leftovers are supposed to be OMITTED (the עכבר case), and offering "other"
# would give the model somewhere to put them instead.
PROMPTED_ENTITY_TYPES = ("person", "place", "organisation", "event")

# The schema spells it -isation; a model writing English defaults to -ization,
# and "location" for a place is just as common. Without this map every
# organisation in the archive would silently land as type='other', which looks
# exactly like a model that failed to classify.
_TYPE_SYNONYMS = {
    "organization": "organisation",
    "org": "organisation",
    "location": "place",
}

_ENTITY_EXTRACTION_SYSTEM_PROMPT = """\
You are a strict named-entity extractor for a personal life-story archive. \
Given ONE recording's transcript, output ONLY a JSON array of objects - one \
per distinct named entity mentioned in it. No commentary, no explanation, no \
text outside the array.

Each object has exactly these four fields:
  "name": the proper name exactly as it appears in the transcript, in the \
same language and script. Never translate, transliterate or correct it.
  "type": one of person, place, organisation, event.
  "alternative_type": the runner-up type if you are genuinely torn between \
two of them, otherwise null. Set it ONLY when the choice is really unclear - \
a person named plainly is not a torn case.
  "summary": one short sentence, in the SAME language as the transcript, \
saying what THIS transcript says about this entity, phrased relative to the \
speaker (e.g. "the speaker's brother", "where the speaker grew up"). Only \
what this transcript actually says - never anything you know from elsewhere.

THE SPEAKER IS NEVER AN ENTITY. The transcript is the speaker's own account, \
so when they name THEMSELVES - "my name is X", "I am X", "we are five: X, \
Y and Z" where X is the speaker - omit that name from the array entirely. \
They are the person the whole archive belongs to, not somebody in it, and \
extracting them creates a second, disconnected copy of the producer inside \
their own family tree. Use "__SELF__" for them in relations instead. Everyone \
ELSE named in the same breath is still extracted normally.

IF SOMETHING FITS NONE OF THE FOUR TYPES, IT IS NOT A NAMED ENTITY - OMIT IT. \
Common nouns are not entities: a mouse, a dog, a car, an unnamed school are \
all omitted. Pronouns and generic descriptions ("my commander", "the right \
person") are omitted. When in doubt whether something is a NAME at all, leave \
it out. When in doubt which TYPE a real name is, include it and set \
alternative_type.

Example output:
[{"name": "גילה", "type": "person", "alternative_type": null, "summary": \
"סבתא של הדובר"}, {"name": "הכפר הירוק", "type": "place", \
"alternative_type": "organisation", "summary": "הפנימייה שבה למד הדובר"}]"""


# The speaker. A transcript names other people but almost never themselves, so
# every relation to the producer needs a marker instead of a name. A literal
# that cannot collide with one — no transcript contains this string.
SELF = "__SELF__"

# Appended to the entity prompt so ONE call does both jobs. A second call over
# the same transcript could name people the first did not extract, and a
# relation would then point at an entity that never gets created.
#
# `{vocabulary}` is filled from the relation_types TABLE, not hardcoded: that
# table is the source (entity_relations has a FK to it), so adding a relation
# type must not require editing a prompt. The FK is the backstop — an invented
# type fails loudly rather than being stored.
_RELATION_PROMPT_SUFFIX = """

SECOND TASK - family relations.

After the entity array, output the line RELATIONS: and then a SECOND JSON \
array of family relations the transcript STATES, with exactly these fields: \
{{"from": ..., "to": ..., "type": ..., "evidence": ...}}.

  "from" / "to": either a name EXACTLY as it appears in your entity array \
above, or the literal "__SELF__" for the speaker themselves.
  "type": one of: {vocabulary}
  "evidence": the short phrase from the transcript that states it.

DIRECTION MATTERS AND IS EASY TO GET BACKWARDS. "from" is the SUBJECT: the \
relation reads "<from> is the <type> of <to>".
  "ניר הוא אח שלי" -> {{"from": "ניר", "to": "__SELF__", "type": "sibling"}}
  "צבי הוא אבא שלי" -> {{"from": "צבי", "to": "__SELF__", "type": "parent"}} \
because צבי is the PARENT OF the speaker, NOT the child.
  "יש לי בת בשם מיה" -> {{"from": "מיה", "to": "__SELF__", "type": "child"}} \
because מיה is the CHILD OF the speaker.

Only propose a relation when BOTH ends are real and the transcript states it \
outright. Never infer one from context, never guess at a person whose \
connection is not stated, and never use an endpoint that is not in your \
entity array (or __SELF__). If there are none, output an empty array. Fewer \
certain relations are far better than more likely ones - a wrong relation in \
a family tree is visible and damaging.

Output nothing after the second array."""


def build_extraction_prompt(
    relation_vocabulary: List[str], speaker_name: Optional[str] = None
) -> str:
    """The entity prompt, plus the relation task when a vocabulary is given.

    An empty vocabulary means relations are not being captured at all, and the
    prompt then asks for entities exactly as before — byte-identical, so
    nothing about existing extraction changes when relations are off.

    `speaker_name` is what makes "the speaker is never an entity" actually
    work, and it was MEASURED rather than assumed. Told only the rule, the
    model extracted "טל" from "אנחנו חמישה: אני טל, עדי…" on 2 of 2 runs —
    it cannot tell which of twelve names is the one narrating. Told that the
    speaker is called "Tal Nahum", it dropped it on 2 of 2, across the script
    boundary the merge key cannot cross.
    """
    prompt = _ENTITY_EXTRACTION_SYSTEM_PROMPT
    if speaker_name:
        prompt += (
            f'\n\nThe speaker of THIS transcript is called "{speaker_name}", and may '
            f"refer to themselves by any form of that name, including a short form "
            f"or the same name written in another script. That name is the SPEAKER "
            f"- never extract it as an entity."
        )
    if not relation_vocabulary:
        return prompt
    return prompt + _RELATION_PROMPT_SUFFIX.format(
        vocabulary=", ".join(sorted(relation_vocabulary))
    )


@dataclass(frozen=True)
class ExtractedEntity:
    """One named entity this recording mentioned, as the extractor saw it."""

    name: str
    type: str = "other"
    alternative_type: Optional[str] = None
    summary: Optional[str] = None
    # True once the PRODUCER answered this entity's type question. It is what
    # lets the writer tell "the extractor guessed place" from "a human said
    # place" — the first must not overwrite an existing type, the second must.
    # Never set by the extractor itself; only by human_confirm_node.
    type_confirmed: bool = False
    # Set only by human_confirm_node from the producer's answer, never by the
    # extractor — the transcript rarely dates anything, and a year guessed
    # from context would silently reorder a life on the timeline.
    year_start: Optional[int] = None
    # True when the confirmation screen PUT the year question to the producer,
    # regardless of whether they answered. Stamps entities.year_asked_at so the
    # same question never reappears on a later recording.
    year_asked: bool = False

    @property
    def needs_type_confirmation(self) -> bool:
        """Ask the producer if and only if the extractor was torn."""
        return self.alternative_type is not None

    def as_dict(self) -> Dict[str, Any]:
        """Plain dict for LangGraph state.

        The pipeline's state is checkpointed and has to survive serialisation
        between the node that extracts and the node that writes, which may be
        separated by a human confirmation lasting days. A dataclass is not
        that; a dict is.
        """
        return {
            "name": self.name,
            "type": self.type,
            "alternative_type": self.alternative_type,
            "summary": self.summary,
            "type_confirmed": self.type_confirmed,
            "year_start": self.year_start,
            "year_asked": self.year_asked,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExtractedEntity":
        return cls(
            name=data["name"],
            type=data.get("type") or "other",
            alternative_type=data.get("alternative_type"),
            summary=data.get("summary"),
            type_confirmed=bool(data.get("type_confirmed")),
            year_start=data.get("year_start"),
            year_asked=bool(data.get("year_asked")),
        )


@dataclass(frozen=True)
class ExtractedRelation:
    """One family relation the transcript stated, as proposed — never applied.

    Endpoints are NAMES (or `SELF`), not ids: at extraction time the entities
    may not exist yet, and after confirmation an identity answer can rename one
    of them. Resolution to ids happens at write time, against the entities this
    same extraction produced.
    """

    from_name: str
    to_name: str
    relation_type: str
    evidence: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        """Plain dict for LangGraph state — same reason as ExtractedEntity."""
        return {
            "from_name": self.from_name,
            "to_name": self.to_name,
            "relation_type": self.relation_type,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExtractedRelation":
        return cls(
            from_name=data["from_name"],
            to_name=data["to_name"],
            relation_type=data["relation_type"],
            evidence=data.get("evidence"),
        )


def _escape_inner_quotes(raw: str) -> str:
    """Escape double quotes that appear INSIDE a JSON string value.

    HEBREW BREAKS JSON, and it does it silently. Gershayim — the mark in
    תנ"ך, צה"ל, ארה"ב, ד"ר, עו"ד — is an ASCII double quote, so a model
    writing a perfectly good Hebrew summary emits

        "summary": "הייתה מורה לתנ"ך ועובדת בעירייה"

    which terminates the string at `לתנ` and makes the whole array
    unparseable. `json.loads` raises, the parser returns [], and EVERY entity
    from that recording is dropped — no entity, no tree entry, no confirmation
    question, and nothing to distinguish it from a recording that genuinely
    mentioned nobody. Found on a real recording ("לאמא שלי קוראים אילנה…"),
    reproducible 3/3, and it cost that producer their mother.

    A repair pass rather than a prompt instruction, because the prompt cannot
    make this safe: the model is not making a mistake — it is writing Hebrew,
    and any Hebrew summary may contain the character.

    Only quotes that cannot be structural are escaped. A `"` closing a string
    is followed, ignoring whitespace, by one of `,:}]` or the end of input;
    anything else is text the model meant literally.
    """
    out: List[str] = []
    in_string = False
    i = 0
    while i < len(raw):
        char = raw[i]
        if char == "\\" and in_string and i + 1 < len(raw):
            # Already escaped — copy the pair through untouched.
            out.append(raw[i : i + 2])
            i += 2
            continue
        if char == '"':
            if not in_string:
                in_string = True
                out.append(char)
            else:
                following = raw[i + 1 :].lstrip()
                if following[:1] in (",", ":", "}", "]", ""):
                    in_string = False
                    out.append(char)
                else:
                    out.append('\\"')
            i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _loads_tolerant(blob: str):
    """`json.loads`, retried once with inner quotes escaped.

    The happy path is untouched — the repair only ever runs on text that has
    already failed to parse, so well-formed output cannot be altered by it.
    """
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        pass
    try:
        repaired = json.loads(_escape_inner_quotes(blob))
    except json.JSONDecodeError:
        return None
    logger.info("extraction JSON repaired: an unescaped quote inside a string value")
    return repaired


def parse_extracted_relations(
    text: str, entity_names: List[str], allowed_types: List[str]
) -> List[ExtractedRelation]:
    """Parse the relations array, dropping everything unusable.

    Strict on purpose, in a way entity parsing is not. An entity we cannot
    classify is still a real name worth keeping; a relation we cannot resolve
    is a claim about two people that would land in a family tree. Dropped:

      * a type outside the offered vocabulary (the FK would reject it anyway,
        but failing here names the value in a log instead of aborting a write);
      * an endpoint that is neither SELF nor one of THIS extraction's entities
        — it would point at a row that is never created;
      * a self-loop, which `ck_entity_relations_not_self` forbids;
      * duplicates of an already-seen (from, to, type).
    """
    marker = re.split(r"RELATIONS:", text or "", maxsplit=1)
    if len(marker) < 2:
        return []
    match = re.search(r"\[.*\]", marker[1], re.DOTALL)
    if not match:
        return []
    # Same repair as the entities array: `evidence` quotes the transcript
    # verbatim, so it is the field MOST likely to carry a gershayim.
    data = _loads_tolerant(match.group(0))
    if not isinstance(data, list):
        return []

    # Match endpoints on the MERGE key, not the raw string, so trailing
    # whitespace or a final-letter variant still resolves to the same entity
    # the writer will create.
    by_key = {normalize_entity_name(n): n for n in entity_names}
    allowed = set(allowed_types)

    out: List[ExtractedRelation] = []
    seen: set = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        rel_type = str(item.get("type") or "").strip().lower()
        if rel_type not in allowed:
            logger.info(f"Dropping proposed relation with unknown type {rel_type!r}")
            continue

        def _resolve(raw: Any) -> Optional[str]:
            name = str(raw or "").strip()
            if name == SELF:
                return SELF
            return by_key.get(normalize_entity_name(name))

        src, dst = _resolve(item.get("from")), _resolve(item.get("to"))
        if src is None or dst is None:
            logger.info(
                f"Dropping proposed relation {item.get('from')!r} -> "
                f"{item.get('to')!r}: an endpoint is not an extracted entity"
            )
            continue
        if src == dst:
            continue

        key = (normalize_entity_name(src), normalize_entity_name(dst), rel_type)
        if key in seen:
            continue
        seen.add(key)

        evidence = str(item.get("evidence") or "").strip() or None
        out.append(
            ExtractedRelation(
                from_name=src, to_name=dst, relation_type=rel_type, evidence=evidence
            )
        )
    return out


def _coerce_type(raw: Any) -> str:
    """A model-supplied type, mapped onto what the column accepts.

    Coerces rather than rejects: a name we cannot classify is still a name,
    and the entity map ("which recordings mention this") is the load-bearing
    job here. Dropping the entity to avoid a wrong `type` would trade the
    thing that matters for the thing that does not.
    """
    if not isinstance(raw, str):
        return "other"
    value = raw.strip().lower()
    value = _TYPE_SYNONYMS.get(value, value)
    return value if value in ENTITY_TYPES else "other"


def _coerce_alternative_type(raw: Any, primary: str) -> Optional[str]:
    """The runner-up type, or None.

    Only ever one of the types the prompt actually offered, and never equal to
    the primary: both of those would produce a confirmation screen asking the
    producer to choose between two identical options, or between an option and
    a category they were never offered.
    """
    if raw is None:
        return None
    value = _coerce_type(raw)
    if value == primary or value not in PROMPTED_ENTITY_TYPES:
        return None
    return value


def parse_extracted_entities(text: str) -> List[ExtractedEntity]:
    """Parse the model's reply into entities, dropping what cannot be used.

    Tolerant of the wrappers models add (markdown fences, a sentence before
    the array) and strict about content: an object with no usable `name` is
    dropped entirely, because a nameless entity cannot be merged, shown, or
    confirmed — there is nothing it could be.
    """
    # Only the text BEFORE the relations marker. The search below is greedy —
    # `\[.*\]` spans from the first bracket to the LAST one — so once the reply
    # carries a second array it would swallow both and parse neither, silently
    # returning zero entities from a perfectly good extraction. Splitting first
    # is what keeps the two arrays independent.
    head = re.split(r"RELATIONS:", text or "", maxsplit=1)[0]

    match = re.search(r"\[.*\]", head, re.DOTALL)
    if not match:
        return []
    data = _loads_tolerant(match.group(0))
    if not isinstance(data, list):
        return []

    entities: List[ExtractedEntity] = []
    # Deduplicate on the SAME key the database merges on, not on the raw
    # string: "ניר" and "ניר " in one transcript are one entity, and one
    # recording contributes one mention row per entity (the unique constraint
    # on entity_mentions says so). Deduplicating here keeps the count the
    # writer reports honest rather than letting the constraint quietly absorb
    # the difference.
    seen: set = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        key = normalize_entity_name(name)
        if not key or key in seen:
            continue
        seen.add(key)

        primary = _coerce_type(item.get("type"))
        summary = str(item.get("summary") or "").strip() or None
        entities.append(
            ExtractedEntity(
                name=name,
                type=primary,
                alternative_type=_coerce_alternative_type(
                    item.get("alternative_type"), primary
                ),
                summary=summary,
            )
        )
    return entities


async def extract(
    transcript: str,
    relation_vocabulary: Optional[List[str]] = None,
    speaker_name: Optional[str] = None,
) -> Tuple[List[ExtractedEntity], List[ExtractedRelation]]:
    """This recording's named entities, and any family relations it states.

    ONE call for both. Two calls over the same transcript can disagree — one
    extracting "טבריה" and the other not — and a relation whose endpoint was
    not extracted points at an entity that never gets created.

    Fail-soft, like every other extraction node: an LLM failure returns
    nothing rather than failing the segment. The recording itself is what
    answers are built from and is already saved by the time this runs. A
    missing entity map degrades retrieval slightly (measured: accuracy 0.991
    with AND without it); a failed segment loses the recording.

    Relations degrade further and separately: if the relation array is
    malformed or names people that were not extracted, the ENTITIES still land
    and only the relations are dropped. Losing a proposed relation costs a
    question the producer was never asked; losing the entities costs the
    entity map.
    """
    if not transcript:
        return [], []
    vocabulary = relation_vocabulary or []
    try:
        raw = await llm_service.generate_response(
            messages=[{"role": "user", "content": transcript}],
            system_prompt=build_extraction_prompt(vocabulary, speaker_name),
            temperature=0,  # structured extraction — deterministic
        )
    except Exception as e:
        logger.warning(f"entity extraction failed: {e}")
        return [], []

    entities = parse_extracted_entities(raw)
    if not vocabulary or not entities:
        # No vocabulary means relations were never asked for; no entities means
        # every endpoint would be unresolvable anyway.
        return entities, []
    relations = parse_extracted_relations(raw, [e.name for e in entities], vocabulary)
    return entities, relations


async def extract_entities(transcript: str) -> List[ExtractedEntity]:
    """Entities only — the pre-relations entry point, kept for callers that
    genuinely do not want relations (and for the tests that pin entity
    parsing on its own)."""
    entities, _ = await extract(transcript)
    return entities
