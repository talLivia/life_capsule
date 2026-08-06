"""Tests for structured entity extraction.

The parsing half is tested directly and hard, because it is the boundary
between a model's free-form reply and a table with CHECK constraints on it. A
type this function lets through unchanged that the column rejects fails the
whole ingest; a type it silently mangles produces a wrong label on a screen
whose entire purpose is showing what the system understood.
"""

from unittest.mock import AsyncMock

import pytest

from app.services import entity_extraction as ex
from app.services.entity_extraction import (
    ENTITY_TYPES,
    ExtractedEntity,
    parse_extracted_entities as parse,
)

pytestmark = pytest.mark.asyncio


# ── Parsing the happy shape ─────────────────────────────────────────────────


async def test_parses_the_four_fields():
    entities = parse(
        '[{"name": "גילה", "type": "person", "alternative_type": null,'
        ' "summary": "סבתא של הדובר"}]'
    )
    assert entities == [
        ExtractedEntity(
            name="גילה", type="person", alternative_type=None, summary="סבתא של הדובר"
        )
    ]


async def test_parses_through_a_markdown_fence():
    """Models wrap JSON in fences unprompted; the old names-only parser
    tolerated it and this one must too."""
    entities = parse('```json\n[{"name": "טבריה", "type": "place"}]\n```')
    assert [e.name for e in entities] == ["טבריה"]


async def test_garbage_yields_no_entities_rather_than_raising():
    assert parse("I could not find any entities.") == []
    assert parse("") == []
    assert parse('{"name": "not a list"}') == []
    assert parse("[{unclosed") == []


async def test_non_object_items_are_skipped_not_fatal():
    """A model that half-reverts to the old bare-string format must not take
    the entities it DID format correctly down with it."""
    entities = parse('["גילה", {"name": "טבריה", "type": "place"}]')
    assert [e.name for e in entities] == ["טבריה"]


# ── Names ───────────────────────────────────────────────────────────────────


async def test_nameless_entities_are_dropped():
    """A nameless entity cannot be merged, shown or confirmed — the merge key
    is derived from the name, so there is nothing it could be."""
    assert parse('[{"name": "", "type": "person"}, {"name": "   "}]') == []


async def test_name_is_kept_verbatim_apart_from_surrounding_space():
    """`name` is what gets shown back to the producer. Normalisation is for
    the key, never for the display value."""
    entities = parse('[{"name": "  הכפר הירוק  ", "type": "place"}]')
    assert entities[0].name == "הכפר הירוק"


async def test_duplicates_within_one_transcript_collapse_to_one():
    """One recording contributes ONE mention per entity — the unique
    constraint on entity_mentions says so. Collapsing here keeps the count the
    writer reports honest instead of letting the constraint absorb it."""
    entities = parse(
        '[{"name": "ניר", "type": "person"}, {"name": "ניר ", "type": "person"},'
        ' {"name": "נִיר", "type": "person"}]'
    )
    assert len(entities) == 1


# ── Types: what the column will actually accept ─────────────────────────────


@pytest.mark.parametrize("type_name", ENTITY_TYPES)
async def test_every_valid_type_survives_parsing(type_name):
    entities = parse('[{"name": "X", "type": "%s"}]' % type_name)
    assert entities[0].type == type_name


async def test_us_spelling_of_organisation_is_not_silently_lost():
    """The schema spells it -isation and a model writing English defaults to
    -ization. Without the synonym map every organisation in the archive would
    land as 'other', which is indistinguishable from a failed classification."""
    assert parse('[{"name": "חיל האוויר", "type": "organization"}]')[0].type == (
        "organisation"
    )
    assert parse('[{"name": "טבריה", "type": "location"}]')[0].type == "place"


async def test_unknown_type_becomes_other_rather_than_dropping_the_entity():
    """A name we cannot classify is still a name, and the entity map is the
    load-bearing job. Dropping it to avoid a wrong `type` would trade the
    thing that matters for the thing that does not."""
    entities = parse('[{"name": "עכבר", "type": "animal"}]')
    assert entities[0].type == "other"
    assert entities[0].name == "עכבר"


async def test_type_matching_is_case_and_space_insensitive():
    assert parse('[{"name": "X", "type": " Person "}]')[0].type == "person"


async def test_missing_or_non_string_type_becomes_other():
    assert parse('[{"name": "X"}]')[0].type == "other"
    assert parse('[{"name": "X", "type": 7}]')[0].type == "other"


# ── alternative_type: the confirmation trigger ──────────────────────────────


async def test_alternative_type_survives_and_flags_confirmation():
    entity = parse(
        '[{"name": "הכפר הירוק", "type": "place", "alternative_type": "organisation"}]'
    )[0]
    assert entity.alternative_type == "organisation"
    assert entity.needs_type_confirmation


async def test_a_clear_classification_asks_nothing():
    """Asking about everything trains the producer to click yes without
    reading, which is worse than not asking."""
    entity = parse('[{"name": "ניר", "type": "person", "alternative_type": null}]')[0]
    assert entity.alternative_type is None
    assert not entity.needs_type_confirmation


async def test_alternative_equal_to_primary_is_dropped():
    """Otherwise the confirmation screen offers the producer a choice between
    two identical options."""
    entity = parse(
        '[{"name": "X", "type": "person", "alternative_type": "person"}]'
    )[0]
    assert entity.alternative_type is None


