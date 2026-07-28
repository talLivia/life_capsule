"""Tests for the entity merge key.

This function decides entity IDENTITY — two names normalising to the same key
become one row via UNIQUE (producer_id, normalized_name). Getting it wrong
merges one person's story into another's, silently, with nothing in the UI to
reveal it. Hence direct tests rather than coverage through the write path.
"""

import pytest

from app.services.entity_names import names_match, normalize_entity_name as norm


# ── What MUST collapse: differences carrying no information ─────────────────


@pytest.mark.parametrize(
    "a,b,why",
    [
        ("Gila", "gila", "case"),
        ("  גילה  ", "גילה", "surrounding whitespace"),
        ("בת   שבע", "בת שבע", "repeated whitespace"),
        ("בת-שבע", "בת שבע", "ASCII hyphen joins words"),
        ("בת־שבע", "בת שבע", "Hebrew maqaf joins words"),
        ("גִּילָה", "גילה", "niqqud — transcripts are unpointed, corrections may not be"),
    ],
)
def test_meaningless_differences_collapse(a, b, why):
    assert names_match(a, b), why


@pytest.mark.parametrize(
    "medial,final", [("כ", "ך"), ("מ", "ם"), ("נ", "ן"), ("פ", "ף"), ("צ", "ץ")]
)
def test_hebrew_final_forms_fold(medial, final):
    """Final (sofit) letters are the SAME letter in end position — purely
    orthographic, so folding them can never merge two different names."""
    assert norm(medial) == norm(final)


def test_final_form_folding_applies_inside_real_names():
    assert norm("ירושלים") == norm("ירושלימ")


# ── What MUST NOT collapse: real letters that distinguish real names ────────


@pytest.mark.parametrize(
    "a,b,why",
    [
        ("טל", "תל", "ט and ת are different letters — this pair is the producer's own name vs 'Tel'"),
        ("טבריה", "תבריה", "a TRANSCRIPTION error, not an orthographic variant — belongs to fuzzy + human confirm"),
        ("אילנה", "עילנה", "א and ע are different letters"),
        ("ניר", "רז", "different names entirely"),
        ("חן", "חנה", "one name is not a prefix-merge of another"),
    ],
)
def test_genuinely_different_names_stay_apart(a, b, why):
    """A false merge is far worse than a false split: it silently attributes
    one person's story to another and the producer cannot see it happened. A
    false split surfaces in the extraction panel as two similar names and is
    fixable by confirming them the same."""
    assert not names_match(a, b), why


def test_definite_article_is_preserved():
    """"הכפר הירוק" is a proper name that begins with ה. Stripping the article
    would produce "כפר הירוק" — a different string that merges with nothing
    and no longer matches what the producer said."""
    assert norm("הכפר הירוק") != norm("כפר הירוק")


# ── Key stability ───────────────────────────────────────────────────────────


def test_normalisation_is_idempotent():
    """The stored key is re-derived on every write; normalising an already
    normalised name must not drift, or a row would stop matching itself."""
    for name in ["גילה", "הכפר הירוק", "Tal Nahum", "בת־שבע", "חיל האוויר"]:
        once = norm(name)
        assert norm(once) == once


def test_unicode_composition_does_not_change_the_key():
    """Two inputs differing only by NFC/NFD composition must produce ONE key —
    otherwise the same name typed on two systems creates two entities."""
    import unicodedata

    name = "גִילָה"
    assert norm(unicodedata.normalize("NFC", name)) == norm(
        unicodedata.normalize("NFD", name)
    )


def test_empty_and_whitespace_are_empty_and_never_match():
    assert norm("") == ""
    assert norm("   ") == ""
    # Two blank names must NOT be considered the same entity — that would
    # collapse every failed extraction into one row.
    assert not names_match("", "")
    assert not names_match("  ", "")
