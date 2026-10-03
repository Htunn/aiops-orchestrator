"""Agent-to-Agent (A2A) integration models."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class AgentStatus(StrEnum):
    """Agent health status."""

    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class TaskStatus(StrEnum):
    """Delegated task status."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class AgentCapability(BaseModel):
    """A capability/skill that an agent provides."""

    name: str = Field(..., description="Unique capability identifier")
    description: str = Field(
        ..., description="Human-readable description of what this capability does"
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema for capability parameters",
    )
    examples: list[str] = Field(
        default_factory=list,
        description="Example invocations or use cases",
    )
    estimated_duration_seconds: int | None = Field(
        None,
        description="Typical execution time in seconds",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Tags for categorization (e.g., 'security', 'ml', 'logs')",
    )

    class Config:
        """Pydantic config."""

        json_schema_extra = {
            "example": {
                "name": "analyze_security_logs",
                "description": "Analyze application logs for security threats and vulnerabilities",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "logs": {"type": "string", "description": "Log content to analyze"},
                        "threat_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Types of threats to check for",
                        },
                    },
                    "required": ["logs"],
                },
                "examples": [
                    "Analyze nginx access logs for SQL injection attempts",
                    "Check application logs for authentication failures",
                ],
                "estimated_duration_seconds": 30,
                "tags": ["security", "logs", "analysis"],
            }
        }


class AgentInfo(BaseModel):
    """Information about a registered agent."""

    agent_id: str = Field(..., description="Unique agent identifier")
    name: str = Field(..., description="Human-readable agent name")
    url: HttpUrl = Field(..., description="Base URL for agent API")
    capabilities: list[AgentCapability] = Field(
        default_factory=list,
        description="List of capabilities this agent provides",
    )
    status: AgentStatus = Field(
        default=AgentStatus.UNKNOWN,
        description="Current health status",
    )
    api_key_hash: str | None = Field(
        None,
        description="Hashed API key for authentication (never store plaintext)",
    )
    webhook_url: HttpUrl | None = Field(
        None,
        description="URL to receive async task completion webhooks",
    )
    version: str = Field(
        default="1.0.0",
        description="Agent API version (semantic versioning)",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional agent metadata (owner, team, etc.)",
    )
    registered_at: datetime | None = Field(
        None,
        description="When the agent was registered",
    )
    last_seen: datetime | None = Field(
        None,
        description="Last successful health check or communication",
    )

    class Config:
        """Pydantic config."""

        json_schema_extra = {
            "example": {
                "agent_id": "security-analyzer-v1",
                "name": "Security Analysis Agent",
                "url": "https://security-agent.example.com",
                "capabilities": [
                    {
                        "name": "analyze_security_logs",
                        "description": "Analyze logs for security threats",
                        "parameters": {
                            "type": "object",
                            "properties": {"logs": {"type": "string"}},
                            "required": ["logs"],
                        },
                    }
                ],
                "status": "online",
                "webhook_url": "https://security-agent.example.com/webhook",
                "version": "1.0.0",
                "metadata": {"team": "security", "owner": "security-team@example.com"},
            }
        }


class AgentRegistrationRequest(BaseModel):
    """Request to register an agent."""

    agent_id: str = Field(..., min_length=3, max_length=100)
    name: str = Field(..., min_length=3, max_length=200)
    url: HttpUrl
    capabilities: list[AgentCapability]
    api_key: str = Field(..., min_length=32, description="Secret API key for this agent")
    webhook_url: HttpUrl | None = None
    version: str = Field(default="1.0.0")
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskDelegationRequest(BaseModel):
    """Request to delegate a task to an agent."""

    capability: str = Field(..., description="Name of the capability to invoke")
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters for the capability",
    )
    async_mode: bool = Field(
        default=False,
        description="If true, return immediately and send result via webhook",
    )
    callback_url: HttpUrl | None = Field(
        None,
        description="URL to receive async task completion webhook",
    )
    timeout_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Maximum execution time (5-300 seconds)",
    )
    priority: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Task priority (1=lowest, 10=highest)",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context (incident, logs, metrics, etc.)",
    )


class TaskDelegationResponse(BaseModel):
    """Response from delegating a task."""

    task_id: str = Field(..., description="Unique task identifier")
    status: TaskStatus = Field(..., description="Current task status")
    result: Any | None = Field(None, description="Task result (only for sync tasks)")
    estimated_duration_seconds: int | None = Field(
        None,
        description="Estimated time to completion (for async tasks)",
    )
    message: str | None = Field(None, description="Human-readable status message")


class TaskStatusResponse(BaseModel):
    """Response for querying task status."""

    task_id: str
    status: TaskStatus
    result: Any | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WebhookPayload(BaseModel):
    """Payload sent via webhook when async task completes."""

    task_id: str
    status: TaskStatus
    result: Any | None = None
    error: str | None = None
    duration_seconds: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentHealthResponse(BaseModel):
    """Response from agent health check."""

    agent_id: str
    status: AgentStatus
    uptime_seconds: int | None = None
    active_tasks: int = 0
    capabilities_count: int = 0
    version: str | None = None
    message: str | None = None
