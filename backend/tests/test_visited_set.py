"""
Tests for CacheService's per-conversation visited-set helper (Prompt 2),
which Prompts 6-7's graph-expansion step uses to avoid re-surfacing a
segment already used earlier in the same conversation.

No live Redis is required — a minimal in-memory fake stands in for
`redis.asyncio.Redis`, exercising the same sadd/smembers/expire/delete
call shapes the real client would receive.
"""

import pytest

from app.services.cache import CacheService

pytestmark = pytest.mark.asyncio


class FakeRedis:
    """Just enough of redis.asyncio.Redis's async interface to exercise the
    visited-set methods without a live server."""

    def __init__(self):
        self.sets: dict[str, set] = {}
        self.ttls: dict[str, int] = {}

    async def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)
        return len(members)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.sets:
                del self.sets[k]
                n += 1
        return n


@pytest.fixture
def cache():
    svc = CacheService()
    svc.redis = FakeRedis()
    return svc


async def test_add_and_get_visited_round_trips(cache):
    await cache.add_visited("sess-1", ["seg-a", "seg-b"])
    assert await cache.get_visited("sess-1") == {"seg-a", "seg-b"}


async def test_visited_sets_are_isolated_per_session(cache):
    await cache.add_visited("sess-1", ["seg-a"])
    await cache.add_visited("sess-2", ["seg-b"])
    assert await cache.get_visited("sess-1") == {"seg-a"}
    assert await cache.get_visited("sess-2") == {"seg-b"}


async def test_add_visited_sets_a_ttl(cache):
    await cache.add_visited("sess-1", ["seg-a"])
    assert cache.redis.ttls[cache._visited_key("sess-1")] == CacheService._VISITED_SET_TTL


async def test_get_visited_empty_for_unknown_session(cache):
    assert await cache.get_visited("never-seen") == set()


async def test_clear_visited_removes_the_set(cache):
    await cache.add_visited("sess-1", ["seg-a"])
    assert await cache.clear_visited("sess-1") is True
    assert await cache.get_visited("sess-1") == set()


async def test_add_visited_with_no_segment_ids_is_a_noop(cache):
    assert await cache.add_visited("sess-1", []) is False
    assert await cache.get_visited("sess-1") == set()


async def test_visited_helpers_degrade_gracefully_without_redis():
    svc = CacheService()  # redis is None until initialize() connects
    assert await svc.add_visited("sess-1", ["seg-a"]) is False
    assert await svc.get_visited("sess-1") == set()
    assert await svc.clear_visited("sess-1") is False
