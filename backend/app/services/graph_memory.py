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


async def add_episode(
    segment_id: str,
    transcript: str,
    topic_tags: list[str],
    timestamp: datetime,
    group_id: str = DEFAULT_GROUP_ID,
) -> AddEpisodeResults:
    """
    Ingest a story segment into Graphiti as an episode. Graphiti's own LLM
    extraction (Claude) pulls entities and relationships out of the
    transcript automatically — we don't hand-parse anything here.

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


async def _search_nodes(query: str, group_id: str, limit: int) -> list[EntityNode]:
    graphiti = get_graphiti()
    config = NODE_HYBRID_SEARCH_RRF.model_copy(update={"limit": limit})
    results = await graphiti.search_(query=query, config=config, group_ids=[group_id])
    return results.nodes
