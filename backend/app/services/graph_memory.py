"""
Graphiti wrapper — the associative-memory / knowledge-graph layer.

Graphiti (github.com/getzep/graphiti) models story segments as temporal
episodes, automatically extracting entities and relationships from their
text via an LLM (Gemini by default, Claude as the production-quality
option — see `_build_graphiti` below) and storing everything in Neo4j
(AuraDB in production; see `neo4j_client.py` for the plain-driver
connectivity check this builds on top of).

Three public functions, matching Prompt 3:
  - `add_episode`            — ingest a segment, let Graphiti extract entities.
  - `find_related_episodes`  — 1-hop graph expansion from known entities.
  - `get_entity_candidates`  — fuzzy name match, for Prompt 5's human-in-
    the-loop disambiguation ("is this the same Gila mentioned before?").

None of these three ever fabricate story content — extraction produces
structured facts (who/what/when) grounded in the transcript text Graphiti
was given; retrieval only ever returns segment ids and existing node
metadata, never generated narrative. The actual verbatim-only response
guarantee lives downstream in response_assembler.py (Prompt 8).

segment_id <-> Graphiti episode uuid: Graphiti generates its own episode
uuid at creation time (add_episode's own `uuid` param is for re-processing
an *existing* episode, not assigning one to a new node — confirmed by
actually running this against a live graph, not from docs). segment_id is
instead embedded in the episode's `name` ("segment-{segment_id}"), and
find_related_episodes resolves back through that convention.

group_id partitioning (addition beyond the literal Prompt 3 spec): every
function takes a `group_id`, defaulting to DEFAULT_GROUP_ID for a
single-archive POC. Multiple producers' story archives must NOT share a
group_id in a shared Neo4j instance — each storyteller's episodes should
be tagged with their own id (e.g. their user_id or avatar_id) once
Prompt 4/5 wire this up for real, so family member A's /talk session can
never surface family member B's stories. Flagging this now since Prompt 9
explicitly describes /talk as "scoped to that person's archive."
"""

from __future__ import annotations

import logging
from datetime import datetime
from difflib import SequenceMatcher
from typing import Optional

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.graphiti import AddEpisodeResults
from graphiti_core.llm_client.anthropic_client import AnthropicClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.gemini_client import GeminiClient
from graphiti_core.nodes import EntityNode, EpisodeType
from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF

from app.config import settings

logger = logging.getLogger(__name__)

# Default partition when no explicit group_id is supplied — fine for a
# single-storyteller POC; see module docstring for why real multi-producer
# deployments must pass their own group_id.
DEFAULT_GROUP_ID = "default"


def _build_graphiti() -> Graphiti:
    """
    Graphiti's OWN internal LLM (entity/relationship extraction) and
    embedder — configured independently of the main app's LLM facade
    (services/llm.py, used for topic classification/importance scoring in
    Prompts 5-7). Never a general-chat call either way — see llm.py's
    module docstring for why that distinction matters project-wide.

    The project plan specifies Claude for extraction; GRAPHITI_LLM_PROVIDER
    also accepts "gemini" (fully cloud-based, cheaper for iteration).
    Extraction quality is generally weaker than Claude's — swap
    GRAPHITI_LLM_PROVIDER back to "anthropic" before relying on real
    ingestion quality.

    Embeddings: Anthropic has no embeddings API, so a second provider is
    always needed here regardless of GRAPHITI_LLM_PROVIDER. "gemini" (the
    default) covers both LLM and embeddings with a single GEMINI_API_KEY;
    "openai" is the alternative for production-quality embeddings — that
    path requires OPENAI_API_KEY even when GRAPHITI_LLM_PROVIDER=anthropic,
    used purely for vector embeddings, never for generating any text a
    family member could see.
    """
    if settings.GRAPHITI_LLM_PROVIDER == "gemini":
        gemini_config = LLMConfig(
            api_key=settings.GEMINI_API_KEY,
            model=settings.GRAPHITI_LLM_MODEL,
            small_model=settings.GRAPHITI_LLM_SMALL_MODEL,
        )
        llm_client = GeminiClient(config=gemini_config)
        # Graphiti also reranks search results via a cross-encoder, which
        # defaults to OpenAIRerankerClient() (reading OPENAI_API_KEY) if
        # left unset — use Gemini's reranker instead so this path needs
        # only GEMINI_API_KEY, no OpenAI key at all.
        cross_encoder = GeminiRerankerClient(config=gemini_config)
    else:
        llm_client = AnthropicClient(
            config=LLMConfig(api_key=settings.ANTHROPIC_API_KEY, model=settings.LLM_MODEL)
        )
        # Pass this explicitly: Graphiti's default OpenAIRerankerClient()
        # reads the raw OPENAI_API_KEY *process* env var directly, not our
        # settings.OPENAI_API_KEY (pydantic-settings' env_file loading
        # doesn't export into os.environ) — leaving it implicit would
        # silently break the moment those two diverge.
        cross_encoder = OpenAIRerankerClient(config=LLMConfig(api_key=settings.OPENAI_API_KEY))

    if settings.GRAPHITI_EMBEDDER_PROVIDER == "gemini":
        embedder = GeminiEmbedder(
            config=GeminiEmbedderConfig(
                api_key=settings.GEMINI_API_KEY,
                embedding_model=settings.GRAPHITI_EMBEDDING_MODEL,
                embedding_dim=settings.GRAPHITI_EMBEDDING_DIM,
            )
        )
    else:
        embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(api_key=settings.OPENAI_API_KEY))

    # Built explicitly (rather than passing uri/user/password straight to
    # Graphiti) so a non-default database name is respected — some AuraDB
    # instances use one matching their instance id instead of "neo4j".
    driver = Neo4jDriver(
        uri=settings.NEO4J_URI,
        user=settings.NEO4J_USER,
        password=settings.NEO4J_PASSWORD,
        database=settings.NEO4J_DATABASE,
    )
    return Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
    )


