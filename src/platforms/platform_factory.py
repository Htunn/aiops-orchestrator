"""
Platform client factory.

Provides factory methods to create platform clients based on configuration.
Supports dynamic client instantiation and platform type detection.
"""

from typing import Type

import structlog

from .base_client import BasePlatformClient, PlatformConfig, PlatformType
from .nutanix_client import NutanixClient
from .openshift_client import OpenShiftClient
from .vmware_client import VMwareClient

logger = structlog.get_logger()


class PlatformFactory:
    """
    Factory for creating platform clients.

    Provides centralized client instantiation based on platform type.
    Supports registration of custom platform clients.

    Example:
        config = PlatformConfig(
            platform_type=PlatformType.NUTANIX,
            endpoint="https://prism-central.example.com:9440",
            username="admin",
            password="secret"
        )

        client = await PlatformFactory.create_client(config)
        async with client:
            vms = await client.list_vms()
    """

    # Registry of platform types to client classes
    _registry: dict[PlatformType, Type[BasePlatformClient]] = {
        PlatformType.NUTANIX: NutanixClient,
        PlatformType.VMWARE: VMwareClient,
        PlatformType.OPENSHIFT: OpenShiftClient,
    }

    @classmethod
    def create_client(cls, config: PlatformConfig) -> BasePlatformClient:
        """
        Create a platform client based on configuration.

        Args:
            config: Platform configuration with type and credentials

        Returns:
            Platform client instance (not yet initialized)

        Raises:
            ValueError: If platform type is not supported

        Example:
            config = PlatformConfig(
                platform_type=PlatformType.VMWARE,
                endpoint="https://vcenter.example.com",
                username="admin",
                password="secret"
            )
            client = PlatformFactory.create_client(config)
            await client.initialize()
        """
        client_class = cls._registry.get(config.platform_type)

        if not client_class:
            raise ValueError(
                f"Unsupported platform type: {config.platform_type}. "
                f"Supported types: {list(cls._registry.keys())}"
            )

        logger.info(
            "creating_platform_client",
            platform=config.platform_type.value,
            endpoint=config.endpoint,
        )

        return client_class(config)

    @classmethod
    async def create_and_initialize(cls, config: PlatformConfig) -> BasePlatformClient:
        """
        Create and initialize a platform client in one step.

        Args:
            config: Platform configuration

        Returns:
            Initialized platform client ready for use

        Raises:
            ValueError: If platform type is not supported
            ConnectionError: If client initialization fails

        Example:
            config = PlatformConfig(...)
            client = await PlatformFactory.create_and_initialize(config)
            vms = await client.list_vms()
            await client.close()
        """
        client = cls.create_client(config)
        await client.initialize()
        return client

    @classmethod
    def register_platform(
        cls, platform_type: PlatformType, client_class: Type[BasePlatformClient]
    ) -> None:
        """
        Register a custom platform client.

        Allows extending the factory with custom platform implementations.

        Args:
            platform_type: Platform type identifier
            client_class: Client class implementing BasePlatformClient

        Example:
            class CustomClient(BasePlatformClient):
                ...

            PlatformFactory.register_platform(
                PlatformType.CUSTOM,
                CustomClient
            )
        """
        if not issubclass(client_class, BasePlatformClient):
            raise ValueError(f"Client class must inherit from BasePlatformClient: {client_class}")

        cls._registry[platform_type] = client_class
        logger.info(
            "platform_registered",
            platform=platform_type.value,
            client_class=client_class.__name__,
        )

    @classmethod
    def get_supported_platforms(cls) -> list[PlatformType]:
        """
        Get list of supported platform types.

        Returns:
            List of registered platform types
        """
        return list(cls._registry.keys())

    @classmethod
    def is_platform_supported(cls, platform_type: PlatformType) -> bool:
        """
        Check if a platform type is supported.

        Args:
            platform_type: Platform type to check

        Returns:
            True if platform is supported, False otherwise
        """
        return platform_type in cls._registry
