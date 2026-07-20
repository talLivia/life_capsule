"""
Fast, non-integration guard-clause tests for graph_memory.py's Prompt 6
additions. The Cypher-querying behavior itself needs a live Neo4j instance
(see test_graph_memory_int.py and scripts/smoke_test_prompt5.py for that);
these just verify the early-return paths that don't touch the network.
"""

import pytest

from app.services import graph_memory as gm

pytestmark = pytest.mark.asyncio


async def test_find_related_episodes_scored_empty_entity_names():
    assert await gm.find_related_episodes_scored([], exclude_ids=[]) == []


async def test_find_related_episodes_scored_zero_max_hops():
    assert await gm.find_related_episodes_scored(["Gila"], exclude_ids=[], max_hops=0) == []
