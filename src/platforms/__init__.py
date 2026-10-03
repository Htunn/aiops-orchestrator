"""
Platform integration clients for multi-cloud infrastructure management.

This package provides unified interfaces for managing resources across
different cloud and virtualization platforms (Nutanix, VMware, OpenShift).
"""

from .base_client import (
    BasePlatformClient,
    HostResource,
    PlatformConfig,
    PlatformHealth,
    PlatformType,
    VMResource,
)
from .nutanix_client import NutanixClient
from .openshift_client import OpenShiftClient
from .platform_factory import PlatformFactory
from .vmware_client import VMwareClient

__all__ = [
    "BasePlatformClient",
    "PlatformConfig",
    "HostResource",
    "VMResource",
    "PlatformHealth",
    "PlatformType",
    "NutanixClient",
    "VMwareClient",
    "OpenShiftClient",
    "PlatformFactory",
]
