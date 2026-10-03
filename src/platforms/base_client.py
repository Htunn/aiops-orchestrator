"""
Base abstract client for platform integrations.

Defines the common interface that all platform clients must implement
for unified resource management across different infrastructure providers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger()


class PlatformType(str, Enum):  # noqa: UP042 — StrEnum changes str()/f-string output used in error messages (platform_factory.py, models.py); not a safe mechanical fix
    """Supported platform types."""

    KUBERNETES = "kubernetes"
    NUTANIX = "nutanix"
    VMWARE = "vmware"
    OPENSHIFT = "openshift"


@dataclass
class PlatformConfig:
    """Configuration for a platform client."""

    platform_type: PlatformType
    endpoint: str
    username: str | None = None
    password: str | None = None
    token: str | None = None
    verify_ssl: bool = True
    timeout: int = 30
    max_retries: int = 3
    extra: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Validate configuration."""
        if not self.endpoint:
            raise ValueError("Platform endpoint is required")

        # Ensure we have either token or username/password
        if not self.token and not (self.username and self.password):
            raise ValueError(
                "Either token or username/password must be provided for authentication"
            )


@dataclass
class VMResource:
    """Unified VM resource representation across platforms."""

    id: str
    name: str
    power_state: str  # running, stopped, suspended, unknown
    cpu_count: int | None = None
    memory_mb: int | None = None
    host: str | None = None
    cluster: str | None = None
    ip_addresses: list[str] | None = None
    platform: str | None = None
    metadata: dict[str, Any] | None = None

    def is_running(self) -> bool:
        """Check if VM is in running state."""
        return self.power_state.lower() in ("running", "on", "poweredon")


@dataclass
class HostResource:
    """Unified host/node resource representation."""

    id: str
    name: str
    status: str  # ready, not_ready, maintenance, unknown
    cpu_capacity: int | None = None
    memory_capacity_mb: int | None = None
    cpu_usage_percent: float | None = None
    memory_usage_percent: float | None = None
    vm_count: int | None = None
    cluster: str | None = None
    platform: str | None = None
    metadata: dict[str, Any] | None = None

    def is_healthy(self) -> bool:
        """Check if host is in healthy state."""
        return self.status.lower() in ("ready", "connected", "normal")


@dataclass
class PlatformHealth:
    """Health status of a platform connection."""

    platform: str
    status: str  # healthy, degraded, unreachable
    message: str | None = None
    response_time_ms: float | None = None
    last_check: str | None = None


class BasePlatformClient(ABC):
    """
    Abstract base class for all platform clients.

    Defines the common interface for VM management, host monitoring,
    and platform health checks across different infrastructure providers.
    """

    def __init__(self, config: PlatformConfig):
        """
        Initialize platform client.

        Args:
            config: Platform configuration including credentials and endpoint
        """
        self.config = config
        self.platform_type = config.platform_type
        self._initialized = False
        self.logger = logger.bind(platform=config.platform_type.value)

    @abstractmethod
    async def initialize(self) -> None:
        """
        Initialize the platform client connection.

        Establishes authentication and validates connectivity.
        Must be called before using other methods.

        Raises:
            ConnectionError: If unable to connect to platform
            AuthenticationError: If authentication fails
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """
        Close the platform client connection and cleanup resources.
        """
        pass

    @abstractmethod
    async def health_check(self) -> PlatformHealth:
        """
        Check platform health and connectivity.

        Returns:
            PlatformHealth: Current health status of the platform
        """
        pass

    # VM Management Methods

    @abstractmethod
    async def list_vms(
        self,
        cluster: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[VMResource]:
        """
        List virtual machines.

        Args:
            cluster: Optional cluster name to filter VMs
            filters: Optional platform-specific filters

        Returns:
            List of VM resources
        """
        pass

    @abstractmethod
    async def get_vm(self, vm_id: str) -> VMResource:
        """
        Get details of a specific VM.

        Args:
            vm_id: Platform-specific VM identifier

        Returns:
            VM resource details

        Raises:
            NotFoundError: If VM doesn't exist
        """
        pass

    @abstractmethod
    async def start_vm(self, vm_id: str) -> bool:
        """
        Power on a VM.

        Args:
            vm_id: Platform-specific VM identifier

        Returns:
            True if operation succeeded

        Raises:
            NotFoundError: If VM doesn't exist
            OperationError: If start operation fails
        """
        pass

    @abstractmethod
    async def stop_vm(self, vm_id: str, force: bool = False) -> bool:
        """
        Power off a VM.

        Args:
            vm_id: Platform-specific VM identifier
            force: If True, force shutdown without guest OS cooperation

        Returns:
            True if operation succeeded

        Raises:
            NotFoundError: If VM doesn't exist
            OperationError: If stop operation fails
        """
        pass

    @abstractmethod
    async def restart_vm(self, vm_id: str) -> bool:
        """
        Restart a VM.

        Args:
            vm_id: Platform-specific VM identifier

        Returns:
            True if operation succeeded

        Raises:
            NotFoundError: If VM doesn't exist
            OperationError: If restart operation fails
        """
        pass

    # Host/Node Management Methods

    @abstractmethod
    async def list_hosts(
        self,
        cluster: str | None = None,
    ) -> list[HostResource]:
        """
        List hosts/nodes in the platform.

        Args:
            cluster: Optional cluster name to filter hosts

        Returns:
            List of host resources
        """
        pass

    @abstractmethod
    async def get_host(self, host_id: str) -> HostResource:
        """
        Get details of a specific host.

        Args:
            host_id: Platform-specific host identifier

        Returns:
            Host resource details

        Raises:
            NotFoundError: If host doesn't exist
        """
        pass

    # Cluster Methods

    @abstractmethod
    async def list_clusters(self) -> list[dict[str, Any]]:
        """
        List clusters/resource pools in the platform.

        Returns:
            List of cluster information dicts
        """
        pass

    # Context Manager Support

    async def __aenter__(self) -> "BasePlatformClient":
        """Async context manager entry."""
        await self.initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        """Async context manager exit."""
        await self.close()
        return False
