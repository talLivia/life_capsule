"""Tests for the embedding helper.

This module was reimplemented by hand when graphiti-core was removed, and it
had no tests before that. It needs them precisely because its failure mode is
SILENT: a question's vector and a segment's vector only mean anything relative
to each other, so a changed model, dimension, or call shape does not raise —
cosine similarity keeps returning plausible numbers and retrieval just quietly
gets worse.

The call-shape assertions below are not pedantry. They pin the two details
that had to match graphiti-core's GeminiEmbedder exactly for the ~3072-dim
vectors already in the database to stay comparable: `contents` is a LIST
containing the string, and `output_dimensionality` is passed explicitly.
Verified once against the live API at migration time (cosine 1.000000 between
a stored vector and the same text re-embedded through this path); these keep
it that way.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import embeddings

pytestmark = pytest.mark.asyncio


# ── cosine_similarity ───────────────────────────────────────────────────────


def test_cosine_of_identical_vectors_is_one():
    assert embeddings.cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_of_orthogonal_vectors_is_zero():
    assert embeddings.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


@pytest.mark.parametrize(
    "a,b,why",
    [
        (None, [1.0], "missing embedding on one side"),
        ([1.0], None, "missing on the other"),
        ([], [1.0], "empty"),
        ([1.0, 2.0], [1.0], "mismatched dimensions"),
        ([0.0, 0.0], [1.0, 1.0], "a zero vector has no direction"),
    ],
)
def test_cosine_returns_no_signal_rather_than_raising(a, b, why):
    """Callers use this as one of three additive scoring terms — a missing
    embedding must degrade the score, never crash a whole turn."""
    assert embeddings.cosine_similarity(a, b) == 0.0, why


# ── provider routing ────────────────────────────────────────────────────────


async def test_gemini_is_the_configured_provider(monkeypatch):
    monkeypatch.setattr(embeddings.settings, "EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setattr(embeddings, "_embed_gemini", AsyncMock(return_value=[0.1]))
    monkeypatch.setattr(embeddings, "_embed_openai", AsyncMock(return_value=[0.9]))
    assert await embeddings.embed_text("x") == [0.1]


async def test_a_non_gemini_provider_routes_to_openai(monkeypatch):
    monkeypatch.setattr(embeddings.settings, "EMBEDDING_PROVIDER", "openai")
    monkeypatch.setattr(embeddings, "_embed_gemini", AsyncMock(return_value=[0.1]))
    monkeypatch.setattr(embeddings, "_embed_openai", AsyncMock(return_value=[0.9]))
    assert await embeddings.embed_text("x") == [0.9]


# ── the call shape that keeps stored vectors comparable ─────────────────────


def _fake_genai(monkeypatch, embed_content):
    """Stand in for `from google import genai` / `from google.genai import types`.

    Both are LOCAL imports inside the function under test, so patching has to
    happen in sys.modules rather than on the module object. `monkeypatch.setitem`
    restores the real entries afterwards — leaving a MagicMock in sys.modules
    would silently break every later test that touches the Gemini client.
    """
    captured = {}

    class FakeEmbedConfig:
        def __init__(self, output_dimensionality):
            captured["output_dimensionality"] = output_dimensionality

    client = MagicMock()
    client.aio.models.embed_content = embed_content
    genai_module = SimpleNamespace(Client=lambda api_key=None: client)
    types_module = SimpleNamespace(EmbedContentConfig=FakeEmbedConfig)
    genai_module.types = types_module

    monkeypatch.setitem(sys.modules, "google.genai", genai_module)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_module)
    monkeypatch.setitem(
        sys.modules, "google", SimpleNamespace(genai=genai_module, __path__=[])
    )
    return captured


async def test_gemini_call_passes_a_list_and_an_explicit_dimensionality(monkeypatch):
    """Both details are load-bearing. `contents` must be a LIST (the bare
    string embeds differently), and without `output_dimensionality` the model
    returns its own default length — either one silently invalidates every
    vector already stored."""
    seen = {}

    async def embed_content(*, model, contents, config):
        seen.update(model=model, contents=contents)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.5] * 8)])

    captured = _fake_genai(monkeypatch, embed_content)
    monkeypatch.setattr(embeddings.settings, "EMBEDDING_MODEL", "gemini-embedding-001")
    monkeypatch.setattr(embeddings.settings, "EMBEDDING_DIM", 3072)

    result = await embeddings._embed_gemini("שירתתי בחיל האוויר")

    assert result == [0.5] * 8
    assert seen["contents"] == ["שירתתי בחיל האוויר"], "must be a LIST, not the bare string"
    assert seen["model"] == "gemini-embedding-001"
    assert captured["output_dimensionality"] == 3072, "must be passed explicitly"


async def test_gemini_raises_when_the_api_returns_nothing(monkeypatch):
    """Callers treat a raised error as "no relevance signal"; silently
    returning an empty vector would instead poison the stored embedding."""

    async def empty(*, model, contents, config):
        return SimpleNamespace(embeddings=[])

    _fake_genai(monkeypatch, empty)
    with pytest.raises(ValueError):
        await embeddings._embed_gemini("x")


async def test_openai_truncates_to_the_configured_dimension(monkeypatch):
    """graphiti-core sliced the returned vector rather than asking the API for
    a size, so the OpenAI path has to slice too or the two would disagree."""
    client = MagicMock()
    client.embeddings.create = AsyncMock(
        return_value=SimpleNamespace(data=[SimpleNamespace(embedding=[0.1] * 100)])
    )
    monkeypatch.setitem(
        sys.modules, "openai", SimpleNamespace(AsyncOpenAI=lambda api_key=None: client)
    )
    monkeypatch.setattr(embeddings.settings, "EMBEDDING_DIM", 8)

    assert len(await embeddings._embed_openai("x")) == 8
