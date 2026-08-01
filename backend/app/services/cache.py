import json
import logging
import time
from typing import Any, Optional

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)


class CacheService:
    """Redis-based caching service for API responses and frequently accessed data."""

    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self.default_ttl = 300  # 5 minutes
        # WHY `redis` is None — every method below degrades to a silent no-op
        # when it is, which is correct for a cache but means a connection that
        # FAILED and a cache deliberately turned OFF are the same value. They
        # are not the same event, and /health could not tell them apart: it
        # reported both as "not configured" and left the app "healthy", so a
        # Redis that never connected in production looked exactly like success
        # while the clip cache silently did nothing.
        self.init_error: Optional[str] = None
        self.disabled: bool = False

    async def initialize(self):
        """Initialize Redis connection.

        A blank REDIS_URL is the ONE benign way to have no cache — an explicit
        "run without Redis". Every other path out of here without a connection
        is a failure and is recorded as one, so the health probe can say so."""
        self.init_error = None
        self.disabled = False

        if not (settings.REDIS_URL or "").strip():
            self.redis = None
            self.disabled = True
            logger.warning("REDIS_URL is blank — cache disabled deliberately, no-op mode")
            return

        try:
            self.redis = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
            )
            await self.redis.ping()
            logger.info("Redis cache connected successfully")
        except Exception as e:
            # Kept non-fatal on purpose: the cache is an optimisation and the
            # app is fully functional without it. The cost of that choice is
            # that nothing downstream notices, which is what init_error fixes.
            logger.error(f"Failed to connect to Redis: {e}")
            self.redis = None
            self.init_error = f"{type(e).__name__}: {e}"

    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache."""
        if not self.redis:
            return None
        try:
            value = await self.redis.get(key)
            if value:
                return json.loads(value)
            return None
        except Exception as e:
            logger.warning(f"Cache get error for key={key}: {e}")
            return None

    async def set(self, key: str, value: Any, ttl: int = None) -> bool:
        """Set value in cache with TTL."""
        if not self.redis:
            return False
        try:
            serialized = json.dumps(value, default=str)
            await self.redis.set(key, serialized, ex=ttl or self.default_ttl)
            return True
        except Exception as e:
            logger.warning(f"Cache set error for key={key}: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """Delete value from cache."""
        if not self.redis:
            return False
        try:
            await self.redis.delete(key)
            return True
        except Exception as e:
            logger.warning(f"Cache delete error for key={key}: {e}")
            return False

    async def delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching pattern."""
        if not self.redis:
            return 0
        try:
            keys = []
            async for key in self.redis.scan_iter(match=pattern):
                keys.append(key)
            if keys:
                await self.redis.delete(*keys)
            return len(keys)
        except Exception as e:
            logger.warning(f"Cache delete_pattern error for pattern={pattern}: {e}")
            return 0

    async def increment(self, key: str, amount: int = 1) -> Optional[int]:
        """Increment a counter in cache."""
        if not self.redis:
            return None
        try:
            return await self.redis.incr(key, amount)
        except Exception as e:
            logger.warning(f"Cache increment error for key={key}: {e}")
            return None

    # ── Retrieval visited-set (Prompts 6-7 loop prevention) ──────────────────
    # Keyed per conversation session so the graph-expansion step never
    # re-surfaces a segment already used earlier in the same conversation.
    # A TTL bounds memory for abandoned sessions instead of relying on an
    # explicit end-of-conversation cleanup call that might never arrive.
    _VISITED_SET_TTL = 6 * 3600  # 6h — comfortably longer than any single conversation

    def _visited_key(self, session_id: str) -> str:
        return f"visited:{session_id}"

    async def add_visited(self, session_id: str, segment_ids: list) -> bool:
        """Mark segment_ids as already surfaced in this conversation session."""
        if not self.redis or not segment_ids:
            return False
        try:
            key = self._visited_key(session_id)
            await self.redis.sadd(key, *segment_ids)
            await self.redis.expire(key, self._VISITED_SET_TTL)
            return True
        except Exception as e:
            logger.warning(f"Visited-set add error for session={session_id}: {e}")
            return False

    async def get_visited(self, session_id: str) -> set:
        """All segment ids already surfaced in this conversation session."""
        if not self.redis:
            return set()
        try:
            return await self.redis.smembers(self._visited_key(session_id))
        except Exception as e:
            logger.warning(f"Visited-set get error for session={session_id}: {e}")
            return set()

    async def clear_visited(self, session_id: str) -> bool:
        """Reset the visited-set, e.g. when a conversation session ends."""
        return await self.delete(self._visited_key(session_id))

    # ── Entity-mention recency (Prompt 7's recency_score) ────────────────────
    # A Redis HASH per session (entity_name -> unix timestamp last mentioned)
    # — distinct from the visited-set above (which tracks segment ids, for
    # exclusion) since recency_score needs to know how long ago a specific
    # ENTITY was last talked about, even via a different segment than the
    # one currently being scored. Written by whichever step finalizes what
    # actually reached the storyteller's response (Prompt 8's "update the
    # session's visited-set with all segment ids used" — the entities of
    # those same segments get recorded here too), never by retrieval or
    # scoring themselves.
    def _entity_mentions_key(self, session_id: str) -> str:
        return f"entity_mentions:{session_id}"

    async def record_entity_mentions(
        self, session_id: str, entity_names: list, at: Optional[float] = None
    ) -> bool:
        """Mark entity_names as mentioned (used in the response) right now
        (or at `at`, e.g. in tests) in this conversation session."""
        if not self.redis or not entity_names:
            return False
        try:
            timestamp = at if at is not None else time.time()
            key = self._entity_mentions_key(session_id)
            await self.redis.hset(key, mapping={name: timestamp for name in entity_names})
            await self.redis.expire(key, self._VISITED_SET_TTL)
            return True
        except Exception as e:
            logger.warning(f"Entity-mention record error for session={session_id}: {e}")
            return False

    async def get_entity_last_mentioned(self, session_id: str, entity_names: list) -> dict:
        """Unix timestamp each of `entity_names` was last mentioned in this
        session — entities never mentioned this session are simply absent
        from the returned dict (matches recency_score's "0 if never
        mentioned this session" per the project plan)."""
        if not self.redis or not entity_names:
            return {}
        try:
            key = self._entity_mentions_key(session_id)
            values = await self.redis.hmget(key, entity_names)
            return {
                name: float(value)
                for name, value in zip(entity_names, values)
                if value is not None
            }
        except Exception as e:
            logger.warning(f"Entity-mention get error for session={session_id}: {e}")
            return {}

    async def cleanup(self):
        """Close Redis connection."""
        if self.redis:
            # redis-py 5.x deprecated close() in favor of aclose(); fall back
            # for older clients that don't have it.
            closer = getattr(self.redis, "aclose", None) or self.redis.close
            await closer()
            logger.info("Redis cache connection closed")


# Global instance
cache_service = CacheService()
