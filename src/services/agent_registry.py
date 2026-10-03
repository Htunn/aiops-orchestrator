"""Agent Registry - Manages discovery and tracking of A2A agents."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select, update

from src.database import get_db_session
from src.database.redis import get_redis
from src.models.agent import AgentCapability, AgentInfo, AgentStatus

logger = structlog.get_logger()


class AgentRegistry:
    """
    Central registry for discovering and tracking AI agents.

    Manages agent registration, capability discovery, health monitoring,
    and routing task delegations to the appropriate agent.
    """

    def __init__(self) -> None:
        self._cache_ttl = 300  # 5 minutes
        self._health_check_interval = 60  # 1 minute

    async def register_agent(
        self,
        agent_id: str,
        name: str,
        url: str,
        capabilities: list[AgentCapability],
        api_key: str,
        webhook_url: str | None = None,
        version: str = "1.0.0",
        metadata: dict[str, Any] | None = None,
    ) -> AgentInfo:
        """
        Register a new agent or update existing registration.

        Args:
            agent_id: Unique agent identifier
            name: Human-readable agent name
            url: Base URL for agent API
            capabilities: List of capabilities the agent provides
            api_key: Secret API key for authentication
            webhook_url: Optional webhook URL for async task results
            version: Agent API version
            metadata: Additional metadata

        Returns:
            AgentInfo object with registration details
        """
        # Hash the API key - never store plaintext
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        agent_info = AgentInfo(
            agent_id=agent_id,
            name=name,
            url=url,
            capabilities=capabilities,
            status=AgentStatus.ONLINE,
            api_key_hash=api_key_hash,
            webhook_url=webhook_url,
            version=version,
            metadata=metadata or {},
            registered_at=datetime.now(UTC),
            last_seen=datetime.now(UTC),
        )

        # Store in database
        async with get_db_session() as session:
            from src.database.models import Agent

            # Check if agent exists
            result = await session.execute(select(Agent).where(Agent.agent_id == agent_id))
            existing = result.scalar_one_or_none()

            if existing:
                # Update existing agent
                existing.name = name
                existing.url = url
                existing.capabilities = [c.model_dump() for c in capabilities]
                existing.status = AgentStatus.ONLINE.value
                existing.api_key_hash = api_key_hash
                existing.webhook_url = webhook_url
                existing.version = version
                existing.agent_metadata = metadata or {}
                existing.last_seen = datetime.now(UTC)
                logger.info("agent_updated", agent_id=agent_id, name=name)
            else:
                # Create new agent
                agent = Agent(
                    agent_id=agent_id,
                    name=name,
                    url=url,
                    capabilities=[c.model_dump() for c in capabilities],
                    status=AgentStatus.ONLINE.value,
                    api_key_hash=api_key_hash,
                    webhook_url=webhook_url,
                    version=version,
                    agent_metadata=metadata or {},
                    registered_at=datetime.now(UTC),
                    last_seen=datetime.now(UTC),
                )
                session.add(agent)
                logger.info("agent_registered", agent_id=agent_id, name=name)

            await session.commit()

        # Cache in Redis
        await self._cache_agent(agent_info)

        return agent_info

    async def get_agent(self, agent_id: str) -> AgentInfo | None:
        """
        Get agent information by ID.

        Checks Redis cache first, falls back to database.

        Args:
            agent_id: Agent identifier

        Returns:
            AgentInfo if found, None otherwise
        """
        # Check cache first
        cached = await self._get_cached_agent(agent_id)
        if cached:
            return cached

        # Fetch from database
        async with get_db_session() as session:
            from src.database.models import Agent

            result = await session.execute(select(Agent).where(Agent.agent_id == agent_id))
            agent = result.scalar_one_or_none()

            if not agent:
                return None

            agent_info = self._db_agent_to_model(agent)

            # Cache for next time
            await self._cache_agent(agent_info)

            return agent_info

    async def list_agents(
        self,
        status_filter: AgentStatus | None = None,
        capability_filter: str | None = None,
    ) -> list[AgentInfo]:
        """
        List all registered agents with optional filtering.

        Args:
            status_filter: Filter by agent status
            capability_filter: Filter by capability name (partial match)

        Returns:
            List of AgentInfo objects
        """
        async with get_db_session() as session:
            from src.database.models import Agent

            query = select(Agent)

            if status_filter:
                query = query.where(Agent.status == status_filter.value)

            result = await session.execute(query)
            agents = result.scalars().all()

            agent_infos = [self._db_agent_to_model(agent) for agent in agents]

            # Apply capability filter if specified
            if capability_filter:
                agent_infos = [
                    agent
                    for agent in agent_infos
                    if any(
                        capability_filter.lower() in cap.name.lower() for cap in agent.capabilities
                    )
                ]

            return agent_infos

    async def find_agents_by_capability(
        self,
        capability_name: str,
        min_score: float = 0.0,
    ) -> list[tuple[AgentInfo, float]]:
        """
        Find agents that provide a specific capability.

        Args:
            capability_name: Capability to search for
            min_score: Minimum match score (0.0-1.0)

        Returns:
            List of (AgentInfo, score) tuples sorted by score descending
        """
        agents = await self.list_agents(status_filter=AgentStatus.ONLINE)
        matches = []

        for agent in agents:
            for capability in agent.capabilities:
                # Simple exact match for now (can be enhanced with fuzzy matching)
                if capability.name.lower() == capability_name.lower():
                    score = 1.0
                    matches.append((agent, score))
                    break
                # Partial match
                elif capability_name.lower() in capability.name.lower():
                    score = 0.7
                    matches.append((agent, score))
                    break

        # Filter by minimum score and sort
        matches = [(agent, score) for agent, score in matches if score >= min_score]
        matches.sort(key=lambda x: x[1], reverse=True)

        return matches

    async def update_agent_status(
        self,
        agent_id: str,
        status: AgentStatus,
        last_seen: datetime | None = None,
    ) -> bool:
        """
        Update agent health status.

        Args:
            agent_id: Agent identifier
            status: New status
            last_seen: Last successful communication time

        Returns:
            True if updated, False if agent not found
        """
        async with get_db_session() as session:
            from src.database.models import Agent

            result = await session.execute(
                update(Agent)
                .where(Agent.agent_id == agent_id)
                .values(
                    status=status.value,
                    last_seen=last_seen or datetime.now(UTC),
                )
            )
            await session.commit()

            if result.rowcount == 0:
                return False

        # Invalidate cache
        await self._invalidate_cache(agent_id)

        logger.info("agent_status_updated", agent_id=agent_id, status=status.value)
        return True

    async def deregister_agent(self, agent_id: str) -> bool:
        """
        Remove an agent from the registry.

        Args:
            agent_id: Agent identifier

        Returns:
            True if removed, False if not found
        """
        async with get_db_session() as session:
            from src.database.models import Agent

            result = await session.execute(select(Agent).where(Agent.agent_id == agent_id))
            agent = result.scalar_one_or_none()

            if not agent:
                return False

            await session.delete(agent)
            await session.commit()

        # Remove from cache
        await self._invalidate_cache(agent_id)

        logger.info("agent_deregistered", agent_id=agent_id)
        return True

    async def get_stale_agents(self, stale_threshold_seconds: int = 300) -> list[str]:
        """
        Get agents that haven't been seen recently.

        Args:
            stale_threshold_seconds: Consider agent stale if not seen in this many seconds

        Returns:
            List of agent IDs
        """
        threshold = datetime.now(UTC) - timedelta(seconds=stale_threshold_seconds)

        async with get_db_session() as session:
            from src.database.models import Agent

            result = await session.execute(
                select(Agent.agent_id).where(Agent.last_seen < threshold)
            )
            agent_ids = [row[0] for row in result.all()]

        return agent_ids

    # ── Cache helpers ─────────────────────────────────────────────────────────

    async def _cache_agent(self, agent_info: AgentInfo) -> None:
        """Store agent info in Redis cache."""
        try:
            redis = get_redis()
            cache_key = f"agent:{agent_info.agent_id}"
            await redis.setex(
                cache_key,
                self._cache_ttl,
                agent_info.model_dump_json(),
            )
        except Exception as e:
            logger.warning("agent_cache_failed", agent_id=agent_info.agent_id, error=str(e))

    async def _get_cached_agent(self, agent_id: str) -> AgentInfo | None:
        """Retrieve agent info from Redis cache."""
        try:
            redis = get_redis()
            cache_key = f"agent:{agent_id}"
            cached = await redis.get(cache_key)
            if cached:
                return AgentInfo.model_validate_json(cached)
        except Exception as e:
            logger.debug("agent_cache_miss", agent_id=agent_id, error=str(e))
        return None

    async def _invalidate_cache(self, agent_id: str) -> None:
        """Remove agent from Redis cache."""
        try:
            redis = get_redis()
            cache_key = f"agent:{agent_id}"
            await redis.delete(cache_key)
        except Exception as e:
            logger.debug("agent_cache_invalidate_failed", agent_id=agent_id, error=str(e))

    # ── Conversion helpers ────────────────────────────────────────────────────

    @staticmethod
    def _db_agent_to_model(agent: Any) -> AgentInfo:
        """Convert database Agent model to AgentInfo."""
        return AgentInfo(
            agent_id=agent.agent_id,
            name=agent.name,
            url=agent.url,
            capabilities=[AgentCapability(**cap) for cap in agent.capabilities],
            status=AgentStatus(agent.status),
            api_key_hash=agent.api_key_hash,
            webhook_url=agent.webhook_url,
            version=agent.version,
            metadata=agent.agent_metadata,
            registered_at=agent.registered_at,
            last_seen=agent.last_seen,
        )


# Global registry instance
_registry: AgentRegistry | None = None


def get_agent_registry() -> AgentRegistry:
    """Get the global agent registry instance."""
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
