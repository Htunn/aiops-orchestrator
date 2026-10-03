"""Add A2A (Agent-to-Agent) tables.

Revision ID: 003_a2a_tables
Revises: 002_aiops_tables
Create Date: 2026-07-26 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "003_a2a_tables"
down_revision: str | None = "002_aiops_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add tables for A2A integration."""

    # ── agents table ─────────────────────────────────────────────────────────
    op.create_table(
        "agents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("agent_id", sa.String(100), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("capabilities", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("status", sa.String(20), nullable=False, server_default="unknown", index=True),
        sa.Column("api_key_hash", sa.String(64), nullable=True),
        sa.Column("webhook_url", sa.String(500), nullable=True),
        sa.Column("version", sa.String(20), nullable=False, server_default="1.0.0"),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    # ── agent_tasks table ────────────────────────────────────────────────────
    op.create_table(
        "agent_tasks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("task_id", sa.String(100), nullable=False, unique=True, index=True),
        sa.Column(
            "from_agent_id", sa.String(100), nullable=True, index=True
        ),  # Can be null for user-initiated
        sa.Column("to_agent_id", sa.String(100), nullable=False, index=True),
        sa.Column("capability", sa.String(100), nullable=False, index=True),
        sa.Column("parameters", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("context", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued", index=True),
        sa.Column("result", postgresql.JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("async_mode", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("callback_url", sa.String(500), nullable=True),
        sa.Column("priority", sa.Integer, nullable=False, server_default="5"),
        sa.Column("timeout_seconds", sa.Integer, nullable=False, server_default="30"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float, nullable=True),
    )

    # ── agent_messages table ─────────────────────────────────────────────────
    op.create_table(
        "agent_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("message_id", sa.String(100), nullable=False, unique=True, index=True),
        sa.Column("from_agent_id", sa.String(100), nullable=False, index=True),
        sa.Column("to_agent_id", sa.String(100), nullable=False, index=True),
        sa.Column("task_id", sa.String(100), nullable=True, index=True),
        sa.Column(
            "message_type", sa.String(50), nullable=False, index=True
        ),  # request, response, webhook, error
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("http_status", sa.Integer, nullable=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    # Create indexes for common query patterns
    op.create_index("idx_agent_tasks_status_created", "agent_tasks", ["status", "created_at"])
    op.create_index("idx_agent_messages_task_timestamp", "agent_messages", ["task_id", "timestamp"])


def downgrade() -> None:
    """Remove A2A tables."""
    op.drop_index("idx_agent_messages_task_timestamp", table_name="agent_messages")
    op.drop_index("idx_agent_tasks_status_created", table_name="agent_tasks")
    op.drop_table("agent_messages")
    op.drop_table("agent_tasks")
    op.drop_table("agents")
