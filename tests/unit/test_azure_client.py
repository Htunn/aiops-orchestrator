"""Unit tests for AzureResourceClient (SPEC-002)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from src.azure.client import (
    AzureAuthorizationError,
    AzureNotFoundError,
    AzureResourceClient,
    AzureScopeError,
    AzureThrottledError,
    _SubscriptionContext,
)


def _make_client(resource_group_scope: list[str] | None = None) -> AzureResourceClient:
    """Build an AzureResourceClient with a pre-populated subscription, bypassing SDK imports."""
    client = AzureResourceClient()
    client._initialized = True
    sub = _SubscriptionContext(
        subscription_id="sub-1",
        display_name="production",
        resource_group_scope=resource_group_scope or [],
        credential=MagicMock(),
    )
    client._subscriptions["sub-1"] = sub
    return client, sub


class TestAvailability:
    def test_not_initialized_by_default(self):
        client = AzureResourceClient()
        assert client.is_available is False

    def test_available_once_initialized(self):
        client, _ = _make_client()
        assert client.is_available is True


class TestScopeEnforcement:
    @pytest.mark.asyncio
    async def test_list_vms_rejects_out_of_scope_resource_group(self):
        client, sub = _make_client(resource_group_scope=["prod-rg"])
        sub.compute_client = MagicMock()

        with pytest.raises(AzureScopeError):
            await client.list_vms("other-rg")

    @pytest.mark.asyncio
    async def test_list_vms_allows_in_scope_resource_group(self):
        client, sub = _make_client(resource_group_scope=["prod-rg"])
        compute_client = MagicMock()

        async def _vm_iter(_rg):
            vm = MagicMock()
            vm.name = "web-01"
            vm.location = "eastus"
            vm.hardware_profile.vm_size = "Standard_D2s_v3"
            vm.tags = {}
            yield vm

        compute_client.virtual_machines.list = _vm_iter
        sub.compute_client = compute_client

        result = await client.list_vms("prod-rg")
        assert result == [
            {
                "name": "web-01",
                "location": "eastus",
                "vm_size": "Standard_D2s_v3",
                "power_state": None,
                "tags": {},
            }
        ]

    @pytest.mark.asyncio
    async def test_unconfigured_subscription_raises_scope_error(self):
        client, _ = _make_client()
        with pytest.raises(AzureScopeError):
            await client.list_vms("prod-rg", subscription_id="unknown-sub")


class TestNameValidation:
    @pytest.mark.asyncio
    async def test_rejects_invalid_resource_group_name(self):
        client, _ = _make_client()
        with pytest.raises(ValueError):
            await client.list_vms("bad/rg; rm -rf")

    @pytest.mark.asyncio
    async def test_rejects_invalid_vm_name(self):
        client, sub = _make_client()
        sub.compute_client = MagicMock()
        with pytest.raises(ValueError):
            await client.restart_vm("prod-rg", "bad name!")


class TestResourceGroups:
    @pytest.mark.asyncio
    async def test_list_resource_groups_filters_by_scope(self):
        client, sub = _make_client(resource_group_scope=["prod-rg"])
        resource_client = MagicMock()

        async def _rg_iter():
            for name in ("prod-rg", "dev-rg"):
                rg = MagicMock()
                rg.name = name
                rg.location = "eastus"
                rg.tags = {}
                yield rg

        resource_client.resource_groups.list = _rg_iter
        sub.resource_client = resource_client

        result = await client.list_resource_groups()
        assert [rg["name"] for rg in result] == ["prod-rg"]


class TestMutatingOperations:
    @pytest.mark.asyncio
    async def test_restart_vm_awaits_lro_to_completion(self):
        client, sub = _make_client(resource_group_scope=["prod-rg"])
        compute_client = MagicMock()
        poller = AsyncMock()
        poller.result = AsyncMock(return_value=None)
        compute_client.virtual_machines.begin_restart = AsyncMock(return_value=poller)
        sub.compute_client = compute_client

        result = await client.restart_vm("prod-rg", "web-01")

        compute_client.virtual_machines.begin_restart.assert_awaited_once_with(
            "prod-rg", "web-01"
        )
        poller.result.assert_awaited_once()
        assert result["status"] == "succeeded"
        assert result["vm_name"] == "web-01"

    @pytest.mark.asyncio
    async def test_scale_vmss_rejects_negative_capacity(self):
        client, sub = _make_client()
        sub.compute_client = MagicMock()
        with pytest.raises(ValueError):
            await client.scale_vmss("prod-rg", "my-vmss", -1)


class TestErrorMapping:
    @pytest.mark.asyncio
    async def test_403_maps_to_authorization_error(self):
        client, sub = _make_client()
        compute_client = MagicMock()
        error = HttpResponseError(message="Forbidden")
        error.status_code = 403
        compute_client.virtual_machines.get = AsyncMock(side_effect=error)
        sub.compute_client = compute_client

        with pytest.raises(AzureAuthorizationError):
            await client.get_vm("prod-rg", "web-01")

    @pytest.mark.asyncio
    async def test_404_maps_to_not_found_error(self):
        client, sub = _make_client()
        compute_client = MagicMock()
        compute_client.virtual_machines.get = AsyncMock(
            side_effect=ResourceNotFoundError(message="not found")
        )
        sub.compute_client = compute_client

        with pytest.raises(AzureNotFoundError):
            await client.get_vm("prod-rg", "web-01")

    @pytest.mark.asyncio
    async def test_persistent_429_maps_to_throttled_error(self):
        client, sub = _make_client()
        compute_client = MagicMock()
        error = HttpResponseError(message="Too Many Requests")
        error.status_code = 429
        compute_client.virtual_machines.get = AsyncMock(side_effect=error)
        sub.compute_client = compute_client

        with pytest.raises(AzureThrottledError):
            await client.get_vm("prod-rg", "web-01")

        # 1 initial attempt + 3 retries = 4 calls
        assert compute_client.virtual_machines.get.await_count == 4
