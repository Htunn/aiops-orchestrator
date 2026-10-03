"""API Backend configuration models."""

from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class ApiBackendConfig(BaseModel):
    """Configuration for a monitored API backend endpoint."""

    name: str = Field(..., description="Unique identifier for this API backend")
    url: HttpUrl = Field(..., description="Base URL of the API endpoint to monitor")
    health_path: str = Field(
        default="/health", description="Health check endpoint path (appended to url)"
    )
    check_interval_seconds: int = Field(
        default=60, ge=10, description="How often to check this endpoint (minimum 10s)"
    )
    timeout_seconds: int = Field(default=10, ge=1, le=60, description="Request timeout in seconds")
    latency_threshold_ms: int = Field(
        default=1000, ge=100, description="Latency threshold in milliseconds (triggers alert)"
    )
    error_rate_threshold: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="Error rate threshold (0.0-1.0, e.g., 0.05 = 5%)",
    )
    consecutive_failures_threshold: int = Field(
        default=2,
        ge=1,
        description="Number of consecutive failures before alerting (prevents flapping)",
    )
    enabled: bool = Field(default=True, description="Whether monitoring is enabled")
    headers: dict[str, str] = Field(
        default_factory=dict, description="Custom HTTP headers (e.g., Authorization)"
    )
    ssl_verify: bool = Field(default=True, description="Verify SSL certificates")
    tags: dict[str, str] = Field(
        default_factory=dict, description="Custom tags/labels for grouping and filtering"
    )

    class Config:
        """Pydantic config."""

        json_schema_extra = {
            "example": {
                "name": "payment-api",
                "url": "https://api.example.com",
                "health_path": "/v1/health",
                "check_interval_seconds": 30,
                "timeout_seconds": 10,
                "latency_threshold_ms": 500,
                "error_rate_threshold": 0.05,
                "consecutive_failures_threshold": 2,
                "enabled": True,
                "headers": {"Authorization": "Bearer ${PAYMENT_API_TOKEN}"},
                "ssl_verify": True,
                "tags": {"team": "platform", "tier": "critical"},
            }
        }


class ApiBackendStatus(BaseModel):
    """Current status of a monitored API backend."""

    name: str
    url: str
    is_up: bool
    last_check_time: str  # ISO 8601 timestamp
    last_latency_ms: float | None = None
    consecutive_failures: int = 0
    error_rate: float = 0.0  # 0.0 to 1.0
    last_error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