_graphiti: Optional[Graphiti] = None


def get_graphiti() -> Graphiti:
    """Lazy singleton — avoids opening a Neo4j connection at import time
    (e.g. during unrelated unit tests that never touch the graph)."""
    global _graphiti
    if _graphiti is None:
        _graphiti = _build_graphiti()
    return _graphiti


async def reset_graphiti_client() -> None:
    """Test hook — drop the cached client so the next get_graphiti() call
    rebuilds it (e.g. against a freshly-monkeypatched settings.NEO4J_URI)."""
    global _graphiti
    if _graphiti is not None:
        await _graphiti.close()
    _graphiti = None


async def init_graph_schema() -> None:
    """
    One-time index/constraint setup. Safe to call repeatedly
    (delete_existing=False) — intended for a deploy-time step or the
    first real ingestion pipeline run (Prompt 5), not the hot path.
    """
    await get_graphiti().build_indices_and_constraints()


async def episode_uuids_for_segment(
    segment_id: str, group_id: str = DEFAULT_GROUP_ID
) -> list[str]:
    """Every episode uuid recorded for this segment.

    Returns a LIST, not one uuid: a segment could accumulate several episodes
    before add_episode started replacing them (see its comment), and any
    cleanup has to remove all of them rather than the first match."""
    graphiti = get_graphiti()
    query = """
        MATCH (e:Episodic {name: $name})
        WHERE e.group_id = $group_id
        RETURN e.uuid AS uuid
    """
    result = await graphiti.driver.execute_query(
        query, name=f"segment-{segment_id}", group_id=group_id, routing_="r"
    )
    return [r["uuid"] for r in result.records]


async def remove_episodes_for_segment(
    segment_id: str, group_id: str = DEFAULT_GROUP_ID
) -> int:
    """Delete this segment's episode(s) and everything that existed only
    because of them. Returns how many episodes were removed.

    Graphiti's remove_episode does the careful part: it drops only edges whose
    FIRST source episode is this one, and only entity nodes whose MENTIONS
    count is 1 — so an entity another recording still references survives
    (verified against real data: a place mentioned by two recordings is kept).

    VERIFIED, NOT ASSUMED: afterwards we re-query for episodes with this
    segment's name and log loudly if any remain. Graphiti's cleanup depends on
    its own bookkeeping (an edge whose episodes[0] doesn't point here would
    survive as an orphan), and this archive has zero RELATES_TO edges, so that
    path is effectively untested here. Silent orphans are exactly what this
    whole change exists to prevent."""
    uuids = await episode_uuids_for_segment(segment_id, group_id=group_id)
    if not uuids:
        return 0

    graphiti = get_graphiti()
    removed = 0
    for uuid in uuids:
        try:
            await graphiti.remove_episode(uuid)
            removed += 1
        except Exception as e:
            logger.error(
                "graphiti_episode_remove_failed",
                extra={"segment_id": segment_id, "episode_uuid": uuid, "error": str(e)},
            )

    leftover = await episode_uuids_for_segment(segment_id, group_id=group_id)
    if leftover:
        logger.error(
            "graphiti_episode_remove_incomplete",
            extra={
                "segment_id": segment_id,
                "group_id": group_id,
                "remaining": leftover,
                "detail": "episodes still present after removal — orphaned graph data",
            },
        )
    else:
        logger.info(
            "graphiti_episodes_removed",
            extra={"segment_id": segment_id, "group_id": group_id, "count": removed},
        )
    return removed


