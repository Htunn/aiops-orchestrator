"""Azure Resource Manager integration (SPEC-002)."""

from src.azure.client import (
    AzureAuthorizationError,
    AzureNotFoundError,
    AzureResourceClient,
    AzureScopeError,
    AzureThrottledError,
)

__all__ = [
    "AzureResourceClient",
    "AzureAuthorizationError",
    "AzureNotFoundError",
    "AzureScopeError",
    "AzureThrottledError",
]
