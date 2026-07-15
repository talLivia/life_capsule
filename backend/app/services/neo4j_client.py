"""
Neo4j AuraDB connection helper.

Plain bolt-driver connectivity check, deliberately independent of Graphiti
(added on top of this in Prompt 3's `graph_memory.py`) — this lets the raw
connection/credentials be verified via `/health` before the heavier
graphiti-core dependency and its own driver management enter the picture.
"""

import logging
from typing import Optional

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.config import settings

logger = logging.getLogger(__name__)


class Neo4jClient:
    def __init__(self):
        self.driver: Optional[AsyncDriver] = None

    async def initialize(self):
        if not settings.NEO4J_URI or not settings.NEO4J_PASSWORD:
            logger.warning(
                "Neo4j not configured (NEO4J_URI/NEO4J_PASSWORD empty) — skipping connect"
            )
            return
        try:
            self.driver = AsyncGraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
            )
            await self.driver.verify_connectivity()
            logger.info("Neo4j connected successfully")
        except Exception as e:
            logger.error(f"Failed to connect to Neo4j: {e}")
            self.driver = None

    async def ping(self) -> bool:
        """Cheap liveness check for the /health endpoint."""
        if not self.driver:
            return False
        try:
            await self.driver.verify_connectivity()
            return True
        except Exception as e:
            logger.warning(f"Neo4j ping failed: {e}")
            return False

    async def cleanup(self):
        if self.driver:
            await self.driver.close()
            self.driver = None
            logger.info("Neo4j connection closed")


# Global instance
neo4j_client = Neo4jClient()
