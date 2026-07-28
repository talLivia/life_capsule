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
from typing import Any, Dict, List, Optional

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


@dataclass(frozen=True)
class ExtractedEntity:
    """One named entity this recording mentioned, as the extractor saw it."""

    name: str
    type: str = "other"
    alternative_type: Optional[str] = None
    summary: Optional[str] = None

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
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExtractedEntity":
        return cls(
            name=data["name"],
            type=data.get("type") or "other",
            alternative_type=data.get("alternative_type"),
            summary=data.get("summary"),
        )


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
    match = re.search(r"\[.*\]", text or "", re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
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


async def extract_entities(transcript: str) -> List[ExtractedEntity]:
    """Extract this recording's named entities.

    Fail-soft, like every other extraction node in the pipeline: an LLM
    failure returns no entities rather than failing the segment. The recording
    itself — transcript, chunks, units — is what answers are built from, and
    it is already saved by the time this runs. A missing entity map degrades
    retrieval slightly (measured: accuracy 0.991 with AND without it); a
    failed segment loses the recording.
    """
    if not transcript:
        return []
    try:
        raw = await llm_service.generate_response(
            messages=[{"role": "user", "content": transcript}],
            system_prompt=_ENTITY_EXTRACTION_SYSTEM_PROMPT,
            temperature=0,  # structured extraction — deterministic
        )
    except Exception as e:
        logger.warning(f"entity extraction failed: {e}")
        return []
    return parse_extracted_entities(raw)
