"""Unit tests for AzureWatchLoop and AzureEvent (SPEC-003)."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from src.monitoring.azure_watchloop import AzureEvent, AzureWatchLoop

_SAMPLE_CONFIG = {
    "azure": {
        "subscriptions": [
            {
                "subscription_id": "sub-1",
                "resource_group_scope": ["prod-rg"],
            }
        ],
        "monitoring": {
            "metrics_poll_interval_seconds": 300,
            "activity_log_lookback_hours": 24,
        },
    }
}


def _make_watchloop(event_callback=None) -> AzureWatchLoop:
    with patch(
        "src.monitoring.azure_watchloop.load_azure_resources_config",
        return_value=_SAMPLE_CONFIG,
    ):
        return AzureWatchLoop(event_callback=event_callback or AsyncMock(), interval=5)


# ── AzureEvent ────────────────────────────────────────────────────────────────


class TestAzureEvent:
    def _make_event(self, **kwargs) -> AzureEvent:
        defaults = dict(
            event_type="azure_resource_unhealthy",
            severity="critical",
            resource_group="prod-rg",
            resource_name="web-01",
            resource_type="vm",
            message="Azure vm web-01 is Unavailable",
        )
        defaults.update(kwargs)
        return AzureEvent(**defaults)

    def test_to_dict_has_shared_fields(self):
        event = self._make_event()
        d = event.to_dict()
        for field in ("event_type", "severity", "message", "detected_at", "resource_group"):
            assert field in d, f"Missing field: {field}"

    def test_to_dict_detected_at_is_iso_string(self):
        event = self._make_event()
        d = event.to_dict()
        assert isinstance(d["detected_at"], str)
        datetime.fromisoformat(d["detected_at"])

    def test_to_dict_contains_resource_fields(self):
        event = self._make_event(resource_name="web-01", resource_type="vm")
        d = event.to_dict()
        assert d["resource_name"] == "web-01"
        assert d["resource_type"] == "vm"


# ── AzureWatchLoop lifecycle ───────────────────────────────────────────────────


class TestAzureWatchLoopLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self):
        loop = _make_watchloop()
        await loop.start()
        assert loop.is_running() is True
        await loop.stop()
        assert loop.is_running() is False

    def test_default_interval_from_config(self):
        loop = _make_watchloop()
        assert loop._interval == 5  # explicit interval passed takes precedence


# ── Discovery ──────────────────────────────────────────────────────────────────


class TestResourceDiscovery:
    @pytest.mark.asyncio
    async def test_discover_resources_combines_all_resource_types(self):
        loop = _make_watchloop()
        mock_client = AsyncMock()
        mock_client.list_vms = AsyncMock(return_value=[{"id": "vm-id-1", "name": "web-01"}])
        mock_client.list_app_services = AsyncMock(return_value=[{"id": "app-id-1", "name": "app-01"}])
        mock_client.list_aks_clusters = AsyncMock(return_value=[{"id": "aks-id-1", "name": "aks-01"}])

        with patch(
            "src.monitoring.azure_watchloop.load_azure_resources_config",
            return_value=_SAMPLE_CONFIG,
        ):
            await loop._discover_resources(mock_client)

        assert len(loop._discovered_resources) == 3
        types = {r["resource_type"] for r in loop._discovered_resources}
        assert types == {"vm", "app_service", "aks"}

    @pytest.mark.asyncio
    async def test_discover_resources_skips_resources_without_id(self):
        loop = _make_watchloop()
        mock_client = AsyncMock()
        mock_client.list_vms = AsyncMock(return_value=[{"name": "no-id-vm"}])
        mock_client.list_app_services = AsyncMock(return_value=[])
        mock_client.list_aks_clusters = AsyncMock(return_value=[])

        with patch(
            "src.monitoring.azure_watchloop.load_azure_resources_config",
            return_value=_SAMPLE_CONFIG,
        ):
            await loop._discover_resources(mock_client)

        assert loop._discovered_resources == []

    @pytest.mark.asyncio
    async def test_discover_resources_tolerates_partial_failures(self):
        loop = _make_watchloop()
        mock_client = AsyncMock()
        mock_client.list_vms = AsyncMock(side_effect=RuntimeError("ARM throttled"))
        mock_client.list_app_services = AsyncMock(return_value=[{"id": "app-id-1", "name": "app-01"}])
        mock_client.list_aks_clusters = AsyncMock(return_value=[])

        with patch(
            "src.monitoring.azure_watchloop.load_azure_resources_config",
            return_value=_SAMPLE_CONFIG,
        ):
            await loop._discover_resources(mock_client)

        assert len(loop._discovered_resources) == 1
        assert loop._discovered_resources[0]["resource_type"] == "app_service"


# ── Resource health ────────────────────────────────────────────────────────────


class TestResourceHealthEvents:
    @pytest.mark.asyncio
    async def test_unhealthy_resource_emits_event(self):
        callback = AsyncMock()
        loop = _make_watchloop(event_callback=callback)
        mock_client = AsyncMock()
        mock_client.resource_health = AsyncMock(
            return_value={"availability_state": "Unavailable"}
        )
        resource = {
            "id": "vm-id-1",
            "name": "web-01",
            "resource_group": "prod-rg",
            "resource_type": "vm",
            "subscription_id": "sub-1",
        }

        await loop._check_resource_health(mock_client, resource)

        callback.assert_awaited_once()
        event = callback.call_args[0][0]
        assert event.event_type == "azure_resource_unhealthy"
        assert event.severity == "critical"

    @pytest.mark.asyncio
    async def test_recovered_resource_emits_recovery_event(self):
        callback = AsyncMock()
        loop = _make_watchloop(event_callback=callback)
        loop._resource_states["vm-id-1"] = "Unavailable"
        mock_client = AsyncMock()
        mock_client.resource_health = AsyncMock(return_value={"availability_state": "Available"})
        resource = {
            "id": "vm-id-1",
            "name": "web-01",
            "resource_group": "prod-rg",
            "resource_type": "vm",
            "subscription_id": "sub-1",
        }

        await loop._check_resource_health(mock_client, resource)

        callback.assert_awaited_once()
        event = callback.call_args[0][0]
        assert event.event_type == "azure_resource_recovered"
        assert event.severity == "info"

    @pytest.mark.asyncio
    async def test_unchanged_state_does_not_emit(self):
        callback = AsyncMock()
        loop = _make_watchloop(event_callback=callback)
        loop._resource_states["vm-id-1"] = "Available"
        mock_client = AsyncMock()
        mock_client.resource_health = AsyncMock(return_value={"availability_state": "Available"})
        resource = {
            "id": "vm-id-1",
            "name": "web-01",
            "resource_group": "prod-rg",
            "resource_type": "vm",
            "subscription_id": "sub-1",
        }

        await loop._check_resource_health(mock_client, resource)

        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_health_check_failure_is_swallowed(self):
        callback = AsyncMock()
        loop = _make_watchloop(event_callback=callback)
        mock_client = AsyncMock()
        mock_client.resource_health = AsyncMock(side_effect=RuntimeError("boom"))
        resource = {
            "id": "vm-id-1",
            "name": "web-01",
            "resource_group": "prod-rg",
            "resource_type": "vm",
            "subscription_id": "sub-1",
        }

        await loop._check_resource_health(mock_client, resource)  # should not raise

        callback.assert_not_called()


# ── Activity log / deallocation ────────────────────────────────────────────────


class TestActivityLogEvents:
    @pytest.mark.asyncio
    async def test_deallocate_operation_emits_event(self):
        callback = AsyncMock()
        loop = _make_watchloop(event_callback=callback)
        mock_client = AsyncMock()
        mock_client.activity_log = AsyncMock(
            return_value=[
                {
                    "operation_name": "Microsoft.Compute/virtualMachines/deallocate/action",
                    "status": "Succeeded",
                    "resource_id": "/subscriptions/sub-1/resourceGroups/prod-rg/providers/Microsoft.Compute/virtualMachines/web-01",
                    "event_timestamp": "2026-10-03T00:00:00Z",
                }
            ]
        )

        with patch(
            "src.monitoring.azure_watchloop.load_azure_resources_config",
            return_value=_SAMPLE_CONFIG,
        ):
            await loop._check_activity_logs(mock_client)

        callback.assert_awaited_once()
        event = callback.call_args[0][0]
        assert event.event_type == "azure_vm_deallocated"
        assert event.resource_name == "web-01"

    @pytest.mark.asyncio
    async def test_duplicate_deallocate_entry_only_emits_once(self):
        callback = AsyncMock()
        loop = _make_watchloop(event_callback=callback)
        mock_client = AsyncMock()
        entries = [
            {
                "operation_name": "Microsoft.Compute/virtualMachines/deallocate/action",
                "status": "Succeeded",
                "resource_id": "/subscriptions/sub-1/resourceGroups/prod-rg/providers/Microsoft.Compute/virtualMachines/web-01",
                "event_timestamp": "2026-10-03T00:00:00Z",
            }
        ]
        mock_client.activity_log = AsyncMock(return_value=entries)

        with patch(
            "src.monitoring.azure_watchloop.load_azure_resources_config",
            return_value=_SAMPLE_CONFIG,
        ):
            await loop._check_activity_logs(mock_client)
            await loop._check_activity_logs(mock_client)

        assert callback.await_count == 1

    @pytest.mark.asyncio
    async def test_non_deallocate_operation_ignored(self):
        callback = AsyncMock()
        loop = _make_watchloop(event_callback=callback)
        mock_client = AsyncMock()
        mock_client.activity_log = AsyncMock(
            return_value=[
                {
                    "operation_name": "Microsoft.Compute/virtualMachines/restart/action",
                    "status": "Succeeded",
                    "resource_id": "vm-id-1",
                    "event_timestamp": "2026-10-03T00:00:00Z",
                }
            ]
        )

        with patch(
            "src.monitoring.azure_watchloop.load_azure_resources_config",
            return_value=_SAMPLE_CONFIG,
        ):
            await loop._check_activity_logs(mock_client)

        callback.assert_not_called()