async def add_episode(
    segment_id: str,
    transcript: str,
    topic_tags: list[str],
    timestamp: datetime,
    group_id: str = DEFAULT_GROUP_ID,
    custom_extraction_instructions: Optional[str] = None,
) -> AddEpisodeResults:
    """
    Ingest a story segment into Graphiti as an episode. Graphiti's own LLM
    extraction (Claude) pulls entities and relationships out of the
    transcript automatically — we don't hand-parse anything here.

    `custom_extraction_instructions` is Prompt 5's human-in-the-loop entity
    resolution hook: once a human confirms (or rejects) that a name in this
    segment is the same real-world entity as one already in the graph,
    analysis_graph.py's finalize_ingest node builds a natural-language
    instruction here (e.g. "treat 'Gila' as entity <uuid>, don't duplicate
    it"). This is Graphiti's *public* add_episode parameter for steering its
    own extraction/dedup — not a private API — so the human's answer has a
    real effect on the resulting graph rather than being purely cosmetic.

    NOTE: Graphiti's `add_episode(uuid=...)` param is for RE-processing an
    already-existing episode (it fetches-by-uuid internally and raises
    NodeNotFoundError if nothing exists yet) — it can't be used to assign
    a custom id to a brand-new episode. So the episode gets Graphiti's own
    generated uuid; `segment_id` is instead embedded in the episode's
    `name` field ("segment-{segment_id}"), which find_related_episodes
    resolves back through. This was discovered by actually running the
    integration test against a live graph, not from documentation.
    """
    graphiti = get_graphiti()

    # Re-recording a question must REPLACE its episode, not add a second one.
    # Graphiti always mints a fresh uuid (see the NOTE above), so without this
    # a re-ingest left the old episode in place and the segment's transcript
    # was counted TWICE by entity extraction. Confirmed live: this archive had
    # 13 episodes for 12 segments, with segment-ab5f6318 present twice.
    removed = await remove_episodes_for_segment(segment_id, group_id=group_id)
    if removed:
        logger.info(
            "graphiti_episode_replaced",
            extra={"segment_id": segment_id, "group_id": group_id, "removed": removed},
        )

    source_description = (
        f"life-story segment — topics: {', '.join(topic_tags)}"
        if topic_tags
        else "life-story segment"
    )
    result = await graphiti.add_episode(
        name=f"segment-{segment_id}",
        episode_body=transcript,
        source_description=source_description,
        reference_time=timestamp,
        source=EpisodeType.text,
        group_id=group_id,
        custom_extraction_instructions=custom_extraction_instructions,
    )
    logger.info(
        "graphiti_episode_added",
        extra={
            "segment_id": segment_id,
            "group_id": group_id,
            "nodes_extracted": len(result.nodes),
            "edges_extracted": len(result.edges),
        },
    )
    return result


