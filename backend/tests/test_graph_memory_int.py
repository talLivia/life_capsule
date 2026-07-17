"""
Integration tests for app/services/graph_memory.py (Prompt 3).

Real Neo4j + a real extraction LLM + real embeddings — these hit live
external services (Gemini by default; Anthropic/OpenAI if
GRAPHITI_LLM_PROVIDER/GRAPHITI_EMBEDDER_PROVIDER are set to that instead)
so they're gated behind the `integration` marker (see pytest.ini) and
skipped unless Neo4j is configured and whichever LLM/embedder provider is
selected has its credentials present. Run explicitly with:

    pytest -m integration tests/test_graph_memory_int.py

Ingests 3 sample Hebrew life-story segments, two of which share an entity
("גילה" / Gila — a fellow soldier in one segment, later the narrator's
spouse in another) and one that shares nothing with the other two, then
verifies find_related_episodes and get_entity_candidates behave
correctly against the real graph. Every test run uses its own throwaway
group_id and cleans up after itself via clear_data — safe to run
repeatedly against a real Neo4j instance without accumulating garbage.
"""

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest

from app.config import settings

pytestmark = pytest.mark.integration

_REQUIRED_ENV = ["NEO4J_URI", "NEO4J_PASSWORD"]
if settings.GRAPHITI_LLM_PROVIDER == "anthropic":
    _REQUIRED_ENV.append("ANTHROPIC_API_KEY")
elif settings.GRAPHITI_LLM_PROVIDER == "gemini":
    _REQUIRED_ENV.append("GEMINI_API_KEY")
if settings.GRAPHITI_EMBEDDER_PROVIDER == "openai":
    _REQUIRED_ENV.append("OPENAI_API_KEY")
elif settings.GRAPHITI_EMBEDDER_PROVIDER == "gemini":
    _REQUIRED_ENV.append("GEMINI_API_KEY")
_missing = [v for v in _REQUIRED_ENV if not os.getenv(v)]
if _missing:
    pytest.skip(
        f"graph_memory integration tests need {_missing} set — skipping",
        allow_module_level=True,
    )

from graphiti_core.utils.maintenance.graph_data_operations import clear_data  # noqa: E402

from app.services import graph_memory as gm  # noqa: E402

# Three sample segments: SEG_ARMY and SEG_MARRIAGE share the entity "גילה"
# (Gila); SEG_CAREER shares nothing with either.
SEG_ARMY = (
    "seg-army",
    "שירתתי בצבא במשך שלוש שנים. הכרתי שם את גילה, שהייתה מפקדת הכיתה שלי.",
    ["military service"],
    datetime(2005, 3, 1, tzinfo=timezone.utc),
)
SEG_MARRIAGE = (
    "seg-marriage",
    "כעבור כמה שנים התחתנתי עם גילה. היא הפכה לאשתי הנפלאה.",
    ["relationships"],
    datetime(2010, 6, 15, tzinfo=timezone.utc),
)
SEG_CAREER = (
    "seg-career",
    "התחלתי לעבוד כמהנדס בחברת הייטק בתל אביב. המנהל שלי היה דן כהן.",
    ["career"],
    datetime(2012, 1, 10, tzinfo=timezone.utc),
)


@pytest.fixture(autouse=True)
async def _fresh_graphiti_client():
    """
    pytest-asyncio (asyncio_mode=auto, no loop_scope override in
    pytest.ini) gives each test function its own event loop. graph_memory's
    get_graphiti() caches a single Graphiti client at module scope, so
    without this fixture the client — and its underlying Neo4j async
    driver/socket — stays bound to whichever test's event loop created it
    first, and every later test fails with "Future attached to a different
    loop". Reset before AND after each test so nothing outlives its loop.
    """
    await gm.reset_graphiti_client()
    yield
    await gm.reset_graphiti_client()


@pytest.fixture
async def group_id():
    """A throwaway group_id so this run's data can't collide with real
    archives or other concurrent test runs, and is fully cleaned up after."""
    gid = f"test-graph-memory-{uuid.uuid4()}"
    await gm.get_graphiti().build_indices_and_constraints()
    yield gid
    driver = gm.get_graphiti().driver
    await clear_data(driver, group_ids=[gid])


@pytest.mark.asyncio
async def test_shared_entity_retrieval_end_to_end(group_id):
    """
    All assertions run against a single ingestion pass, not one per test.

    Originally 6 separate tests each re-ingested all 3 segments (18 total
    add_episode calls). Real cloud LLM providers' free tiers rate-limit
    hard enough (Gemini's free tier: 5 generate_content requests/minute
    per model, and a single add_episode makes several — extraction, dedup,
    edge extraction) that even 3 back-to-back add_episode calls reliably
    tripped RESOURCE_EXHAUSTED — discovered by actually running this
    against live Gemini, not a hypothetical concern. Ingesting once (not
    per test) plus pacing the 3 calls below keeps this under the cap;
    find_related_episodes/get_entity_candidates only need embeddings + RRF
    (pure math, no generation), so they aren't paced.
    """
    for i, (segment_id, transcript, topics, ts) in enumerate((SEG_ARMY, SEG_MARRIAGE, SEG_CAREER)):
        if i > 0:
            await asyncio.sleep(20)
        await gm.add_episode(
            segment_id=segment_id,
            transcript=transcript,
            topic_tags=topics,
            timestamp=ts,
            group_id=group_id,
        )

    # Shared entity (גילה) links seg-army -> seg-marriage, excluding the
    # segment we started from and not pulling in the unrelated seg-career.
    related = await gm.find_related_episodes(
        entity_names=["גילה"], exclude_ids=["seg-army"], group_id=group_id
    )
    assert "seg-marriage" in related
    assert "seg-army" not in related
    assert "seg-career" not in related

    # A different entity (דן כהן, only in seg-career) doesn't pull in
    # either of the unrelated segments.
    related_career = await gm.find_related_episodes(
        entity_names=["דן כהן"], exclude_ids=["seg-career"], group_id=group_id
    )
    assert "seg-army" not in related_career
    assert "seg-marriage" not in related_career

    # exclude_ids actually filters — without it, seg-marriage IS reachable
    # from גילה; with it (simulating "already surfaced this conversation"),
    # it's removed from the results.
    related_unfiltered = await gm.find_related_episodes(
        entity_names=["גילה"], exclude_ids=[], group_id=group_id
    )
    assert "seg-marriage" in related_unfiltered
    related_filtered = await gm.find_related_episodes(
        entity_names=["גילה"], exclude_ids=["seg-marriage"], group_id=group_id
    )
    assert "seg-marriage" not in related_filtered

    # An entity that was never mentioned anywhere returns nothing.
    related_unknown = await gm.find_related_episodes(
        entity_names=["שם שלא קיים בכלל"], exclude_ids=[], group_id=group_id
    )
    assert related_unknown == []

    # get_entity_candidates fuzzy-matches a known entity...
    candidates = await gm.get_entity_candidates("גילה", group_id=group_id)
    assert len(candidates) >= 1
    assert any("גילה" in c["name"] for c in candidates)
    for c in candidates:
        assert set(c.keys()) == {"uuid", "name", "summary"}

    # ...and doesn't invent an exact match for a name that was never
    # mentioned.
    candidates_unknown = await gm.get_entity_candidates(
        "קסם מוחלט שלא קיים", group_id=group_id
    )
    assert not any(c["name"].strip() == "קסם מוחלט שלא קיים" for c in candidates_unknown)
