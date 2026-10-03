"""A2A API Endpoints - REST API for Agent-to-Agent communication."""

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Path, status
from fastapi.responses import JSONResponse

from src.database.models import Agent, AgentMessage, AgentTask
from src.database.postgres import get_db_session
from src.exceptions import (
    A2AAuthenticationError,
    A2ACapabilityNotFoundError,
    A2ARegistrationError,
)
from src.models.agent import (
    AgentHealthResponse,
    AgentInfo,
    AgentRegistrationRequest,
    AgentStatus,
    TaskDelegationRequest,
    TaskDelegationResponse,
    TaskStatus,
    TaskStatusResponse,
    WebhookPayload,
)
from src.services.a2a_auth import get_a2a_auth
from src.services.agent_registry import get_agent_registry
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()
router = APIRouter(prefix="/api/a2a", tags=["A2A"])


# ── Dependencies ──────────────────────────────────────────────────────────────


async def verify_a2a_auth(
    authorization: str | None = Header(None, alias="Authorization"),
) -> str:
    """
    Verify A2A authentication.

    Args:
        authorization: Authorization header value

    Returns:
        Agent ID from validated token

    Raises:
        HTTPException: If authentication fails
    """
    auth = get_a2a_auth()
    try:
        agent_id = auth.validate_request(authorization)
        return agent_id
    except A2AAuthenticationError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


# ── Agent Registration Endpoints ──────────────────────────────────────────────


@router.post("/register", response_model=AgentInfo, status_code=status.HTTP_201_CREATED)
async def register_agent(
    request: AgentRegistrationRequest,
    db: AsyncSession = Depends(get_db_session),
) -> AgentInfo:
    """
    Register a new agent in the A2A network.

    Returns the registered agent info with a generated API key.
    Store the API key securely - it won't be retrievable later.
    """
    registry = get_agent_registry()

    try:
        agent_info, api_key = await registry.register_agent(
            agent_id=request.agent_id,
            name=request.name,
            url=request.url,
            capabilities=request.capabilities,
            webhook_url=request.webhook_url,
            version=request.version,
            metadata=request.metadata,
            db=db,
        )

        logger.info(
            "a2a_agent_registered",
            agent_id=agent_info.agent_id,
            name=agent_info.name,
            capabilities_count=len(agent_info.capabilities),
        )

        # Include API key in response (only returned once)
        response_data = agent_info.model_dump()
        response_data["api_key"] = api_key  # Only shown during registration

        return JSONResponse(
            content=response_data,
            status_code=status.HTTP_201_CREATED,
        )

    except A2ARegistrationError as e:
        logger.error("a2a_registration_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e


@router.get("/agents", response_model=list[AgentInfo])
async def list_agents(
    status_filter: AgentStatus | None = None,
    capability: str | None = None,
    db: AsyncSession = Depends(get_db_session),
) -> list[AgentInfo]:
    """
    List all registered agents.

    Args:
        status_filter: Filter by agent status (online, offline, degraded, unknown)
        capability: Filter by capability name

    Returns:
        List of registered agents
    """
    registry = get_agent_registry()

    agents = await registry.list_agents(
        status=status_filter,
        capability=capability,
        db=db,
    )

    logger.debug(
        "a2a_agents_listed",
        count=len(agents),
        status_filter=status_filter,
        capability=capability,
    )

    return agents


@router.get("/agents/{agent_id}", response_model=AgentInfo)
async def get_agent_info(
    agent_id: str = Path(..., description="Agent identifier"),
    db: AsyncSession = Depends(get_db_session),
) -> AgentInfo:
    """
    Get information about a specific agent.

    Args:
        agent_id: Agent identifier

    Returns:
        Agent information

    Raises:
        HTTPException: If agent not found
    """
    registry = get_agent_registry()

    agent = await registry.get_agent(agent_id=agent_id, db=db)

    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} not found",
        )

    return agent


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deregister_agent(
    agent_id: str = Path(..., description="Agent identifier"),
    caller_agent_id: str = Depends(verify_a2a_auth),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """
    Deregister an agent.

    Only the agent itself can deregister.

    Args:
        agent_id: Agent identifier to deregister
        caller_agent_id: Authenticated agent making the request

    Raises:
        HTTPException: If permission denied or agent not found
    """
    # Only allow agents to deregister themselves
    if caller_agent_id != agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agents can only deregister themselves",
        )

    registry = get_agent_registry()
    success = await registry.deregister_agent(agent_id=agent_id, db=db)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} not found",
        )

    logger.info("a2a_agent_deregistered", agent_id=agent_id)


