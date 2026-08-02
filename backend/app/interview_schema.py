"""
Structural validation for the v2 interview question file.

Step 1 of docs/INTERVIEW_RESTRUCTURE.md.

The whole design rests on this file being well-formed: 16 categories, 129
questions and nested gates, edited by hand from here on. At that size a typo
does not raise — it produces a flow that quietly skips a branch, or a
question id that silently stops resolving to a life period. This turns those
into a failing test instead.

`validate` returns (errors, warnings). Errors mean the file is structurally
broken and must not ship. Warnings mean it is well-formed but not finished —
today that is wording the converter wrote rather than the producer.

Deliberately knows nothing about specific categories, gates or option values.
Adding a category, a screening question or a third branch must never require
touching this file; it checks SHAPE, not content.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Tuple

SCHEMA_VERSION = 2

_STEP_KINDS = {"question", "gate"}


def iter_steps(steps: List[Dict[str, Any]], path: str = "") -> Iterator[Tuple[str, dict]]:
    """Every step in the tree, depth-first, with a readable path for errors."""
    for i, step in enumerate(steps):
        here = f"{path}[{i}]"
        yield here, step
        if isinstance(step, dict) and step.get("kind") == "gate":
            for j, opt in enumerate(step.get("options") or []):
                if isinstance(opt, dict):
                    yield from iter_steps(opt.get("steps") or [], f"{here}.options[{j}]")


def count_questions(steps: List[Dict[str, Any]]) -> int:
    return sum(1 for _, s in iter_steps(steps) if s.get("kind") == "question")


def count_gates(steps: List[Dict[str, Any]]) -> int:
    return sum(1 for _, s in iter_steps(steps) if s.get("kind") == "gate")


def validate(doc: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []

    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version is {doc.get('schema_version')!r}, expected {SCHEMA_VERSION}"
        )
        # Everything below assumes the v2 shape; reading on would produce
        # noise rather than information.
        return errors, warnings

    languages = doc.get("languages")
    if not isinstance(languages, dict) or not languages:
        errors.append("languages must be a non-empty object")
        return errors, warnings

    # One id namespace across categories, questions AND gates. They are looked
    # up by different callers but a collision means one of those lookups
    # silently resolves the wrong thing — e.g. the OUTGOING question id
    # 'military_service' is also an incoming CATEGORY id.
    seen_ids: Dict[str, str] = {}

    def claim(kind: str, ident: Any, where: str) -> None:
        if not isinstance(ident, str) or not ident.strip():
            errors.append(f"{where}: {kind} id must be a non-empty string, got {ident!r}")
            return
        if ident in seen_ids:
            errors.append(
                f"{where}: duplicate id {ident!r} (already used by {seen_ids[ident]})"
            )
            return
        seen_ids[ident] = f"{kind} at {where}"

    for lang, block in languages.items():
        categories = (block or {}).get("categories")
        if not isinstance(categories, list) or not categories:
            errors.append(f"languages.{lang}.categories must be a non-empty list")
            continue

        for ci, cat in enumerate(categories):
            cwhere = f"{lang}.categories[{ci}]"
            claim("category", cat.get("id"), cwhere)
            if not (cat.get("name") or "").strip():
                errors.append(f"{cwhere}: name is empty")

            steps = cat.get("steps")
            if not isinstance(steps, list):
                errors.append(f"{cwhere}: steps must be a list")
                continue
            if not steps:
                # Not fatal — a category can legitimately be a placeholder —
                # but it renders as an empty accordion section, so say so.
                warnings.append(f"{cwhere}: category {cat.get('id')!r} has no steps")

            for spath, step in iter_steps(steps, f"{cwhere}.steps"):
                if not isinstance(step, dict):
                    errors.append(f"{spath}: step must be an object")
                    continue
                kind = step.get("kind")
                if kind not in _STEP_KINDS:
                    errors.append(f"{spath}: unknown step kind {kind!r}")
                    continue

                claim(kind, step.get("id"), spath)

                if not (step.get("text") or "").strip():
                    errors.append(f"{spath}: text is empty")

                if step.get("needs_wording_confirmation"):
                    warnings.append(
                        f"{spath}: {step.get('id')!r} carries converter-written wording "
                        f"awaiting producer confirmation"
                    )

                if kind != "gate":
                    continue

                options = step.get("options")
                if not isinstance(options, list) or len(options) < 2:
                    # A one-option gate is not a choice; it is a question that
                    # cannot be answered any other way, and almost certainly a
                    # truncated edit.
                    errors.append(
                        f"{spath}: gate needs at least 2 options, got "
                        f"{len(options) if isinstance(options, list) else options!r}"
                    )
                    continue

                values = []
                for oi, opt in enumerate(options):
                    owhere = f"{spath}.options[{oi}]"
                    if not isinstance(opt, dict):
                        errors.append(f"{owhere}: option must be an object")
                        continue
                    if not (opt.get("value") or "").strip():
                        errors.append(f"{owhere}: value is empty")
                    else:
                        values.append(opt["value"])
                    if not (opt.get("label") or "").strip():
                        errors.append(f"{owhere}: label is empty")
                    # An option with no `steps` key is ambiguous between "ends
                    # here" and "unfinished". Require the key; [] is the
                    # explicit, meaningful "ends here" (e.g. 'together').
                    if not isinstance(opt.get("steps"), list):
                        errors.append(f"{owhere}: steps must be a list (use [] to end the branch)")

                dupes = {v for v in values if values.count(v) > 1}
                if dupes:
                    errors.append(f"{spath}: duplicate option values {sorted(dupes)}")

    # Retired questions are lookup-only history. Their ids are FIXED — they
    # are the values already stored in raw_segments.question_id — so they
    # cannot be renamed to avoid a clash, and the rule has to be about which
    # clashes actually mislead a lookup.
    #
    # Against a live QUESTION or GATE id: an error. category_for_question_id
    # searches live steps first and then retired, so one id meaning two
    # different questions silently resolves to whichever is found first.
    #
    # Against a CATEGORY id: a warning only. Questions and categories are
    # looked up in different indexes and never compete, so nothing resolves
    # wrongly — but it reads as a bug to a human, and it is real here: the
    # outgoing question id 'military_service' is also an incoming category id.
    for ri, item in enumerate(doc.get("retired") or []):
        rwhere = f"retired[{ri}]"
        rid = item.get("id")
        if not isinstance(rid, str) or not rid.strip():
            errors.append(f"{rwhere}: id must be a non-empty string")
            continue
        clash = seen_ids.get(rid)
        if clash and clash.startswith("category"):
            warnings.append(
                f"{rwhere}: retired question id {rid!r} is also a live {clash}. "
                f"Harmless — they are looked up in different indexes — but confusing to read."
            )
        elif clash:
            errors.append(
                f"{rwhere}: retired id {rid!r} collides with a live {clash} — "
                f"category_for_question_id would resolve the wrong one"
            )
        if not (item.get("category") or "").strip():
            errors.append(f"{rwhere}: retired question {rid!r} has no category to resolve to")

    # meta is documentation, and documentation that drifts is worse than none.
    meta = doc.get("meta") or {}
    for lang, block in languages.items():
        cats = (block or {}).get("categories") or []
        if not isinstance(cats, list):
            continue
        actual_q = sum(count_questions(c.get("steps") or []) for c in cats)
        actual_g = sum(count_gates(c.get("steps") or []) for c in cats)
        if "total_questions" in meta and meta["total_questions"] != actual_q:
            errors.append(
                f"meta.total_questions is {meta['total_questions']} but {lang} has {actual_q}"
            )
        if "total_gates" in meta and meta["total_gates"] != actual_g:
            errors.append(f"meta.total_gates is {meta['total_gates']} but {lang} has {actual_g}")
        if "total_categories" in meta and meta["total_categories"] != len(cats):
            errors.append(
                f"meta.total_categories is {meta['total_categories']} but {lang} has {len(cats)}"
            )
        break  # counts describe the content, which is one set across languages

    return errors, warnings
