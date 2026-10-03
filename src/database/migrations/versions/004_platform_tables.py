"""Add platform configuration and monitoring tables.

Revision ID: 004_platform_tables
Revises: 003_a2a_tables
Create Date: 2026-07-28

"""

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "004_platform_tables"
down_revision = "003_a2a_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create platform configuration and monitoring tables."""

    # Platform configurations table
    op.create_table(
        "platform_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column(
            "platform_type", sa.String(50), nullable=False, comment="nutanix, vmware, openshift"
        ),
        sa.Column("endpoint", sa.String(500), nullable=False),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("password_encrypted", sa.Text, nullable=True),
        sa.Column("token", sa.Text, nullable=True),
        sa.Column("verify_ssl", sa.Boolean, nullable=False, default=True),
        sa.Column("timeout", sa.Integer, nullable=False, default=30),
        sa.Column("max_retries", sa.Integer, nullable=False, default=3),
        sa.Column("enabled", sa.Boolean, nullable=False, default=True),
        sa.Column("extra_config", postgresql.JSON, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_health_check", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "health_status", sa.String(20), nullable=True, comment="healthy, degraded, unreachable"
        ),
    )

    # Create indexes
    op.create_index("ix_platform_configs_name", "platform_configs", ["name"])
    op.create_index("ix_platform_configs_platform_type", "platform_configs", ["platform_type"])
    op.create_index("ix_platform_configs_enabled", "platform_configs", ["enabled"])

    # Platform health history table
    op.create_table(
        "platform_health_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("platform_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("response_time_ms", sa.Float, nullable=True),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    # Create indexes
    op.create_index("ix_platform_health_platform_id", "platform_health_history", ["platform_id"])
    op.create_index("ix_platform_health_status", "platform_health_history", ["status"])
    op.create_index("ix_platform_health_checked_at", "platform_health_history", ["checked_at"])
    op.create_index(
        "ix_platform_health_platform_time", "platform_health_history", ["platform_id", "checked_at"]
    )

    # Platform operations audit log table
    op.create_table(
        "platform_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("platform_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "operation_type",
            sa.String(50),
            nullable=False,
            comment="list_vms, start_vm, stop_vm, etc.",
        ),
        sa.Column("resource_type", sa.String(50), nullable=True, comment="vm, host, cluster"),
        sa.Column("resource_id", sa.String(255), nullable=True),
        sa.Column("resource_name", sa.String(255), nullable=True),
        sa.Column(
            "initiator",
            sa.String(50),
            nullable=False,
            default="system",
            comment="system, user, ai_agent",
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, default="pending"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSON, nullable=False, server_default="{}"),
        sa.Column("error_message", sa.Text, nullable=True),
    )

    # Create indexes
    op.create_index("ix_platform_operations_platform_id", "platform_operations", ["platform_id"])
    op.create_index(
        "ix_platform_operations_operation_type", "platform_operations", ["operation_type"]
    )
    op.create_index("ix_platform_operations_user_id", "platform_operations", ["user_id"])
    op.create_index("ix_platform_operations_status", "platform_operations", ["status"])
    op.create_index("ix_platform_operations_started_at", "platform_operations", ["started_at"])


def downgrade() -> None:
    """Drop platform configuration and monitoring tables."""
    op.drop_table("platform_operations")
    op.drop_table("platform_health_history")
    op.drop_table("platform_configs")
