"""A2A Client - HTTP client for calling other AI agents."""

import asyncio
from typing import Any

import httpx
import structlog

from src.config import get_settings
from src.exceptions import A2ATaskDelegationError, A2ATimeoutError
from src.models.agent import (
    AgentHealthResponse,
    TaskDelegationRequest,
    TaskDelegationResponse,
    TaskStatusResponse,
    WebhookPayload,
)
from src.services.a2a_auth import get_a2a_auth

logger = structlog.get_logger()
settings = get_settings()


class A2AClient:
    """
    HTTP client for making requests to other AI agents.

    Supports:
    - Task delegation (sync and async)
    - Status polling
    - Webhook delivery
    - Automatic retry with exponential backoff
    """

    def __init__(
        self,
        timeout_seconds: int = 30,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
    ):
        """
        Initialize A2A client.

        Args:
            timeout_seconds: Default request timeout
            max_retries: Maximum number of retries for failed requests
            backoff_factor: Exponential backoff multiplier
        """
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.auth = get_a2a_auth()

    async def delegate_task(
        self,
        agent_url: str,
        agent_id: str,
        request: TaskDelegationRequest,
        api_key: str | None = None,
    ) -> TaskDelegationResponse:
        """
        Delegate a task to another agent.

        Args:
            agent_url: Base URL of the target agent
            agent_id: Target agent identifier
            request: Task delegation request
            api_key: API key for authentication (if required)

        Returns:
            Task delegation response

        Raises:
            A2ATaskDelegationError: If delegation fails
            A2ATimeoutError: If request times out
        """
        url = f"{agent_url.rstrip('/')}/api/a2a/delegate"

        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"AIOps-Orchestrator/{settings.a2a_agent_id}",
        }

        # Add authentication if API key provided
        if api_key:
            token = self.auth.create_jwt_token(
                agent_id=settings.a2a_agent_id,
                capabilities=[request.capability],
            )
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    url,
                    json=request.model_dump(mode="json", exclude_none=True),
                    headers=headers,
                )
                response.raise_for_status()

                data = response.json()
                return TaskDelegationResponse(**data)

        except httpx.TimeoutException as e:
            logger.error(
                "a2a_delegation_timeout",
                agent_id=agent_id,
                agent_url=agent_url,
                capability=request.capability,
                error=str(e),
            )
            raise A2ATimeoutError(
                f"Task delegation to {agent_id} timed out after {self.timeout_seconds}s"
            ) from e

        except httpx.HTTPStatusError as e:
            logger.error(
                "a2a_delegation_failed",
                agent_id=agent_id,
                agent_url=agent_url,
                capability=request.capability,
                status_code=e.response.status_code,
                error=str(e),
            )
            raise A2ATaskDelegationError(
                f"Task delegation failed: {e.response.status_code} - {e.response.text}"
            ) from e

        except Exception as e:
            logger.error(
                "a2a_delegation_error",
                agent_id=agent_id,
                agent_url=agent_url,
                capability=request.capability,
                error=str(e),
            )
            raise A2ATaskDelegationError(f"Task delegation error: {e}") from e

    async def get_task_status(
        self,
        agent_url: str,
        task_id: str,
        api_key: str | None = None,
    ) -> TaskStatusResponse:
        """
        Poll task status from another agent.

        Args:
            agent_url: Base URL of the target agent
            task_id: Task identifier
            api_key: API key for authentication (if required)

        Returns:
            Task status response

        Raises:
            A2ATaskDelegationError: If status check fails
        """
        url = f"{agent_url.rstrip('/')}/api/a2a/tasks/{task_id}"

        headers = {
            "User-Agent": f"AIOps-Orchestrator/{settings.a2a_agent_id}",
        }

        if api_key:
            token = self.auth.create_jwt_token(agent_id=settings.a2a_agent_id)
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()

                data = response.json()
                return TaskStatusResponse(**data)

        except Exception as e:
            logger.error(
                "a2a_status_check_failed",
                agent_url=agent_url,
                task_id=task_id,
                error=str(e),
            )
            raise A2ATaskDelegationError(f"Failed to get task status: {e}") from e

    async def send_webhook(
        self,
        webhook_url: str,
        payload: WebhookPayload,
        api_key: str | None = None,
    ) -> bool:
        """
        Send webhook callback for async task completion.

        Args:
            webhook_url: Target webhook URL
            payload: Webhook payload
            api_key: API key for authentication (if required)

        Returns:
            True if webhook delivered successfully

        Raises:
            A2ATaskDelegationError: If webhook delivery fails after retries
        """
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"AIOps-Orchestrator/{settings.a2a_agent_id}",
        }

        if api_key:
            token = self.auth.create_jwt_token(agent_id=settings.a2a_agent_id)
            headers["Authorization"] = f"Bearer {token}"

        for attempt in range(self.max_retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(
                        webhook_url,
                        json=payload.model_dump(mode="json"),
                        headers=headers,
                    )
                    response.raise_for_status()

                    logger.info(
                        "a2a_webhook_delivered",
                        webhook_url=webhook_url,
                        task_id=payload.task_id,
                        status=payload.status,
                    )
                    return True

            except Exception as e:
                wait_seconds = self.backoff_factor**attempt
                logger.warning(
                    "a2a_webhook_failed_retry",
                    webhook_url=webhook_url,
                    task_id=payload.task_id,
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                    wait_seconds=wait_seconds,
                    error=str(e),
                )

                if attempt < self.max_retries - 1:
                    await asyncio.sleep(wait_seconds)
                else:
                    raise A2ATaskDelegationError(
                        f"Webhook delivery failed after {self.max_retries} attempts: {e}"
                    ) from e

        return False

    async def check_agent_health(
        self,
        agent_url: str,
        timeout_seconds: int = 5,
    ) -> AgentHealthResponse | None:
        """
        Check health of another agent.

        Args:
            agent_url: Base URL of the target agent
            timeout_seconds: Health check timeout

        Returns:
            Agent health response, or None if unreachable
        """
        url = f"{agent_url.rstrip('/')}/health"

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.get(url)
                response.raise_for_status()

                data = response.json()
                return AgentHealthResponse(**data)

        except Exception as e:
            logger.debug(
                "a2a_health_check_failed",
                agent_url=agent_url,
                error=str(e),
            )
            return None

    async def wait_for_task_completion(
        self,
        agent_url: str,
        task_id: str,
        api_key: str | None = None,
        poll_interval_seconds: int = 2,
        max_wait_seconds: int = 300,
    ) -> TaskStatusResponse:
        """
        Poll task status until completion or timeout.

        Args:
            agent_url: Base URL of the target agent
            task_id: Task identifier
            api_key: API key for authentication
            poll_interval_seconds: Polling interval
            max_wait_seconds: Maximum time to wait

        Returns:
            Final task status

        Raises:
            A2ATimeoutError: If task doesn't complete within max_wait_seconds
            A2ATaskDelegationError: If polling fails
        """
        elapsed = 0
        terminal_statuses = {"completed", "failed", "timeout", "cancelled"}

        while elapsed < max_wait_seconds:
            status = await self.get_task_status(agent_url, task_id, api_key)

            if status.status in terminal_statuses:
                logger.info(
                    "a2a_task_completed",
                    task_id=task_id,
                    status=status.status,
                    elapsed_seconds=elapsed,
                )
                return status

            await asyncio.sleep(poll_interval_seconds)
            elapsed += poll_interval_seconds

        raise A2ATimeoutError(f"Task {task_id} did not complete within {max_wait_seconds}s")


# Global client instance
_client: A2AClient | None = None


def get_a2a_client() -> A2AClient:
    """Get the global A2A client instance."""
    global _client
    if _client is None:
        _client = A2AClient()
    return _client
