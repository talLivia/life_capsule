"""
Phase 1b of docs/FAMILY_TREE_TIMELINE.md — the stable question id.

`question_index` is positional. Insert a question near the front of
interview_questions.json and every later index points at a DIFFERENT question,
so anything deriving a life period from the index silently refiles historical
recordings under the wrong milestone. No exception, no error — a wrong tree.

These pin the two things that prevent that: category is derived from the stable
`id`, and the category list itself is read from the JSON rather than held
anywhere in code.
"""

import json
from pathlib import Path

import pytest

from app import interview_config

V2_PATH = Path(__file__).resolve().parent.parent / "app" / "interview_questions.json"


@pytest.fixture
def reordered_catalog(monkeypatch):
    """Simulate a question-set edit: a new category inserted at the FRONT,
    shifting every later question's position.

    Built as a v2 document, since that is now the live format. The property
    under test is unchanged and is the whole reason question_id exists —
    positions move, ids do not.
    """
    original = interview_config._load_all()
    existing = original["languages"]["he"]["categories"]
    catalog = {
        "schema_version": 2,
        "languages": {
            "he": {
                "categories": [
                    {
                        "id": "origins",
                        "name": "מוצא",
                        "steps": [
                            {"kind": "question", "id": "new_birth", "text": "איפה נולדת?"},
                            {"kind": "question", "id": "new_parents",
                             "text": "מי היו ההורים שלך?"},
                        ],
                    },
                    *existing,
                ]
            }
        },
        "retired": original.get("retired", []),
    }
    # Several views memoise over _load_all, so swapping the catalog without
    # clearing them all would test the OLD question set. interview_config
    # exposes one helper for exactly this, so a new cache cannot be forgotten
    # here.
    monkeypatch.setattr(interview_config, "_load_all", lambda: catalog)
    interview_config.cache_clear()
    yield catalog
    # monkeypatch restores _load_all after this, but the derived caches are
    # ours to reset or every later test sees the reordered set.
    interview_config.cache_clear()


def test_category_survives_a_reordered_question_set(reordered_catalog):
    """THE test this column exists for.

    A question keeps its category after a whole new category is inserted ahead
    of it, even though its POSITION has moved — which is why nothing may derive
    a life period from question_index.
    """
    probe = "military_service_q01"
    assert interview_config.category_for_question_id(probe) == "military_service"

    ids = [q["id"] for q in interview_config.get_questions("he")]
    # the fixture must actually shift things, or this proves nothing
    assert ids.index(probe) >= 2
    # and whatever now sits at the positions the new category took is NOT it
    assert ids[0] == "new_birth" and ids[1] == "new_parents"


def test_categories_are_derived_from_the_file_not_a_constant(reordered_catalog):
    """A new category added to the JSON must appear with no code change."""
    cats = [c["category"] for c in interview_config.get_categories("he")]
    assert cats[0] == "origins", "a category added to the file must appear first"
    assert "military_service" in cats
    # order is first appearance in the file — that IS the chronology
    assert cats.index("origins") < cats.index("childhood")


def test_category_count_is_not_fixed(reordered_catalog):
    """Nothing may assume a fixed number of categories.

    Asserted RELATIVE to the real set rather than against a literal, so this
    keeps testing the property after the next content edit instead of becoming
    a number that has to be bumped.
    """
    with_extra = len(interview_config.get_categories("he"))
    interview_config.cache_clear()
    monkey_free = len(json.load(open(V2_PATH, encoding="utf-8"))["languages"]["he"]["categories"])
    assert with_extra == monkey_free + 1


def test_get_categories_groups_every_question_exactly_once():
    total = sum(len(c["question_ids"]) for c in interview_config.get_categories("he"))
    assert total == len(interview_config.get_questions("he"))
    seen = [qid for c in interview_config.get_categories("he") for qid in c["question_ids"]]
    assert len(seen) == len(set(seen))


def test_question_ids_recover_from_verbatim_text():
    """What the backfill relies on, and the reason it is deadline-bound: this
    works only while the JSON still holds the wording recordings were made
    with."""
    for lang in ("he", "en"):
        for q in interview_config.get_questions(lang):
            assert interview_config.question_id_for_text(q["text"]) == q["id"]
            # leading/trailing whitespace must not defeat it
            assert interview_config.question_id_for_text(f"  {q['text']}  ") == q["id"]


def test_unknown_text_and_ids_are_rejected_rather_than_guessed():
    assert interview_config.question_id_for_text("something nobody asked") is None
    assert interview_config.question_id_for_text("") is None
    assert interview_config.category_for_question_id("not_a_question") is None
    assert interview_config.is_valid_question_id("childhood_q01") is True
    assert interview_config.is_valid_question_id("brother-ish") is False
    # A RETIRED question is deliberately not "valid" — it must never be
    # offered to a producer — but it must still resolve for history.
    assert interview_config.is_valid_question_id("childhood_home") is False
    assert interview_config.category_for_question_id("childhood_home") == "childhood"


# ── ingest ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_stores_a_valid_question_id(client, test_user, auth_headers):
    from app.interview_config import get_questions

    q = get_questions("he")[3]
    resp = await client.post(
        "/api/v1/interview/segments/ingest",
        json={
            "interview_session_id": await _session_id(client, auth_headers),
            "question_index": 3,
            "question_id": q["id"],
            "question_asked": q["text"],
            "video_key": f"segments/{test_user.id}/y/3/z.webm",
        },
        headers=auth_headers,
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json().get("question_id", q["id"]) == q["id"]


@pytest.mark.asyncio
async def test_ingest_recovers_the_id_when_the_client_sends_none(
    client, test_user, auth_headers, db_session
):
    """An older client sends no question_id. The endpoint must recover it from
    the question text rather than storing NULL."""
    from sqlalchemy import select

    from app.interview_config import get_questions
    from app.models import RawSegment

    q = get_questions("he")[0]
    resp = await client.post(
        "/api/v1/interview/segments/ingest",
        json={
            "interview_session_id": await _session_id(client, auth_headers),
            "question_index": 0,
            "question_asked": q["text"],
            "video_key": f"segments/{test_user.id}/y/0/recovered.webm",
        },
        headers=auth_headers,
    )
    assert resp.status_code in (200, 201), resp.text

    seg = (
        await db_session.execute(
            select(RawSegment).where(RawSegment.video_key == f"segments/{test_user.id}/y/0/recovered.webm")
        )
    ).scalar_one()
    assert seg.question_id == q["id"]


@pytest.mark.asyncio
async def test_ingest_drops_an_invented_question_id(
    client, test_user, auth_headers, db_session
):
    """A client cannot invent an id. Storing an unresolvable value would be
    worse than NULL — it looks attributed and is not."""
    from sqlalchemy import select

    from app.models import RawSegment

    resp = await client.post(
        "/api/v1/interview/segments/ingest",
        json={
            "interview_session_id": await _session_id(client, auth_headers),
            "question_index": 0,
            "question_id": "totally_made_up",
            "question_asked": "a question that is not in the guided set",
            "video_key": f"segments/{test_user.id}/y/0/invented.webm",
        },
        headers=auth_headers,
    )
    assert resp.status_code in (200, 201), resp.text

    seg = (
        await db_session.execute(
            select(RawSegment).where(RawSegment.video_key == f"segments/{test_user.id}/y/0/invented.webm")
        )
    ).scalar_one()
    assert seg.question_id is None


async def _session_id(client, headers) -> str:
    resp = await client.get("/api/v1/interview/session", headers=headers)
    return resp.json()["session"]["id"]
