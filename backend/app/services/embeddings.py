"""
Standalone text-embedding helper for Prompt 7's relevance scoring.

A question's embedding and a segment's embedding (computed at ingestion time
by analysis_graph.py's embed_transcript node) must live in the SAME vector
space for cosine similarity to mean anything — so there is one embedding
config here and everything uses it.

⚠️ CHANGING `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL` OR `EMBEDDING_DIM`
INVALIDATES EVERY EMBEDDING ALREADY STORED. Nothing fails loudly if you do:
cosine similarity between vectors from two different models still returns a
number, it just stops meaning anything, and retrieval quietly degrades. A
change here has to be followed by re-embedding every RawSegment and
TranscriptChunk.

These settings used to be spelled GRAPHITI_EMBEDDER_PROVIDER /
GRAPHITI_EMBEDDING_MODEL / GRAPHITI_EMBEDDING_DIM, because this module used
graphiti-core's embedder wrappers even though it never touched the graph
itself. Graphiti is gone; the provider clients here call the same APIs with
the same model and the same output dimensionality, deliberately, so every
vector already in the database stays comparable. That equivalence is the
whole reason this file is written the way it is — see `_embed_gemini` for the
call it is reproducing.
"""

from __future__ import annotations

import math
from typing import List, Optional

from app.config import settings


async def _embed_gemini(text: str) -> List[float]:
    """Reproduces graphiti-core's GeminiEmbedder.create exactly.

    Specifically: `contents` is a LIST containing the one string (not the bare
    string), and `output_dimensionality` is passed rather than left to the
    model's default. Both matter — the model returns a different-length vector
    without the second, and every stored vector was produced with it.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    result = await client.aio.models.embed_content(
        model=settings.EMBEDDING_MODEL,
        contents=[text],
        config=types.EmbedContentConfig(output_dimensionality=settings.EMBEDDING_DIM),
    )
    if not result.embeddings or not result.embeddings[0].values:
        raise ValueError("No embeddings returned from the Gemini API")
    return result.embeddings[0].values


async def _embed_openai(text: str) -> List[float]:
    """The OpenAI alternative, likewise matching what graphiti-core did —
    including the TRUNCATION to `embedding_dim`, which is a slice of the
    returned vector rather than an API parameter."""
    import openai

    client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    result = await client.embeddings.create(input=text, model=settings.EMBEDDING_MODEL)
    return result.data[0].embedding[: settings.EMBEDDING_DIM]


async def embed_text(text: str) -> List[float]:
    """Embed a single piece of text (a question, or a segment transcript at
    ingestion time). Raises on failure — callers decide how to degrade
    (ingestion leaves embedding=None and carries on, never failing the
    segment)."""
    if settings.EMBEDDING_PROVIDER == "gemini":
        return await _embed_gemini(text)
    return await _embed_openai(text)


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
