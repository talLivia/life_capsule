import json
import logging
from typing import Any, Optional

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)


class CacheService:
    """Redis-based caching service for API responses and frequently accessed data."""

    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None
        self.default_ttl = 300  # 5 minutes

    async def initialize(self):
        """Initialize Redis connection."""
        try:
            self.redis = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
            )
            await self.redis.ping()
            logger.info("Redis cache connected successfully")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            self.redis = None

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
