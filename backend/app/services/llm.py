"""
LLM facade for constrained, task-scoped Claude/GPT calls.

NOTE: this project's free-form chat loop (the model answering an open-ended
conversation from general knowledge) was removed in Prompt 1 — see
`websocket.py`'s response producer. Every remaining caller of this module
(topic extraction, entity extraction, importance scoring, summarization,
retrieval's topic classifier, bridge-phrase assembly) must pass an explicit,
narrowly-scoped `system_prompt`; there is no default "helpful assistant"
persona, on purpose, so a call site can never silently fall back to
unconstrained general-knowledge answering.

Provider-agnostic interface that currently supports Anthropic (default),
OpenAI, and Gemini — LLM_PROVIDER=gemini reuses GEMINI_API_KEY (the same key
already used for Graphiti's own extraction, see graph_memory.py) instead of
requiring a separate Anthropic/OpenAI account. The Anthropic path takes
advantage of:

  * **Prompt caching** — the system prompt is wrapped in a content block
    with `cache_control={"type": "ephemeral"}`. Cached reads cost ~10% of
    fresh input, which dominates per-token cost for chatty avatars that
    share a system prompt across many turns. Workspace-isolated as of
    Anthropic's Feb 2026 change.
  * **Extended thinking (opt-in)** — when callers pass `thinking=True`
    we set `thinking={"type": "enabled", "budget_tokens": ...}` so the
    model reasons internally before answering. Reserved for hard turns;
    using it on every turn would multiply token cost.

Exceptions are re-raised as `LLMError` subclasses so the WebSocket pipeline
can distinguish rate-limit / auth / network failures and surface
appropriate user-facing messages.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator, Dict, List, Optional

import anthropic
import openai
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.config import settings

logger = logging.getLogger(__name__)

# Extended-thinking budget. Claude 4.x Opus supports up to 128k thinking
# tokens; for an interactive avatar we want responses fast, so we cap the
# budget low. Increase for research/agentic use cases.
_DEFAULT_THINKING_BUDGET = 4096


class LLMError(Exception):
    """Base class — chat pipeline catches this and emits a typed WS error."""


class LLMRateLimited(LLMError):
    """Provider returned 429."""


class LLMAuthError(LLMError):
    """Provider returned 401/403 — usually a misconfigured API key."""


class LLMUnavailable(LLMError):
    """Network failure, timeout, or 5xx from the provider."""


def _cacheable_system(system_prompt: Optional[str]) -> list[dict]:
    """
    Build a system block list with prompt-cache marking applied to the
    (long-lived) system prompt. The SDK accepts either a plain string OR
    a list of blocks; blocks are needed to attach `cache_control` per-block.

    No default persona: every caller must supply a task-scoped system
    prompt, so it's impossible to accidentally invoke Claude as an
    open-ended general-knowledge assistant.
    """
    if not system_prompt:
        raise LLMError(
            "system_prompt is required — there is no default chat persona "
            "(see module docstring: free-form chat was removed in Prompt 1)"
        )
    return [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]


def _map_anthropic_exception(exc: Exception) -> LLMError:
    """Translate Anthropic SDK exceptions into the typed LLMError hierarchy."""
    if isinstance(exc, anthropic.RateLimitError):
        return LLMRateLimited(str(exc))
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return LLMAuthError(str(exc))
    if isinstance(
        exc,
        (anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.InternalServerError),
    ):
        return LLMUnavailable(str(exc))
    if isinstance(exc, anthropic.BadRequestError):
        # 400 from Anthropic is usually our bug, not theirs — surface verbatim.
        return LLMError(f"Invalid request to Anthropic: {exc}")
    return LLMError(str(exc))


def _map_openai_exception(exc: Exception) -> LLMError:
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimited(str(exc))
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return LLMAuthError(str(exc))
    if isinstance(
        exc, (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError)
    ):
        return LLMUnavailable(str(exc))
    return LLMError(str(exc))


def _map_gemini_exception(exc: Exception) -> LLMError:
    """google-genai raises APIError (ClientError=4xx, ServerError=5xx) with a
    `.code` carrying the HTTP status — no separate exception classes per
    status the way anthropic/openai have, so branch on the code instead."""
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        if code == 429:
            return LLMRateLimited(str(exc))
        if code in (401, 403):
            return LLMAuthError(str(exc))
        if isinstance(exc, genai_errors.ServerError) or (code and code >= 500):
            return LLMUnavailable(str(exc))
    return LLMError(str(exc))


class LLMService:
    """LLM Service for AI responses."""

    def __init__(self):
        self.provider = settings.LLM_PROVIDER
        self.model = settings.LLM_MODEL
        self.temperature = settings.LLM_TEMPERATURE
        self.max_tokens = settings.LLM_MAX_TOKENS

        if self.provider == "anthropic":
            self.client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        elif self.provider == "ollama":
            # Ollama (and vLLM / LM Studio / OpenRouter) speak the OpenAI
            # wire protocol — reuse the OpenAI client against their base URL.
            # Fully local and free; no API key required (the client insists
            # on a non-empty string, so we pass a placeholder).
            base_url = settings.OPENAI_BASE_URL or "http://localhost:11434/v1"
            self.client = openai.AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY or "ollama",
                base_url=base_url,
            )
            self.provider = "openai"  # downstream code paths are identical
            logger.info(f"LLM provider 'ollama' → OpenAI-compatible client at {base_url}")
        elif self.provider == "openai":
            self.client = openai.AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY,
                base_url=settings.OPENAI_BASE_URL,  # None → api.openai.com
            )
        elif self.provider == "gemini":
            self.client = genai.Client(api_key=settings.GEMINI_API_KEY)

    # ── non-streaming ────────────────────────────────────────────────────────

    async def generate_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        thinking: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        """`temperature` overrides settings.LLM_TEMPERATURE for this call only
        — e.g. Prompt 6's topic classifier wants temperature=0 (deterministic)
        regardless of the app-wide default used for conversational replies."""
        if self.provider == "anthropic":
            return await self._generate_anthropic(messages, system_prompt, thinking, temperature)
        if self.provider == "openai":
            return await self._generate_openai(messages, system_prompt, temperature)
        if self.provider == "gemini":
            return await self._generate_gemini(messages, system_prompt, temperature)
        raise LLMError(f"Unsupported LLM provider: {self.provider}")

    async def _generate_anthropic(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str],
        thinking: bool,
        temperature: Optional[float] = None,
    ) -> str:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
            "system": _cacheable_system(system_prompt),
            "messages": messages,
        }
        # Extended thinking — model "thinks" privately before answering. The
        # thinking tokens still count against output budget so we widen
        # max_tokens to cover both. Per Anthropic docs, temperature must be 1
        # when extended thinking is enabled — this is a hard API requirement,
        # so it overrides any explicit `temperature` argument too.
        if thinking:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": _DEFAULT_THINKING_BUDGET}
            kwargs["max_tokens"] = max(self.max_tokens + _DEFAULT_THINKING_BUDGET, self.max_tokens)
            kwargs["temperature"] = 1.0

        try:
            response = await self.client.messages.create(**kwargs)
        except Exception as e:
            mapped = _map_anthropic_exception(e)
            logger.error(
                "anthropic_call_failed",
                extra={"error_type": type(e).__name__, "mapped": type(mapped).__name__},
            )
            raise mapped from e

        # Find the first text block (skip thinking blocks if any).
        for block in response.content or []:
            if getattr(block, "type", None) == "text" and hasattr(block, "text"):
                self._log_usage(response.usage, thinking)
                return block.text

        raise LLMError("Anthropic response contained no text block")

    async def _generate_openai(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> str:
        if not system_prompt:
            raise LLMError("system_prompt is required — see module docstring")
        messages = [{"role": "system", "content": system_prompt}] + messages

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as e:
            mapped = _map_openai_exception(e)
            logger.error("openai_call_failed", extra={"error_type": type(e).__name__})
            raise mapped from e

        return response.choices[0].message.content or ""

    def _gemini_contents(self, messages: List[Dict[str, str]]) -> list:
        # Gemini's roles are "user"/"model" (not "assistant") — our own
        # call sites never put "system" in the messages list (it's always
        # the separate system_prompt param, same convention as the
        # anthropic/openai paths above), so anything else safely passes
        # through as "user".
        return [
            genai_types.Content(
                role="model" if m.get("role") == "assistant" else "user",
                parts=[genai_types.Part.from_text(text=m["content"])],
            )
            for m in messages
        ]

    async def _generate_gemini(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> str:
        if not system_prompt:
            raise LLMError("system_prompt is required — see module docstring")

        config = genai_types.GenerateContentConfig(
            temperature=temperature if temperature is not None else self.temperature,
            max_output_tokens=self.max_tokens,
            system_instruction=system_prompt,
        )
        try:
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=self._gemini_contents(messages),
                config=config,
            )
        except Exception as e:
            mapped = _map_gemini_exception(e)
            logger.error(
                "gemini_call_failed",
                extra={"error_type": type(e).__name__, "mapped": type(mapped).__name__},
            )
            raise mapped from e

        text = getattr(response, "text", None)
        if not text:
            raise LLMError("Gemini response contained no text")
        self._log_gemini_usage(getattr(response, "usage_metadata", None))
        return text

    # ── streaming ────────────────────────────────────────────────────────────

    async def stream_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        if self.provider == "anthropic":
            async for chunk in self._stream_anthropic(messages, system_prompt):
                yield chunk
        elif self.provider == "openai":
            async for chunk in self._stream_openai(messages, system_prompt):
                yield chunk
        elif self.provider == "gemini":
            async for chunk in self._stream_gemini(messages, system_prompt):
                yield chunk
        else:
            raise LLMError(f"Unsupported LLM provider: {self.provider}")

    async def _stream_anthropic(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        try:
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=_cacheable_system(system_prompt),
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as e:
            mapped = _map_anthropic_exception(e)
            logger.error("anthropic_stream_failed", extra={"error_type": type(e).__name__})
            raise mapped from e

    async def _stream_openai(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        if not system_prompt:
            raise LLMError("system_prompt is required — see module docstring")
        messages = [{"role": "system", "content": system_prompt}] + messages

        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                stream=True,
            )
            async for chunk in stream:
                content = chunk.choices[0].delta.content
                if content:
                    yield content
        except Exception as e:
            mapped = _map_openai_exception(e)
            logger.error("openai_stream_failed", extra={"error_type": type(e).__name__})
            raise mapped from e

    async def _stream_gemini(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        if not system_prompt:
            raise LLMError("system_prompt is required — see module docstring")

        config = genai_types.GenerateContentConfig(
            temperature=self.temperature,
            max_output_tokens=self.max_tokens,
            system_instruction=system_prompt,
        )
        try:
            stream = await self.client.aio.models.generate_content_stream(
                model=self.model,
                contents=self._gemini_contents(messages),
                config=config,
            )
            async for chunk in stream:
                if chunk.text:
                    yield chunk.text
        except Exception as e:
            mapped = _map_gemini_exception(e)
            logger.error("gemini_stream_failed", extra={"error_type": type(e).__name__})
            raise mapped from e

    # ── helpers ──────────────────────────────────────────────────────────────

    def _log_usage(self, usage, thinking: bool) -> None:
        if usage is None:
            return
        try:
            logger.info(
                "llm_usage",
                extra={
                    "in_tokens": getattr(usage, "input_tokens", 0),
                    "out_tokens": getattr(usage, "output_tokens", 0),
                    "cache_create_tokens": getattr(usage, "cache_creation_input_tokens", 0),
                    "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0),
                    "thinking": thinking,
                },
            )
        except Exception:
            pass

    def _log_gemini_usage(self, usage_metadata) -> None:
        if usage_metadata is None:
            return
        try:
            logger.info(
                "llm_usage",
                extra={
                    "in_tokens": getattr(usage_metadata, "prompt_token_count", 0) or 0,
                    "out_tokens": getattr(usage_metadata, "candidates_token_count", 0) or 0,
                    "provider": "gemini",
                },
            )
        except Exception:
            pass


# Global instance
llm_service = LLMService()
