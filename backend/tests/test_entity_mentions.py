"""
Tests for CacheService's per-session entity-mention recency tracking
(Prompt 7's recency_score reads this) — a minimal in-memory fake stands in
for redis.asyncio.Redis's hset/hmget/expire, same approach as
test_visited_set.py.
"""

import pytest

from app.services.cache import CacheService

pytestmark = pytest.mark.asyncio


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})
        return len(mapping)

    async def hmget(self, key, fields):
        h = self.hashes.get(key, {})
        return [h.get(f) for f in fields]

    async def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.hashes:
                del self.hashes[k]
                n += 1
        return n


@pytest.fixture
def cache():
    svc = CacheService()
    svc.redis = FakeRedis()
    return svc


async def test_record_and_get_entity_mentions_round_trip(cache):
    await cache.record_entity_mentions("sess-1", ["Gila", "Dan"], at=1000.0)
    mentions = await cache.get_entity_last_mentioned("sess-1", ["Gila", "Dan"])
    assert mentions == {"Gila": 1000.0, "Dan": 1000.0}


async def test_get_entity_last_mentioned_omits_unmentioned_entities(cache):
    await cache.record_entity_mentions("sess-1", ["Gila"], at=1000.0)
    mentions = await cache.get_entity_last_mentioned("sess-1", ["Gila", "NeverMentioned"])
    assert mentions == {"Gila": 1000.0}


async def test_entity_mentions_are_isolated_per_session(cache):
    await cache.record_entity_mentions("sess-1", ["Gila"], at=1000.0)
    await cache.record_entity_mentions("sess-2", ["Gila"], at=2000.0)
    assert await cache.get_entity_last_mentioned("sess-1", ["Gila"]) == {"Gila": 1000.0}
    assert await cache.get_entity_last_mentioned("sess-2", ["Gila"]) == {"Gila": 2000.0}


async def test_record_entity_mentions_sets_a_ttl(cache):
    await cache.record_entity_mentions("sess-1", ["Gila"], at=1000.0)
    assert cache.redis.ttls[cache._entity_mentions_key("sess-1")] == CacheService._VISITED_SET_TTL


async def test_record_entity_mentions_updates_timestamp_on_remention(cache):
    await cache.record_entity_mentions("sess-1", ["Gila"], at=1000.0)
    await cache.record_entity_mentions("sess-1", ["Gila"], at=2000.0)
    assert await cache.get_entity_last_mentioned("sess-1", ["Gila"]) == {"Gila": 2000.0}


async def test_record_entity_mentions_with_no_names_is_a_noop(cache):
    assert await cache.record_entity_mentions("sess-1", []) is False
    assert await cache.get_entity_last_mentioned("sess-1", ["Gila"]) == {}


async def test_entity_mentions_degrade_gracefully_without_redis():
    svc = CacheService()
    assert await svc.record_entity_mentions("sess-1", ["Gila"]) is False
    assert await svc.get_entity_last_mentioned("sess-1", ["Gila"]) == {}
