"""
Platform registry service.

Manages platform configurations, client instantiation, and health monitoring.
Provides centralized access to all configured infrastructure platforms.

Configuration sources (in priority order):
1. Database (PlatformConfig table) - production default
2. Environment variables - local development, CI/CD, Docker

Environment variable format:
  NUTANIX_ENDPOINT, NUTANIX_USERNAME, NUTANIX_PASSWORD, NUTANIX_VERIFY_SSL
  VMWARE_ENDPOINT, VMWARE_USERNAME, VMWARE_PASSWORD, VMWARE_VERIFY_SSL
  K8S_ENDPOINT, K8S_TOKEN, K8S_VERIFY_SSL
  OPENSHIFT_ENDPOINT, OPENSHIFT_TOKEN, OPENSHIFT_VERIFY_SSL
"""

import asyncio
import os
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database import get_db_session
from src.database.models import PlatformConfig as PlatformConfigModel
from src.database.models import PlatformHealthHistory, PlatformOperation
from src.platforms import (
    BasePlatformClient,
    PlatformConfig,
    PlatformFactory,
    PlatformHealth,
    PlatformType,
)

logger = structlog.get_logger()


class PlatformRegistry:
    """
    Manages platform client connections and configurations.

    Provides:
    - Load platform configs from database
    - Create and cache platform clients
    - Health check monitoring
    - Operation audit logging

    Example:
        registry = PlatformRegistry()
        await registry.initialize()

        # Get a platform client
        client = await registry.get_client("production-nutanix")
        vms = await client.list_vms()

        # Close all connections
        await registry.close_all()
    """

    def __init__(self):
        """Initialize platform registry."""
        self._clients: dict[str, BasePlatformClient] = {}
        self._configs: dict[str, PlatformConfigModel] = {}
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """
        Initialize registry by loading platform configs from database and environment.

        Priority order:
        1. Database configurations (production)
        2. Environment variables (local dev, CI/CD)

        Loads all enabled platform configurations but doesn't create clients
        until they're requested (lazy initialization).
        """
        async with self._lock:
            if self._initialized:
                return

            # Load from database first
            async with get_db_session() as session:
                # Load enabled platform configs
                result = await session.execute(
                    select(PlatformConfigModel).where(PlatformConfigModel.enabled == True)
                )
                configs = result.scalars().all()

                for config in configs:
                    self._configs[config.name] = config
                    logger.info(
                        "platform_config_loaded_from_db",
                        platform_name=config.name,
                        platform_type=config.platform_type,
                    )

            # Load from environment variables (fallback for local dev)
            env_configs = self._load_env_configs()
            for name, config_model in env_configs.items():
                if name not in self._configs:
                    self._configs[name] = config_model
                    logger.info(
                        "platform_config_loaded_from_env",
                        platform_name=name,
                        platform_type=config_model.platform_type,
                    )

            self._initialized = True
            logger.info("platform_registry_initialized", platform_count=len(self._configs))

    def _load_env_configs(self) -> dict[str, PlatformConfigModel]:
        """
        Load platform configurations from environment variables.

        Returns:
            Dictionary of platform name to PlatformConfigModel instances
        """
        settings = get_settings()
        env_configs = {}

        # Nutanix from env
        if settings.nutanix_endpoint:
            env_configs["nutanix-env"] = PlatformConfigModel(
                name="nutanix-env",
                platform_type="nutanix",
                endpoint=settings.nutanix_endpoint,
                username=settings.nutanix_username,
                password_encrypted=settings.nutanix_password,
                verify_ssl=settings.nutanix_verify_ssl,
                enabled=True,
                timeout=30,
                max_retries=3,
            )

        # VMware from env
        if settings.vmware_endpoint:
            env_configs["vmware-env"] = PlatformConfigModel(
                name="vmware-env",
                platform_type="vmware",
                endpoint=settings.vmware_endpoint,
                username=settings.vmware_username,
                password_encrypted=settings.vmware_password,
                verify_ssl=settings.vmware_verify_ssl,
                enabled=True,
                timeout=30,
                max_retries=3,
            )

        # Kubernetes from env
        if settings.k8s_endpoint:
            env_configs["kubernetes-env"] = PlatformConfigModel(
                name="kubernetes-env",
                platform_type="kubernetes",
                endpoint=settings.k8s_endpoint,
                token=settings.k8s_token,
                verify_ssl=settings.k8s_verify_ssl,
                enabled=True,
                timeout=30,
                max_retries=3,
            )

        # OpenShift from env
        if settings.openshift_endpoint:
            env_configs["openshift-env"] = PlatformConfigModel(
                name="openshift-env",
                platform_type="openshift",
                endpoint=settings.openshift_endpoint,
                token=settings.openshift_token,
                verify_ssl=settings.openshift_verify_ssl,
                enabled=True,
                timeout=30,
                max_retries=3,
            )

        return env_configs

    async def reload_configs(self) -> None:
        """
        Reload platform configurations from database.

        Useful when configurations are updated dynamically.
        """
        logger.info("reloading_platform_configs")

        # Close existing clients
        await self.close_all()

        # Reload configs
        self._initialized = False
        await self.initialize()

    async def get_client(self, platform_name: str) -> BasePlatformClient:
        """
        Get or create a platform client.

        Args:
            platform_name: Name of the platform configuration

        Returns:
            Initialized platform client

        Raises:
            ValueError: If platform not found or disabled
            ConnectionError: If client initialization fails
        """
        if not self._initialized:
            await self.initialize()

        # Check if client already exists
        if platform_name in self._clients:
            return self._clients[platform_name]

        # Get config from registry
        config_model = self._configs.get(platform_name)
        if not config_model:
            raise ValueError(f"Platform not found: {platform_name}")

        if not config_model.enabled:
            raise ValueError(f"Platform disabled: {platform_name}")

        # Create platform config from model
        platform_config = self._model_to_config(config_model)

        # Create and initialize client
        try:
            client = await PlatformFactory.create_and_initialize(platform_config)

            # Cache client
            async with self._lock:
                self._clients[platform_name] = client

            logger.info(
                "platform_client_created",
                platform_name=platform_name,
                platform_type=config_model.platform_type,
            )

            return client

        except Exception as e:
            logger.error(
                "platform_client_creation_failed",
                platform_name=platform_name,
                error=str(e),
            )
            raise ConnectionError(f"Failed to create platform client: {e}") from e

    def _model_to_config(self, model: PlatformConfigModel) -> PlatformConfig:
        """
        Convert database model to platform config.

        Args:
            model: Database model

        Returns:
            Platform configuration

        Note: In production, decrypt password_encrypted using a secrets manager
        """
        # Map string to enum
        platform_type = PlatformType(model.platform_type)

        # TODO: Decrypt password in production
        password = model.password_encrypted

        return PlatformConfig(
            platform_type=platform_type,
            endpoint=model.endpoint,
            username=model.username,
            password=password,
            token=model.token,
            verify_ssl=model.verify_ssl,
            timeout=model.timeout,
            max_retries=model.max_retries,
            extra=model.extra_config,
        )

    async def get_all_clients(self) -> dict[str, BasePlatformClient]:
        """
        Get all enabled platform clients.

        Returns:
            Dictionary mapping platform names to clients
        """
        if not self._initialized:
            await self.initialize()

        clients = {}
        for platform_name in self._configs.keys():
            try:
                client = await self.get_client(platform_name)
                clients[platform_name] = client
            except Exception as e:
                logger.warning(
                    "failed_to_get_platform_client",
                    platform_name=platform_name,
                    error=str(e),
                )

        return clients

    async def check_health(self, platform_name: str) -> PlatformHealth:
        """
        Check health of a specific platform.

        Args:
            platform_name: Name of the platform

        Returns:
            Platform health status

        Raises:
            ValueError: If platform not found
        """
        client = await self.get_client(platform_name)
        health = await client.health_check()

        # Store health check result in database
        await self._store_health_check(platform_name, health)

        return health

    async def check_all_health(self) -> dict[str, PlatformHealth]:
        """
        Check health of all enabled platforms.

        Returns:
            Dictionary mapping platform names to health status
        """
        if not self._initialized:
            await self.initialize()

        health_results = {}
        for platform_name in self._configs.keys():
            try:
                health = await self.check_health(platform_name)
                health_results[platform_name] = health
            except Exception as e:
                logger.error(
                    "health_check_failed",
                    platform_name=platform_name,
                    error=str(e),
                )
                health_results[platform_name] = PlatformHealth(
                    platform=platform_name,
                    status="unreachable",
                    message=str(e),
                )

        return health_results

    async def _store_health_check(self, platform_name: str, health: PlatformHealth) -> None:
        """Store health check result in database."""
        config = self._configs.get(platform_name)
        if not config:
            return

        async with get_db_session() as session:
            # Update platform config
            config.health_status = health.status
            config.last_health_check = health.last_check
            session.add(config)

            # Store history
            history = PlatformHealthHistory(
                platform_id=config.id,
                status=health.status,
                response_time_ms=health.response_time_ms,
                message=health.message,
            )
            session.add(history)

            await session.commit()

    async def log_operation(
        self,
        platform_name: str,
        operation_type: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        resource_name: str | None = None,
        initiator: str = "system",
        user_id: UUID | None = None,
        status: str = "completed",
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        """
        Log a platform operation to the audit trail.

        Args:
            platform_name: Name of the platform
            operation_type: Type of operation (e.g., "start_vm", "list_vms")
            resource_type: Type of resource (e.g., "vm", "host")
            resource_id: Platform-specific resource ID
            resource_name: Human-readable resource name
            initiator: Who initiated the operation (system, user, ai_agent)
            user_id: User ID if initiated by a user
            status: Operation status (completed, failed, etc.)
            result: Operation result data
            error_message: Error message if failed
        """
        config = self._configs.get(platform_name)
        if not config:
            logger.warning("platform_not_found_for_logging", platform_name=platform_name)
            return

        async with get_db_session() as session:
            operation = PlatformOperation(
                platform_id=config.id,
                operation_type=operation_type,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_name=resource_name,
                initiator=initiator,
                user_id=user_id,
                status=status,
                result=result or {},
                error_message=error_message,
            )
            session.add(operation)
            await session.commit()

        logger.info(
            "platform_operation_logged",
            platform_name=platform_name,
            operation_type=operation_type,
            resource_name=resource_name,
            status=status,
        )

    async def close_all(self) -> None:
        """Close all platform client connections."""
        async with self._lock:
            for platform_name, client in self._clients.items():
                try:
                    await client.close()
                    logger.info("platform_client_closed", platform_name=platform_name)
                except Exception as e:
                    logger.warning(
                        "platform_client_close_failed",
                        platform_name=platform_name,
                        error=str(e),
                    )

            self._clients.clear()

    async def list_platforms(self) -> list[dict[str, Any]]:
        """
        List all configured platforms.

        Returns:
            List of platform information dicts
        """
        if not self._initialized:
            await self.initialize()

        platforms = []
        for name, config in self._configs.items():
            platforms.append(
                {
                    "name": name,
                    "type": config.platform_type,
                    "endpoint": config.endpoint,
                    "enabled": config.enabled,
                    "health_status": config.health_status,
                    "last_health_check": config.last_health_check.isoformat()
                    if config.last_health_check
                    else None,
                }
            )

        return platforms

    def get_platform_names(self) -> list[str]:
        """
        Get list of all platform names.

        Returns:
            List of platform names
        """
        return list(self._configs.keys())

    def is_platform_available(self, platform_name: str) -> bool:
        """
        Check if a platform is configured and enabled.

        Args:
            platform_name: Name of the platform

        Returns:
            True if platform is available, False otherwise
        """
        config = self._configs.get(platform_name)
        return config is not None and config.enabled


# Singleton instance
_registry: PlatformRegistry | None = None


async def get_platform_registry() -> PlatformRegistry:
    """
    Get the singleton platform registry instance.

    Returns:
        Initialized platform registry
    """
    global _registry

    if _registry is None:
        _registry = PlatformRegistry()
        await _registry.initialize()

    return _registry
