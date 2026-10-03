"""Task Delegator - Select and delegate tasks to appropriate agents."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from src.database.models import AgentTask
from src.database.postgres import get_db_session
from src.exceptions import A2ACapabilityNotFoundError, A2ATaskDelegationError, A2ATimeoutError
from src.models.agent import (
    AgentInfo,
    TaskDelegationRequest,
    TaskDelegationResponse,
    TaskStatus,
    TaskStatusResponse,
    WebhookPayload,
)
from src.services.a2a_client import get_a2a_client
from src.services.agent_registry import get_agent_registry
from src.services.capability_matcher import match_capability
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()


class TaskDelegator:
    """
    Manages task delegation to other agents.

    Responsibilities:
    - Select best agent for a given capability
    - Serialize context for delegation
    - Handle sync vs async delegation
    - Manage timeouts and retries
    """

    def __init__(self):
        self.client = get_a2a_client()
        self.registry = get_agent_registry()

    async def delegate(
        self,
        capability: str,
        parameters: dict[str, Any],
        context: dict[str, Any] | None = None,
        async_mode: bool = False,
        timeout_seconds: int = 30,
        priority: int = 5,
        db: AsyncSession | None = None,
    ) -> TaskDelegationResponse | TaskStatusResponse:
        """
        Delegate a task to the most appropriate agent.

        Args:
            capability: Required capability name
            parameters: Task parameters
            context: Additional context to send with the task
            async_mode: If True, return immediately; if False, wait for completion
            timeout_seconds: Maximum time to wait for task completion
            priority: Task priority (1-10, higher = more urgent)
            db: Database session

        Returns:
            TaskDelegationResponse for async mode, TaskStatusResponse for sync mode

        Raises:
            A2ACapabilityNotFoundError: If no agent has the required capability
            A2ATaskDelegationError: If delegation fails
            A2ATimeoutError: If task times out
        """
        # Get database session
        if db is None:
            async with get_db_session() as db:
                return await self._delegate_internal(
                    capability=capability,
                    parameters=parameters,
                    context=context,
                    async_mode=async_mode,
                    timeout_seconds=timeout_seconds,
                    priority=priority,
                    db=db,
                )
        else:
            return await self._delegate_internal(
                capability=capability,
                parameters=parameters,
                context=context,
                async_mode=async_mode,
                timeout_seconds=timeout_seconds,
                priority=priority,
                db=db,
            )

    async def _delegate_internal(
        self,
        capability: str,
        parameters: dict[str, Any],
        context: dict[str, Any] | None,
        async_mode: bool,
        timeout_seconds: int,
        priority: int,
        db: AsyncSession,
    ) -> TaskDelegationResponse | TaskStatusResponse:
        """Internal delegation logic with database session."""
        # Find agents with the required capability
        agents = await self.registry.find_agents_by_capability(
            capability=capability,
            db=db,
        )

        if not agents:
            raise A2ACapabilityNotFoundError(f"No agents found with capability: {capability}")

        # Select best agent using capability matcher
        selected_agent = await self._select_best_agent(agents, capability, parameters)

        logger.info(
            "a2a_delegating_task",
            agent_id=selected_agent.agent_id,
            capability=capability,
            async_mode=async_mode,
        )

        # Create delegation request
        request = TaskDelegationRequest(
            capability=capability,
            parameters=parameters,
            context=context or {},
            async_mode=async_mode,
            timeout_seconds=timeout_seconds,
            priority=priority,
        )

        # Delegate to the selected agent
        try:
            response = await self.client.delegate_task(
                agent_url=selected_agent.url,
                agent_id=selected_agent.agent_id,
                request=request,
                api_key=selected_agent.api_key_hash,  # Will be used for auth
            )

            # Record task in database
            task = AgentTask(
                task_id=response.task_id,
                from_agent_id="aiops-orchestrator",  # This orchestrator
                to_agent_id=selected_agent.agent_id,
                capability=capability,
                parameters=parameters,
                context=context or {},
                status=response.status.value,
                async_mode=async_mode,
                priority=priority,
                timeout_seconds=timeout_seconds,
                created_at=datetime.now(UTC),
            )
            db.add(task)
            await db.commit()

            # For sync mode, wait for completion
            if not async_mode:
                logger.debug(
                    "a2a_waiting_for_completion",
                    task_id=response.task_id,
                    timeout_seconds=timeout_seconds,
                )

                final_status = await self.client.wait_for_task_completion(
                    agent_url=selected_agent.url,
                    task_id=response.task_id,
                    api_key=selected_agent.api_key_hash,
                    max_wait_seconds=timeout_seconds,
                )

                # Update task in database
                task.status = final_status.status.value
                task.result = final_status.result
                task.error = final_status.error
                task.completed_at = final_status.completed_at or datetime.now(UTC)

                if final_status.started_at:
                    task.started_at = final_status.started_at
                    if task.completed_at:
                        duration = (task.completed_at - task.started_at).total_seconds()
                        task.duration_seconds = duration

                await db.commit()

                return final_status

            # For async mode, return immediately
            return response

        except Exception as e:
            logger.error(
                "a2a_delegation_failed",
                agent_id=selected_agent.agent_id,
                capability=capability,
                error=str(e),
            )
            raise

    async def _select_best_agent(
        self,
        agents: list[AgentInfo],
        capability: str,
        parameters: dict[str, Any],
    ) -> AgentInfo:
        """
        Select the best agent for a task.

        Args:
            agents: List of candidate agents
            capability: Required capability
            parameters: Task parameters

        Returns:
            Selected agent

        Uses capability matcher to score and rank agents.
        """
        if len(agents) == 1:
            return agents[0]

        # Score agents based on capability match
        scored_agents = []
        for agent in agents:
            score = match_capability(
                agent_capabilities=agent.capabilities,
                required_capability=capability,
                parameters=parameters,
            )
            scored_agents.append((score, agent))

        # Sort by score (descending) and return best
        scored_agents.sort(key=lambda x: x[0], reverse=True)
        best_score, best_agent = scored_agents[0]

        logger.debug(
            "a2a_agent_selected",
            agent_id=best_agent.agent_id,
            score=best_score,
            capability=capability,
        )

        return best_agent

    async def cancel_task(
        self,
        task_id: str,
        db: AsyncSession | None = None,
    ) -> bool:
        """
        Cancel a delegated task.

        Args:
            task_id: Task identifier
            db: Database session

        Returns:
            True if task was cancelled successfully

        Raises:
            A2ATaskDelegationError: If cancellation fails
        """

        async def _cancel_internal(db: AsyncSession) -> bool:
            from sqlalchemy import select

            # Fetch task
            result = await db.execute(select(AgentTask).where(AgentTask.task_id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                logger.warning("a2a_cancel_task_not_found", task_id=task_id)
                return False

            # Check if task can be cancelled
            if task.status in {
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
                TaskStatus.CANCELLED.value,
            }:
                logger.warning(
                    "a2a_cancel_invalid_status",
                    task_id=task_id,
                    status=task.status,
                )
                return False

            # Update status
            task.status = TaskStatus.CANCELLED.value
            task.completed_at = datetime.now(UTC)
            await db.commit()

            logger.info("a2a_task_cancelled", task_id=task_id)
            return True

        if db is None:
            async with get_db_session() as db:
                return await _cancel_internal(db)
        else:
            return await _cancel_internal(db)


# Global delegator instance
_delegator: TaskDelegator | None = None


def get_task_delegator() -> TaskDelegator:
    """Get the global task delegator instance."""
    global _delegator
    if _delegator is None:
        _delegator = TaskDelegator()
    return _delegator
