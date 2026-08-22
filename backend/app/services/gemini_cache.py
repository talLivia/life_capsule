"""Per-producer explicit Gemini context caches (GEMINI_CONTEXT_CACHING_PLAN
Phase B — skeleton, built 2026-08-23, ACTIVATION GATED).

One cache per (producer, archive_version): the cache holds the archive-read
call's system prompt (instructions + mark-free transcript + entity map),
which under SHOWN_STATE_PLACEMENT=message is byte-stable until the archive
itself changes. Identity therefore INCLUDES the version fingerprint —
correctness never depends on deletion succeeding: a stale cache can be paid
for, never served, because its key stops matching the moment the archive
version moves (the same `_archive_version` tuple the in-process archive
cache validates against).

Fail-soft everywhere, same posture as every other cache in this codebase:
no answer is ever allowed to fail over a cache. Expiry/miss is a PRICE
event — the read retries uncached at full price and the cache is recreated
in the background.

GEMINI_CONTEXT_CACHE=off (default): every function is an inert no-op.
Activation (wiring read_with_cache into _read_archive_for_ranges and
flipping the flag) happens only after Phase A clears its §6 gate — a cache
pinned on today's marks-bearing transcript would serve turns 2+ a prefix
whose shown-marks don't match the conversation, an ungated prompt change.

Model-coupled surface (re-verify at every model upgrade, alongside the
prompt re-baseline): minimum cacheable token count, storage pricing, and
TTL-update support are PER-MODEL properties of the pinned
ARCHIVE_READ_MODEL.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

#: Initial cache lifetime. A conversation's length, not a day — storage is
#: billed per token-hour and idle producers should simply not hold caches.
CACHE_TTL_SECONDS = 45 * 60

#: Renew when a hit lands inside this window before expiry, so an active
#: conversation never straddles an expiry mid-flight.
RENEW_WINDOW_SECONDS = 10 * 60


def version_hash(version: tuple) -> str:
    """Stable short hash of the `_archive_version` tuple. Part of the cache
    IDENTITY (display name), which is what makes invalidation correct by
    construction — a version move orphans the old cache rather than
    requiring its deletion."""
    return hashlib.sha256(repr(tuple(version)).encode()).hexdigest()[:16]


def cache_display_name(group_id: str, version: tuple) -> str:
    return f"archive:{group_id}:{version_hash(version)}"


@dataclass
class _Handle:
    name: str  # API resource name (cachedContents/...)
    version_key: str  # version_hash the cache was built from
    expires_at: float  # monotonic-ish wall clock, seconds


#: In-process, like _ARCHIVE_CACHE and for the same reason (no Redis in this
#: deployment). Being wrong here is never a correctness problem: a missing
#: entry means one full-price call + a re-create; a stale entry fails the
#: version check and is ignored.
_REGISTRY: Dict[str, _Handle] = {}


def _enabled() -> bool:
    return settings.GEMINI_CONTEXT_CACHE == "on"


def registry_lookup(group_id: str, version: tuple) -> Optional[str]:
    """The cache resource name to reference for this producer at this
    archive version, or None (off / unknown / version-mismatched /
    expired). Pure and synchronous — safe anywhere."""
    if not _enabled():
        return None
    h = _REGISTRY.get(group_id)
    if h is None:
        return None
    if h.version_key != version_hash(version):
        return None
    if h.expires_at <= time.time():
        _REGISTRY.pop(group_id, None)
        return None
    return h.name


async def ensure_cache(group_id: str, version: tuple, system_prompt: str) -> Optional[str]:
    """Return a live cache name for (producer, version), creating one if
    needed. Fail-soft: any API error logs and returns None — the caller
    proceeds uncached."""
    if not _enabled():
        return None
    existing = registry_lookup(group_id, version)
    if existing:
        return existing
    try:
        from app.services.llm import llm_service
        from google.genai import types as genai_types

        created = await llm_service.client.aio.caches.create(
            model=settings.ARCHIVE_READ_MODEL or llm_service.model,
            config=genai_types.CreateCachedContentConfig(
                display_name=cache_display_name(group_id, version),
                system_instruction=system_prompt,
                ttl=f"{CACHE_TTL_SECONDS}s",
            ),
        )
        _REGISTRY[group_id] = _Handle(
            name=created.name,
            version_key=version_hash(version),
            expires_at=time.time() + CACHE_TTL_SECONDS,
        )
        logger.info(
            f"gemini cache created for {group_id}: {created.name} "
            f"({cache_display_name(group_id, version)})"
        )
        return created.name
    except Exception as e:
        # Includes prompts under the model's minimum cacheable size — a
        # per-model property; small archives simply run uncached.
        logger.warning(f"gemini cache create failed for {group_id}: {e}")
        return None


async def touch_cache(group_id: str) -> None:
    """Extend TTL when a hit lands near expiry, so active conversations
    don't straddle an expiry. Best-effort; a failed renewal just means one
    future full-price call."""
    if not _enabled():
        return
    h = _REGISTRY.get(group_id)
    if h is None or h.expires_at - time.time() > RENEW_WINDOW_SECONDS:
        return
    try:
        from app.services.llm import llm_service
        from google.genai import types as genai_types

        await llm_service.client.aio.caches.update(
            name=h.name,
            config=genai_types.UpdateCachedContentConfig(ttl=f"{CACHE_TTL_SECONDS}s"),
        )
        h.expires_at = time.time() + CACHE_TTL_SECONDS
    except Exception as e:
        logger.warning(f"gemini cache ttl renewal failed for {group_id}: {e}")


async def drop_cache(group_id: str) -> None:
    """Best-effort deletion — BILLING hygiene, never correctness (version
    keying already orphans stale caches). Called from the same two places
    that refresh the in-process archive cache: finalize_ingest_node and
    segment deletion."""
    h = _REGISTRY.pop(group_id, None)
    if h is None or not _enabled():
        return
    try:
        from app.services.llm import llm_service

        await llm_service.client.aio.caches.delete(name=h.name)
        logger.info(f"gemini cache dropped for {group_id}: {h.name}")
    except Exception as e:
        logger.warning(f"gemini cache delete failed for {group_id} (harmless): {e}")


async def read_with_cache(
    group_id: str,
    version: tuple,
    system_prompt: str,
    call,
) -> Tuple[str, bool]:
    """The fail-soft read wrapper (UNWIRED until Phase B activation).

    `call` is an async callable taking `cached_content: Optional[str]` and
    returning the model text — in practice a partial over
    llm_service.generate_response. Returns (text, used_cache).

    Miss/expiry handling: a failed cached call drops the registry entry and
    retries the SAME request uncached — the answer must never fail over a
    cache — then recreation is left to the NEXT read (keeping this wrapper
    single-purpose; no background task fan-out mid-question)."""
    name = registry_lookup(group_id, version) or await ensure_cache(
        group_id, version, system_prompt
    )
    if name is None:
        return await call(cached_content=None), False
    try:
        text = await call(cached_content=name)
        await touch_cache(group_id)
        return text, True
    except Exception as e:
        logger.warning(
            f"cached read failed for {group_id} ({e}); retrying uncached at full price"
        )
        _REGISTRY.pop(group_id, None)
        return await call(cached_content=None), False


def _reset_registry_for_tests() -> None:
    _REGISTRY.clear()
