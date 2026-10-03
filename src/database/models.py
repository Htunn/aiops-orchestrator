"""Database models for the application."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class User(Base):
    """User model representing users across different channels."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    channel_user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    preferred_model: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="User's preferred AI model"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<User {self.channel_type}:{self.channel_user_id}>"


class Conversation(Base):
    """Conversation/session model."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    channel_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    model_override: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="Override model for this conversation"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_activity: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False, server_default="{}"
    )

    def __repr__(self) -> str:
        return f"<Conversation {self.id} user={self.user_id}>"


class Message(Base):
    """Message model storing conversation history."""

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, comment="user, assistant, system")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_used: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="AI model used for this message"
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False, server_default="{}"
    )

    def __repr__(self) -> str:
        return f"<Message {self.role} in {self.conversation_id}>"


class ChannelConfig(Base):
    """Channel-specific configuration."""

    __tablename__ = "channel_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_type: Mapped[str] = mapped_column(String(20), nullable=False, unique=True, index=True)
    default_model: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Default model for this channel"
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<ChannelConfig {self.channel_type}>"


# ── AIOps Tables ──────────────────────────────────────────────────────────────


class Incident(Base):
    """An operational incident detected by the watchloop or Alertmanager."""

    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="P3", index=True)
    # open | investigating | remediating | resolved
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    namespace: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    resource_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    resource_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    rca_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False, server_default="{}"
    )

    __table_args__ = (Index("ix_incidents_status_created", "status", "created_at"),)

    def __repr__(self) -> str:
        return f"<Incident {self.severity} {self.event_type}/{self.resource_name} [{self.status}]>"


class AlertEvent(Base):
    """An alert event from Alertmanager or the internal watchloop."""

    __tablename__ = "alert_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    rule_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="warning", index=True)
    # firing | resolved
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="firing")
    # prometheus | alertmanager | watchloop | manual
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="watchloop")
    labels: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False, server_default="{}"
    )
    annotations: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False, server_default="{}"
    )
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<AlertEvent {self.rule_name} [{self.status}]>"


class RemediationAction(Base):
    """Audit record of every remediation action (approved or auto-executed)."""

    __tablename__ = "remediation_actions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    playbook_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    playbook_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    resource_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    namespace: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # human | auto
    initiator: Mapped[str] = mapped_column(String(50), nullable=False, default="human")
    # pending | approved | rejected | executing | completed | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False, server_default="{}"
    )

    def __repr__(self) -> str:
        return f"<RemediationAction {self.action_type} [{self.status}]>"


class K8sStateSnapshot(Base):
    """Periodic snapshot of cluster resource state for drift detection."""

    __tablename__ = "k8s_state_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    resource_kind: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    resource_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    spec_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_data: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False, server_default="{}"
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_k8s_snapshot_resource", "namespace", "resource_kind", "resource_name"),
    )


class PlatformConfig(Base):
    """Platform connection configuration for multi-cloud infrastructure."""

    __tablename__ = "platform_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    platform_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, comment="nutanix, vmware, openshift"
    )
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Password stored encrypted (placeholder - use vault/secrets manager in production)
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    token: Mapped[str | None] = mapped_column(Text, nullable=True)
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timeout: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    extra_config: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    last_health_check: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    health_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="healthy, degraded, unreachable"
    )

    def __repr__(self) -> str:
        return f"<PlatformConfig {self.name} ({self.platform_type})>"


class PlatformHealthHistory(Base):
    """Historical health check data for platforms."""

    __tablename__ = "platform_health_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    platform_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    response_time_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (Index("ix_platform_health_platform_time", "platform_id", "checked_at"),)

    def __repr__(self) -> str:
        return f"<PlatformHealthHistory {self.platform_id} {self.status}>"


class PlatformOperation(Base):
    """Audit log for platform operations (VM start/stop, etc.)."""

    __tablename__ = "platform_operations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    platform_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    operation_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, comment="list_vms, start_vm, stop_vm, etc."
    )
    resource_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="vm, host, cluster"
    )
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resource_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    initiator: Mapped[str] = mapped_column(
        String(50), nullable=False, default="system", comment="system, user, ai_agent"
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    # pending | executing | completed | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False, server_default="{}"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<PlatformOperation {self.operation_type} [{self.status}]>"


class AuditLog(Base):
    """Audit trail for all destructive AIOps operations."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    # auto for automated actions
    initiator: Mapped[str] = mapped_column(String(100), nullable=False, default="unknown")
    action: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    resource_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    resource_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    namespace: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # success | failed | rejected
    result: Mapped[str] = mapped_column(String(20), nullable=False, default="success")
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False, server_default="{}"
    )

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} by {self.initiator} [{self.result}]>"


class ApprovalAuditLog(Base):
    """Persistent audit trail for every human-in-the-loop approval lifecycle event.

    Unlike Redis (TTL-limited), this record survives indefinitely for compliance.
    """

    __tablename__ = "approval_audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    approval_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(10), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(20), nullable=False)
    channel_target: Mapped[str] = mapped_column(String(255), nullable=False)
    # requested | approved | rejected | expired | executed | failed
    event_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    actor: Mapped[str | None] = mapped_column(String(255), nullable=True)  # who approved/rejected
    playbook_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    incident_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    tool_params: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False, server_default="{}"
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_approval_audit_approval_time", "approval_id", "timestamp"),)

    def __repr__(self) -> str:
        return f"<ApprovalAuditLog {self.approval_id[:8]} {self.event_type} by {self.actor or self.requested_by}>"


# ── Agent-to-Agent (A2A) Models ───────────────────────────────────────────────


class Agent(Base):
    """Registered AI agent for A2A integration."""

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    capabilities: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="unknown", index=True
    )
    api_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    version: Mapped[str] = mapped_column(String(20), nullable=False, server_default="1.0.0")
    agent_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return f"<Agent {self.agent_id} {self.status}>"


class AgentTask(Base):
    """Delegated task to another agent."""

    __tablename__ = "agent_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    from_agent_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    to_agent_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    capability: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="queued", index=True
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    async_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    callback_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (Index("idx_agent_tasks_status_created", "status", "created_at"),)

    def __repr__(self) -> str:
        return f"<AgentTask {self.task_id} {self.status}>"


class AgentMessage(Base):
    """A2A communication message log."""

    __tablename__ = "agent_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    from_agent_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    to_agent_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    message_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (Index("idx_agent_messages_task_timestamp", "task_id", "timestamp"),)

    def __repr__(self) -> str:
        return f"<AgentMessage {self.message_type} {self.from_agent_id}->{self.to_agent_id}>"