async def find_related_episodes(
    entity_names: list[str],
    exclude_ids: list[str],
    max_hops: int = 1,
    group_id: str = DEFAULT_GROUP_ID,
    limit: int = 10,
) -> list[str]:
    """
    Find segment ids sharing any of `entity_names`, within `max_hops` of
    those entities, excluding `exclude_ids` (the session's visited-set —
    see cache.py's add_visited/get_visited from Prompt 2).

    Returns a deduplicated list of segment_ids only — never transcript
    content. Callers needing the actual text look it up from Postgres by
    id (Prompt 6's retrieval pipeline does exactly this), keeping this
    graph query cheap, side-effect-free, and impossible to accidentally
    return unvetted text through.

    Entity names are matched exactly (case-insensitive) against existing
    node names before traversal starts — fuzzy matching is deliberately
    `get_entity_candidates`'s job (used at ingestion time, with a human in
    the loop), not something retrieval should do silently at query time.

    IMPLEMENTATION NOTE (found by running this against live-extracted
    data, not from docs): this queries Graphiti's MENTIONS relationship
    (Episodic -> Entity) directly via Cypher, not the search_()/EntityEdge
    API. A first attempt used EdgeSearchMethod.bfs over RELATES_TO
    (entity-to-entity fact) edges and their `.episodes` field — but a
    segment that names only one entity produces zero RELATES_TO edges
    (there's no second entity to relate it to), so an edge-only search
    misses exactly the "which episodes mention this entity" case this
    function exists to answer. max_hops=1 means "episodes directly
    mentioning the origin entities"; each additional hop follows one more
    RELATES_TO step through the entity graph before collecting mentioning
    episodes, so an entity related-but-never-co-mentioned can still surface.
    """
    if not entity_names or max_hops < 1:
        return []

    graphiti = get_graphiti()

    origin_uuids: list[str] = []
    for name in entity_names:
        nodes = await _search_nodes(name, group_id=group_id, limit=5)
        origin_uuids.extend(
            n.uuid for n in nodes if n.name.strip().lower() == name.strip().lower()
        )

    if not origin_uuids:
        return []

    # max_hops-1 extra RELATES_TO steps from the origin entities before
    # collecting MENTIONS. The bound is interpolated (not parameterized —
    # Cypher variable-length patterns don't accept parameters for the
    # range) but max_hops is an internal int, never raw user input.
    extra_hops = max_hops - 1
    query = f"""
        MATCH (origin:Entity)
        WHERE origin.uuid IN $origin_uuids AND origin.group_id = $group_id
        MATCH (origin)-[:RELATES_TO*0..{extra_hops}]-(related:Entity)
        MATCH (related)<-[:MENTIONS]-(ep:Episodic)
        WHERE ep.group_id = $group_id
        RETURN DISTINCT ep.name AS name
        LIMIT $limit
    """
    result = await graphiti.driver.execute_query(
        query, origin_uuids=origin_uuids, group_id=group_id, limit=limit * 4
    )

    # Episode names carry the "segment-{segment_id}" convention set in
    # add_episode — resolve back to our own segment_ids.
    excluded = set(exclude_ids)
    segment_ids: list[str] = []
    seen: set[str] = set()
    for record in result.records:
        segment_id = record["name"].removeprefix("segment-")
        if segment_id in excluded or segment_id in seen:
            continue
        seen.add(segment_id)
        segment_ids.append(segment_id)

    return segment_ids[:limit]


async def get_episode_entity_names(
    segment_id: str, group_id: str = DEFAULT_GROUP_ID
) -> list[str]:
    """
    Entity names Graphiti recorded as mentioned by a specific segment's
    episode — the bridge Prompt 6's retrieval pipeline needs between
    `primary_match`'s Postgres segment_ids and `expand_graph`'s entity-based
    graph traversal (the doc's "entities from the primary segment(s)").
    Never returns transcript content, only entity names already public via
    get_entity_candidates.
    """
    graphiti = get_graphiti()
    query = """
        MATCH (ep:Episodic {name: $name})-[:MENTIONS]->(e:Entity)
        WHERE ep.group_id = $group_id
        RETURN DISTINCT e.name AS name
    """
    result = await graphiti.driver.execute_query(
        query, name=f"segment-{segment_id}", group_id=group_id
    )
    return [record["name"] for record in result.records]


