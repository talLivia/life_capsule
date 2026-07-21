"""
Standalone text-embedding helper for Prompt 7's relevance scoring.

Deliberately reuses the SAME provider/model/dimension settings Graphiti's
own embedder uses (GRAPHITI_EMBEDDER_PROVIDER, GEMINI_EMBEDDING_MODEL,
GRAPHITI_EMBEDDING_DIM) rather than introducing a separate embedding
config — a question's embedding and a segment's embedding (computed at
ingestion time, analysis_graph.py's embed_transcript node) must live in the
same vector space for cosine similarity to mean anything, and Graphiti
already has this fully configured (see graph_memory.py's _build_graphiti
docstring for why gemini is the default).

Independent module from graph_memory.py on purpose: this has nothing to do
with the graph client itself (no Neo4j driver, no LLM client) — just the
embedder, usable standalone for arbitrary text (a question, a transcript)
without spinning up a full Graphiti instance.
"""

from __future__ import annotations

import math
from typing import List, Optional

from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

from app.config import settings


def _build_embedder():
    if settings.GRAPHITI_EMBEDDER_PROVIDER == "gemini":
        return GeminiEmbedder(
            config=GeminiEmbedderConfig(
                api_key=settings.GEMINI_API_KEY,
                embedding_model=settings.GRAPHITI_EMBEDDING_MODEL,
                embedding_dim=settings.GRAPHITI_EMBEDDING_DIM,
            )
        )
    return OpenAIEmbedder(config=OpenAIEmbedderConfig(api_key=settings.OPENAI_API_KEY))


_embedder = None


def get_embedder():
    """Lazy singleton — avoids constructing an API client at import time
    (e.g. during unrelated unit tests that never touch embeddings)."""
    global _embedder
    if _embedder is None:
        _embedder = _build_embedder()
    return _embedder


def reset_embedder() -> None:
    """Test hook — drop the cached embedder so the next get_embedder() call
    rebuilds it (e.g. against monkeypatched settings)."""
    global _embedder
    _embedder = None


async def embed_text(text: str) -> List[float]:
    """Embed a single piece of text (a question, or a segment transcript at
    ingestion time). Raises on failure — callers decide how to degrade
    (relevance_scorer.py treats a missing/failed embedding as "no relevance
    signal", not a hard error)."""
    return await get_embedder().create(text)


def cosine_similarity(a: Optional[List[float]], b: Optional[List[float]]) -> float:
    """Returns 0.0 (no signal) rather than raising when either vector is
    missing or malformed — callers use this as one of three additive
    scoring terms, not something that should crash a whole turn."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