# ── Task Delegation Endpoints ─────────────────────────────────────────────────


@router.post("/delegate", response_model=TaskDelegationResponse)
async def delegate_task(
    request: TaskDelegationRequest,
    caller_agent_id: str = Depends(verify_a2a_auth),
    db: AsyncSession = Depends(get_db_session),
) -> TaskDelegationResponse:
    """
    Receive a delegated task from another agent.

    This endpoint is called by other agents to delegate tasks to this agent.

    Args:
        request: Task delegation request
        caller_agent_id: Authenticated agent making the request

    Returns:
        Task delegation response with task_id and status
    """
    registry = get_agent_registry()

    # Find matching capability
    agents = await registry.find_agents_by_capability(
        capability=request.capability,
        db=db,
    )

    if not agents:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No agents found with capability: {request.capability}",
        )

    # Create task record
    task_id = str(uuid.uuid4())
    task = AgentTask(
        task_id=task_id,
        from_agent_id=caller_agent_id,
        to_agent_id=agents[0].agent_id,  # Use best match
        capability=request.capability,
        parameters=request.parameters,
        context=request.context or {},
        status=TaskStatus.QUEUED.value,
        async_mode=request.async_mode,
        callback_url=request.callback_url,
        priority=request.priority,
        timeout_seconds=request.timeout_seconds,
        created_at=datetime.now(UTC),
    )

    db.add(task)
    await db.commit()

    # Log message
    message = AgentMessage(
        message_id=str(uuid.uuid4()),
        from_agent_id=caller_agent_id,
        to_agent_id=agents[0].agent_id,
        task_id=task_id,
        message_type="task_delegation",
        payload=request.model_dump(mode="json"),
        http_status=status.HTTP_200_OK,
        timestamp=datetime.now(UTC),
    )
    db.add(message)
    await db.commit()

    logger.info(
        "a2a_task_delegated",
        task_id=task_id,
        from_agent=caller_agent_id,
        to_agent=agents[0].agent_id,
        capability=request.capability,
        async_mode=request.async_mode,
    )

    # For now, return queued status
    # TODO: Implement actual task execution in Phase 3
    return TaskDelegationResponse(
        task_id=task_id,
        status=TaskStatus.QUEUED,
        message=f"Task queued for execution by {agents[0].name}",
    )


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str = Path(..., description="Task identifier"),
    caller_agent_id: str = Depends(verify_a2a_auth),
    db: AsyncSession = Depends(get_db_session),
) -> TaskStatusResponse:
    """
    Get the status of a delegated task.

    Args:
        task_id: Task identifier
        caller_agent_id: Authenticated agent making the request

    Returns:
        Task status response

    Raises:
        HTTPException: If task not found
    """
    from sqlalchemy import select

    # Fetch task from database
    result = await db.execute(select(AgentTask).where(AgentTask.task_id == task_id))
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    # Only allow task creator or assignee to view status
    if caller_agent_id not in {task.from_agent_id, task.to_agent_id}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to view this task",
        )

    return TaskStatusResponse(
        task_id=task.task_id,
        status=TaskStatus(task.status),
        result=task.result,
        error=task.error,
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
    )


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def receive_webhook(
    payload: WebhookPayload,
    caller_agent_id: str = Depends(verify_a2a_auth),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    """
    Receive async task completion webhook.

    Args:
        payload: Webhook payload
        caller_agent_id: Authenticated agent sending the webhook

    Returns:
        Acknowledgement
    """
    from sqlalchemy import select

    # Update task status
    result = await db.execute(select(AgentTask).where(AgentTask.task_id == payload.task_id))
    task = result.scalar_one_or_none()

    if task:
        task.status = payload.status.value
        task.result = payload.result
        task.error = payload.error
        task.completed_at = payload.completed_at or datetime.now(UTC)

        if task.started_at and task.completed_at:
            duration = (task.completed_at - task.started_at).total_seconds()
            task.duration_seconds = duration

        await db.commit()

        logger.info(
            "a2a_webhook_received",
            task_id=payload.task_id,
            status=payload.status,
            from_agent=caller_agent_id,
        )

    return {"status": "acknowledged", "task_id": payload.task_id}
