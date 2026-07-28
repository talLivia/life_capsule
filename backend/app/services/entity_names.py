"""Name normalisation — the merge key for entities.

`entities.normalized_name` is what decides whether two extracted names are the
same real-world thing, via UNIQUE (producer_id, normalized_name). Everything
about entity identity rests on this function, so it lives alone and is tested
directly rather than being an inline `.lower()` somewhere in the write path.

DESIGN RULE: normalise only differences that are ALWAYS meaningless, never
differences that are merely usually meaningless. A false merge is far worse
than a false split — two entities wrongly merged silently attribute one
person's story to another, and the producer has no way to see it happened. A
false split shows up in the extraction panel as two similar names and can be
fixed by confirming them as the same. So this deliberately does LESS than a
fuzzy matcher would.

Fuzzy matching still exists, one layer up and with a human in the loop:
pg_trgm proposes candidates and `names_are_similar` gates them, exactly as
before. This function only collapses variants that carry no information at
all.
"""

from __future__ import annotations

import re
import unicodedata

# Hebrew final letters (sofit). The same letter, positional variant only:
# "ניר" written mid-word vs at the end is the same name. Folding these is
# always safe because the distinction is orthographic, never semantic.
_FINAL_FORMS = {
    "ך": "כ",  # ך -> כ
    "ם": "מ",  # ם -> מ
    "ן": "נ",  # ן -> נ
    "ף": "פ",  # ף -> פ
    "ץ": "צ",  # ץ -> צ
}

# Maqaf (Hebrew hyphen) and the ASCII hyphen both join words; normalise to a
# space so "בת-שבע" and "בת שבע" meet.
_JOINERS = re.compile(r"[־\-]")

_WHITESPACE = re.compile(r"\s+")

# Definite article on a name is a real ambiguity we do NOT touch: "הכפר
# הירוק" is a proper name that begins with ה, and stripping it would turn it
# into "כפר הירוק" — a different string that merges with nothing useful and
# breaks the name the producer actually said. Left alone deliberately.


def normalize_entity_name(name: str) -> str:
    """The match key for `name`.

    Collapses: case, surrounding and repeated whitespace, Unicode form,
    Hebrew diacritics, Hebrew final-letter forms, and hyphen/maqaf joining.

    Deliberately does NOT collapse: ט/ת, ו/וו, א/ע or any other
    similar-sounding pair. Those are genuinely different letters that
    distinguish real names, and folding them would merge "טל" with "תל".
    The תבריה/טבריה case they were proposed for is a TRANSCRIPTION error, not
    an orthographic variant — it belongs to fuzzy candidate matching plus
    human confirmation (pg_trgm + names_are_similar), which is where a human
    can say "yes, same place". Silently merging them here would apply the
    same rule to names where it is wrong, with nothing to catch it.
    """
    if not name:
        return ""

    # NFKD first: decomposes so diacritics become separate combining
    # characters that can be dropped, and folds compatibility variants (e.g.
    # presentation forms) onto their plain equivalents.
    text = unicodedata.normalize("NFKD", name)
    # Joiners BEFORE marks. Maqaf is U+05BE, which sits inside the Hebrew
    # block's mark range — an earlier version stripped it as a diacritic and
    # turned "בת־שבע" into "בתשבע" rather than "בת שבע".
    text = _JOINERS.sub(" ", text)
    # Drop combining marks by Unicode CATEGORY rather than a hand-written
    # codepoint range. The range approach was wrong twice over: it swept in
    # Hebrew punctuation (maqaf, paseq, sof pasuq), and it silently ignored
    # diacritics in every other script a name might be written in.
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.casefold()  # casefold, not lower: correct for non-ASCII scripts
    text = "".join(_FINAL_FORMS.get(ch, ch) for ch in text)
    text = _WHITESPACE.sub(" ", text).strip()
    # Recompose so the stored key is a canonical single form — two inputs
    # that differ only by composition must not produce different keys.
    return unicodedata.normalize("NFC", text)


def names_match(a: str, b: str) -> bool:
    """Whether two raw names normalise to the same key — i.e. whether they
    would collide on the unique constraint. Never a fuzzy comparison."""
    return bool(a) and bool(b) and normalize_entity_name(a) == normalize_entity_name(b)