async def test_alternative_type_never_offers_a_category_the_prompt_did_not():
    """'other' is a schema fallback, not a category worth asking about — a
    screen asking "is this a place or an other?" is not a question."""
    entity = parse('[{"name": "X", "type": "place", "alternative_type": "other"}]')[0]
    assert entity.alternative_type is None

    entity = parse('[{"name": "X", "type": "place", "alternative_type": "gibberish"}]')[0]
    assert entity.alternative_type is None


# ── Summaries ───────────────────────────────────────────────────────────────


async def test_blank_summary_becomes_none_not_empty_string():
    """NULL and '' would render differently downstream while meaning the same
    thing, and only one of them is honest about having nothing to say."""
    assert parse('[{"name": "X", "type": "person", "summary": "   "}]')[0].summary is None
    assert parse('[{"name": "X", "type": "person"}]')[0].summary is None


# ── State round-trip ────────────────────────────────────────────────────────


async def test_dict_round_trip_is_lossless():
    """The pipeline checkpoints this between the node that extracts and the
    node that writes, which a human confirmation can separate by days."""
    entity = ExtractedEntity(
        name="הכפר הירוק",
        type="place",
        alternative_type="organisation",
        summary="הפנימייה שבה למד הדובר",
    )
    assert ExtractedEntity.from_dict(entity.as_dict()) == entity


# ── The call itself ─────────────────────────────────────────────────────────


async def test_extract_entities_sends_the_transcript_and_parses_the_reply(monkeypatch):
    mock = AsyncMock(return_value='[{"name": "גילה", "type": "person"}]')
    monkeypatch.setattr(ex.llm_service, "generate_response", mock)

    entities = await ex.extract_entities("סבתא שלי גילה גרה בטבריה")

    assert [e.name for e in entities] == ["גילה"]
    kwargs = mock.call_args.kwargs
    assert kwargs["messages"][0]["content"] == "סבתא שלי גילה גרה בטבריה"
    assert kwargs["temperature"] == 0


async def test_extract_entities_is_fail_soft(monkeypatch):
    """The recording is already saved by the time this runs. A missing entity
    map costs a little retrieval quality (measured: 0.991 with AND without);
    a failed segment costs the recording."""
    monkeypatch.setattr(
        ex.llm_service, "generate_response", AsyncMock(side_effect=RuntimeError("down"))
    )
    assert await ex.extract_entities("some transcript") == []


async def test_extract_entities_skips_the_call_for_an_empty_transcript(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(ex.llm_service, "generate_response", mock)
    assert await ex.extract_entities("") == []
    mock.assert_not_awaited()


async def test_the_prompt_says_to_omit_what_fits_no_category():
    """The עכבר case — a common noun the old extractor returned as an entity.
    The prompt has to say this outright; nothing downstream can tell a real
    'other' from a noun that should never have been extracted."""
    prompt = ex._ENTITY_EXTRACTION_SYSTEM_PROMPT
    assert "OMIT IT" in prompt
    for offered in ex.PROMPTED_ENTITY_TYPES:
        assert offered in prompt
    # "other" must NOT be offered as a category, or the model has somewhere to
    # park exactly the leftovers it is being told to drop.
    assert '"other"' not in prompt


# ── Hebrew gershayim breaks JSON ─────────────────────────────────────────────
#
# Found on a real recording: "לאמא שלי קוראים אילנה … הייתה מורה לתנ״ך".
# The model wrote a correct summary containing תנ"ך, whose gershayim is an
# ASCII double quote, which terminated the JSON string early. json.loads
# raised, the parser returned [], and every entity from that recording was
# silently dropped — no entity, no tree entry, no confirmation question.
# Reproducible 3/3 against the live model.


def test_gershayim_in_a_summary_does_not_drop_the_entities():
    raw = (
        '[\n'
        '  {"name": "אילנה", "type": "person", "alternative_type": null,\n'
        '   "summary": "אמא של הדובר, הייתה מורה לתנ"ך ועובדת היום בעירייה."},\n'
        '  {"name": "טבריה", "type": "place", "alternative_type": null,\n'
        '   "summary": "המקום בו נולדה אמו של הדובר."}\n'
        ']'
    )
    entities = parse(raw)
    assert [e.name for e in entities] == ["אילנה", "טבריה"]
    # The summary keeps the quote as written — it is the producer's language,
    # not a defect to normalise away.
    assert 'תנ"ך' in entities[0].summary


def test_gershayim_in_relation_evidence_does_not_drop_the_relations():
    """`evidence` quotes the transcript verbatim, so it is the field most
    likely to carry one."""
    raw = (
        'ENTITIES:\n[{"name": "אילנה", "type": "person", '
        '"alternative_type": null, "summary": "s"}]\n'
        'RELATIONS:\n[{"from": "אילנה", "to": "__SELF__", "type": "parent", '
        '"evidence": "אמא שלי לימדה תנ"ך בבית הספר"}]'
    )
    relations = ex.parse_extracted_relations(raw, ["אילנה"], ["parent"])
    assert len(relations) == 1
    assert relations[0].relation_type == "parent"


def test_well_formed_json_is_not_touched_by_the_repair():
    """The repair only runs on text that already failed to parse, so a valid
    reply cannot be altered by it — including one with ESCAPED quotes."""
    raw = (
        '[{"name": "X", "type": "person", "alternative_type": null, '
        '"summary": "he said \\"hello\\" once"}]'
    )
    entities = parse(raw)
    assert entities[0].summary == 'he said "hello" once'


def test_several_gershayim_in_one_reply():
    raw = (
        '[{"name": "צה\"ל", "type": "organisation", "alternative_type": null, '
        '"summary": "שירת בצה"ל ולמד אח"כ באוני"ב"}]'
    )
    entities = parse(raw)
    assert len(entities) == 1
