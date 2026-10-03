"""Configuration settings for the application."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.models.api_backend import ApiBackendConfig

# pydantic-settings parses `.env` for the Settings model but does not export those
# values into os.environ — the YAML config loaders below (load_agents_config,
# load_api_backend_configs, load_azure_resources_config) resolve ${VAR_NAME}
# references via os.getenv(), so .env must also be loaded into the real process
# environment. load_dotenv() never overrides a variable already set in the shell.
load_dotenv()


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # GitHub Models API
    github_token: str | None = Field(
        default=None, description="GitHub fine-grained personal access token"
    )

    # Google Gemini API
    gemini_api_key: str | None = Field(None, description="Google Gemini API key")

    # vLLM Configuration
    vllm_base_url: str | None = Field(
        None,
        description="vLLM server URL (e.g., http://localhost:8000/v1)",
    )
    vllm_api_key: str | None = Field(
        None,
        description="vLLM API key (optional, depends on server config)",
    )

    # Ollama Configuration
    ollama_base_url: str | None = Field(
        default="http://localhost:11434/v1",
        description="Ollama server URL",
    )

    # Telegram Bot
    telegram_token: str | None = Field(None, description="Telegram bot token")

    # Slack Bot
    slack_bot_token: str | None = Field(None, description="Slack bot token")
    slack_signing_secret: str | None = Field(
        None, description="Slack signing secret for webhook verification"
    )

    # MCP Server Configuration
    mcp_server_url: str | None = Field(
        default=None,
        description="MCP (Model Context Protocol) server URL for custom business logic",
    )

    # Database Configuration
    database_url: str = Field(
        default="postgresql+asyncpg://aiagent:aiagent_password@localhost:5432/aiagent",
        description="PostgreSQL database URL",
    )

    # Redis Configuration
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL",
    )

    # Application Settings
    log_level: str = Field(default="INFO", description="Logging level")
    environment: Literal["development", "production"] = Field(
        default="development", description="Environment"
    )

    # FastAPI Server
    api_host: str = Field(default="0.0.0.0", description="API host")
    api_port: int = Field(default=8000, description="API port")

    # Session Configuration
    session_ttl_seconds: int = Field(default=3600, description="Session TTL in seconds (1 hour)")

    # Default AI Model
    default_model: str = Field(
        default="gpt-4",
        description="Default AI model (gpt-4, claude-3-opus, llama-3-70b)",
    )

    # Rate Limiting
    rate_limit_per_minute: int = Field(default=60, description="Rate limit per minute")

    # AIOps - Monitoring
    prometheus_url: str | None = Field(
        None, description="Prometheus server URL (e.g. http://prometheus:9090)"
    )
    grafana_url: str | None = Field(None, description="Grafana server URL")
    grafana_api_key: str | None = Field(None, description="Grafana API key for annotations")

    # AIOps - Watchloop
    k8s_watchloop_interval: int = Field(
        default=30, ge=5, description="Watchloop poll interval in seconds (min 5)"
    )
    k8s_watchloop_enabled: bool = Field(
        default=True, description="Enable K8s watchloop background task"
    )

    # AIOps - Remediation
    auto_remediation_enabled: bool = Field(
        default=False, description="Enable fully automatic remediation (no approval)"
    )
    aiops_notification_channel: str | None = Field(
        None, description="Channel ID/name for AIOps alerts"
    )
    alertmanager_webhook_secret: str | None = Field(
        None, description="Alertmanager webhook secret for validation"
    )

    # Telegram webhook secret (set via setWebhook secret_token param)
    telegram_webhook_secret: str | None = Field(
        None, description="Telegram bot API webhook secret token for request verification"
    )

    # AIOps - Approval gate
    approval_timeout_seconds: int = Field(
        default=300, ge=30, description="Seconds before pending approval auto-cancels (min 30)"
    )

    # AIOps - Timeouts for AI/MCP calls
    rca_timeout_seconds: int = Field(
        default=30, ge=5, description="Timeout for RCA AI completion (seconds)"
    )
    log_ai_timeout_seconds: int = Field(
        default=15, ge=5, description="Timeout for log AI enrichment (seconds)"
    )
    mcp_tool_timeout_seconds: int = Field(
        default=60, ge=10, description="Timeout for a single MCP tool call (seconds)"
    )

    # Platform Authentication - Nutanix
    nutanix_endpoint: str | None = Field(None, description="Nutanix Prism Central endpoint")
    nutanix_username: str | None = Field(None, description="Nutanix username")
    nutanix_password: str | None = Field(None, description="Nutanix password")
    nutanix_verify_ssl: bool = Field(True, description="Verify Nutanix SSL certificates")

    # Platform Authentication - VMware
    vmware_endpoint: str | None = Field(None, description="VMware vCenter endpoint")
    vmware_username: str | None = Field(None, description="VMware username")
    vmware_password: str | None = Field(None, description="VMware password")
    vmware_verify_ssl: bool = Field(True, description="Verify VMware SSL certificates")

    # Platform Authentication - Kubernetes
    k8s_endpoint: str | None = Field(None, description="Kubernetes API server endpoint")
    k8s_token: str | None = Field(None, description="Kubernetes bearer token")
    k8s_verify_ssl: bool = Field(True, description="Verify Kubernetes SSL certificates")

    # Platform Authentication - OpenShift
    openshift_endpoint: str | None = Field(None, description="OpenShift API server endpoint")
    openshift_token: str | None = Field(None, description="OpenShift bearer token")
    openshift_verify_ssl: bool = Field(True, description="Verify OpenShift SSL certificates")

    # AIOps - Input limits
    max_log_bytes: int = Field(
        default=10_485_760,
        ge=1024,
        description="Maximum log size accepted by log analyzer (bytes, default 10 MB)",
    )

    # OpenTelemetry
    otel_enabled: bool = Field(
        default=False, description="Enable OpenTelemetry distributed tracing"
    )
    otel_service_name: str = Field(
        default="aiops-orchestrator", description="OTel service.name resource attribute"
    )
    otlp_endpoint: str | None = Field(
        None, description="OTLP gRPC endpoint, e.g. http://jaeger:4317"
    )
    otel_sample_rate: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Fraction of traces to sample (0.0-1.0)"
    )

    # AIOps - API Backend Monitoring
    api_backends_config_path: str = Field(
        default="config/api_backends.yml",
        description="Path to API backends configuration file",
    )
    api_backend_monitoring_enabled: bool = Field(
        default=True, description="Enable API backend monitoring watchloop"
    )

    # A2A (Agent-to-Agent) Integration
    a2a_enabled: bool = Field(default=False, description="Enable Agent-to-Agent integration")
    a2a_agent_id: str = Field(
        default="aiops-orchestrator",
        description="Unique identifier for this agent",
    )
    a2a_agent_name: str = Field(
        default="AIOps Orchestrator",
        description="Human-readable name for this agent",
    )
    a2a_agents_config_path: str = Field(
        default="config/agents.yml",
        description="Path to agents registry configuration file",
    )
    a2a_jwt_secret: str = Field(
        default="change-me-in-production-very-secret-key",
        description="Secret key for JWT token signing (use strong random value in production)",
    )
    a2a_webhook_url: str | None = Field(
        None,
        description="Public webhook URL for async task completion callbacks",
    )
    a2a_token_expiry_hours: int = Field(
        default=1,
        ge=1,
        le=24,
        description="JWT token expiry time in hours",
    )

    # Azure Resource Management (SPEC-002)
    azure_integration_enabled: bool = Field(
        default=False, description="Enable Azure ARM resource management integration"
    )
    azure_resources_config_path: str = Field(
        default="config/azure_resources.yml",
        description="Path to Azure subscriptions/resource-group scope configuration file",
    )
    azure_tenant_id: str | None = Field(
        None, description="Entra ID tenant ID (service principal auth)"
    )
    azure_client_id: str | None = Field(None, description="Entra ID application (client) ID")
    azure_client_secret: str | None = Field(None, description="Entra ID application client secret")


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


def load_api_backend_configs(config_path: str | None = None) -> list[ApiBackendConfig]:
    """
    Load API backend configurations from YAML file.

    Supports environment variable substitution in header values using ${VAR_NAME} syntax.
    Returns an empty list if the config file doesn't exist or is malformed.
    """
    if config_path is None:
        settings = get_settings()
        config_path = settings.api_backends_config_path

    # Try relative to project root, then absolute path
    config_file = Path(config_path)
    if not config_file.is_absolute():
        # Assume relative to project root (where alembic.ini is)
        project_root = Path(__file__).parent.parent
        config_file = project_root / config_path

    if not config_file.exists():
        return []

    try:
        with open(config_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data or "api_backends" not in data:
            return []

        backends = []
        for backend_data in data["api_backends"]:
            # Resolve environment variables in header values
            if "headers" in backend_data:
                resolved_headers = {}
                for key, value in backend_data["headers"].items():
                    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                        env_var = value[2:-1]
                        resolved_headers[key] = os.getenv(env_var, "")
                    else:
                        resolved_headers[key] = value
                backend_data["headers"] = resolved_headers

            backends.append(ApiBackendConfig(**backend_data))

        return backends
    except Exception:
        # Return empty list on any error (file not found, parse error, validation error)
        # Errors are logged by the watchloop initialization
        return []


def load_agents_config(config_path: str | None = None) -> dict[str, Any]:
    """
    Load agents configuration from YAML file.

    Supports environment variable substitution in URLs and API keys using ${VAR_NAME} syntax.
    Returns an empty dict if the config file doesn't exist or is malformed.
    """
    if config_path is None:
        settings = get_settings()
        config_path = settings.a2a_agents_config_path

    # Try relative to project root, then absolute path
    config_file = Path(config_path)
    if not config_file.is_absolute():
        project_root = Path(__file__).parent.parent
        config_file = project_root / config_path

    if not config_file.exists():
        return {}

    try:
        with open(config_file, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        # Substitute environment variables in agent URLs and API keys
        for agent in data.get("agents", []):
            for field in ["url", "api_key", "webhook_url"]:
                if field in agent and isinstance(agent[field], str):
                    value = agent[field]
                    # Pattern: ${VAR_NAME} or ${VAR_NAME:-default}
                    if value.startswith("${") and "}" in value:
                        var_spec = value[2 : value.index("}")]
                        # Check for default value syntax: ${VAR:-default}
                        if ":-" in var_spec:
                            var_name, default_val = var_spec.split(":-", 1)
                            agent[field] = os.getenv(var_name, default_val)
                        else:
                            agent[field] = os.getenv(var_spec, "")

        return data

    except Exception:
        # Return empty dict on any error
        return {}


def load_azure_resources_config(config_path: str | None = None) -> dict[str, Any]:
    """
    Load Azure subscriptions/resource-group scope configuration from YAML file.

    Supports environment variable substitution using ${VAR_NAME} or ${VAR_NAME:-default}
    syntax. Returns an empty dict if the config file doesn't exist or is malformed
    (Azure integration then stays disabled — see AzureResourceClient.is_available).
    """
    if config_path is None:
        settings = get_settings()
        config_path = settings.azure_resources_config_path

    config_file = Path(config_path)
    if not config_file.is_absolute():
        project_root = Path(__file__).parent.parent
        config_file = project_root / config_path

    if not config_file.exists():
        return {}

    try:
        with open(config_file, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        def _resolve(value: Any) -> Any:
            if isinstance(value, str) and value.startswith("${") and "}" in value:
                var_spec = value[2 : value.index("}")]
                if ":-" in var_spec:
                    var_name, default_val = var_spec.split(":-", 1)
                    return os.getenv(var_name, default_val)
                return os.getenv(var_spec, "")
            return value

        for sub in data.get("azure", {}).get("subscriptions", []):
            if "subscription_id" in sub:
                sub["subscription_id"] = _resolve(sub["subscription_id"])

        auth = data.get("azure", {}).get("auth", {})
        if "use_managed_identity" in auth:
            resolved = _resolve(auth["use_managed_identity"])
            auth["use_managed_identity"] = (
                str(resolved).lower() in ("true", "1", "yes")
                if isinstance(resolved, str)
                else bool(resolved)
            )

        return data

    except Exception:
        return {}