async def find_related_episodes_scored(
    entity_names: list[str],
    exclude_ids: list[str],
    max_hops: int = 1,
    group_id: str = DEFAULT_GROUP_ID,
    limit: int = 10,
) -> list[dict]:
    """
    Like `find_related_episodes`, but also reports how many of the origin
    entities each candidate episode shares — Prompt 6's retrieval pipeline
    uses this count as its "edge-weight/confidence" proxy. Graphiti's
    MENTIONS-based expansion (see `find_related_episodes`'s docstring for
    why it goes through MENTIONS rather than RELATES_TO) doesn't carry a
    numeric edge weight the way a single fact-edge would, so "how many of
    the entities this conversation cares about does this episode actually
    mention" is the honest, computable substitute rather than a fabricated
    score.

    Returns [{"segment_id": str, "shared_entity_count": int}, ...], sorted
    by shared_entity_count descending. Never returns transcript content.
    """
    if not entity_names or max_hops < 1:
        return []

    graphiti = get_graphiti()

    origin_uuids: list[str] = []
    for name in entity_names:
        nodes = await _search_nodes(name, group_id=group_id, limit=5)
        origin_uuids.extend(
            n.uuid for n in nodes if n.name.strip().lower() == name.strip().lower()
        )

    if not origin_uuids:
        return []

    extra_hops = max_hops - 1
    query = f"""
        MATCH (origin:Entity)
        WHERE origin.uuid IN $origin_uuids AND origin.group_id = $group_id
        MATCH (origin)-[:RELATES_TO*0..{extra_hops}]-(related:Entity)
        MATCH (related)<-[:MENTIONS]-(ep:Episodic)
        WHERE ep.group_id = $group_id
        WITH ep, collect(DISTINCT origin.uuid) AS matched_origins
        RETURN ep.name AS name, size(matched_origins) AS shared_entity_count
        ORDER BY shared_entity_count DESC
        LIMIT $limit
    """
    result = await graphiti.driver.execute_query(
        query, origin_uuids=origin_uuids, group_id=group_id, limit=limit * 4
    )

    excluded = set(exclude_ids)
    seen: set[str] = set()
    scored: list[dict] = []
    for record in result.records:
        segment_id = record["name"].removeprefix("segment-")
        if segment_id in excluded or segment_id in seen:
            continue
        seen.add(segment_id)
        scored.append(
            {"segment_id": segment_id, "shared_entity_count": record["shared_entity_count"]}
        )

    return scored[:limit]


async def get_entity_candidates(
    name: str, group_id: str = DEFAULT_GROUP_ID, limit: int = 5
) -> list[dict]:
    """
    Fuzzy-match existing graph entities against `name` — backs Prompt 5's
    human-in-the-loop disambiguation step ("is this the same Gila
    mentioned before?").

    Returns candidates ranked by relevance, each as
    {"uuid", "name", "summary"} — deliberately NOT a single "best" match.
    The decision to treat a candidate as the same real-world entity is
    always made explicitly (a human confirming via the LangGraph
    interrupt() in Prompt 5), never inferred silently here.
    """
    nodes = await _search_nodes(name, group_id=group_id, limit=limit)
    return [{"uuid": n.uuid, "name": n.name, "summary": n.summary} for n in nodes]


_TOKEN_SIMILARITY_THRESHOLD = 0.75


def names_are_similar(a: str, b: str) -> bool:
    """
    Lexical-similarity gate shared by two callers that both need "is this
    candidate node plausibly the same real-world entity as this name" -
    analysis_graph.py's check_entities_node (Prompt 5, ingestion-time
    disambiguation) and retrieval_service.py's primary_match (Prompt 6/10,
    resolving a name mentioned in a live question against the graph's
    canonical node names).

    Deliberately token-aware rather than a single whole-string similarity
    ratio: comparing full strings character-by-character rewards a shared
    surname as heavily as a shared full name — confirmed live that "גילה
    כהן" (Gila Cohen) vs "דן כהן" (Dan Cohen) scores *higher* (0.57) via
    SequenceMatcher than the genuinely-unrelated pair should, while two
    different Cohens are obviously not the same person. Two people sharing
    one surname must NOT count as similar; one name being a more/less
    specific version of the other (e.g. "Gila" vs "Gila Cohen" — the same
    person named with different specificity) should.
    """
    a_norm, b_norm = a.strip().lower(), b.strip().lower()
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True

    a_tokens, b_tokens = set(a_norm.split()), set(b_norm.split())
    if a_tokens and b_tokens and (a_tokens <= b_tokens or b_tokens <= a_tokens):
        return True

    # Single-token names only: a strict character-similarity fallback for
    # spelling/transliteration variants (e.g. "גילה" vs "גליה"). Never
    # applied to multi-token names — that's exactly the shared-surname trap
    # above.
    if len(a_tokens) == 1 and len(b_tokens) == 1:
        return SequenceMatcher(None, a_norm, b_norm).ratio() >= _TOKEN_SIMILARITY_THRESHOLD

    return False


async def _search_nodes(query: str, group_id: str, limit: int) -> list[EntityNode]:
    graphiti = get_graphiti()
    config = NODE_HYBRID_SEARCH_RRF.model_copy(update={"limit": limit})
    results = await graphiti.search_(query=query, config=config, group_ids=[group_id])
    return results.nodes
