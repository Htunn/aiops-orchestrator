"""Unit tests for AzureHandler (SPEC-002)."""

from unittest.mock import AsyncMock, patch

import pytest

from src.services.azure_handler import AzureHandler


class TestQueryDetection:
    def test_detects_azure_keywords_with_action_verb(self):
        handler = AzureHandler()
        assert handler.is_azure_query("list vms in resource group prod-rg") is True
        assert handler.is_azure_query("restart vm web-01 in resource group prod-rg") is True

    def test_rejects_message_without_action_verb(self):
        handler = AzureHandler()
        assert handler.is_azure_query("azure is a cloud provider") is False

    def test_rejects_unrelated_message(self):
        handler = AzureHandler()
        assert handler.is_azure_query("what's the weather today") is False


class TestLowRiskDirectExecution:
    @pytest.mark.asyncio
    async def test_list_resource_groups_executes_directly(self):
        handler = AzureHandler()
        mock_client = AsyncMock()
        mock_client.is_available = True
        mock_client.list_resource_groups = AsyncMock(return_value=[{"name": "prod-rg"}])

        with patch(
            "src.services.azure_handler.AzureResourceClient.get_instance",
            AsyncMock(return_value=mock_client),
        ):
            response = await handler.handle_query(
                "list resource groups",
                requested_by="user-1",
                channel_type="slack",
                channel_target="C123",
            )

        assert "azure_list_resource_groups" in response
        assert "prod-rg" in response

    @pytest.mark.asyncio
    async def test_not_configured_returns_warning(self):
        handler = AzureHandler()
        mock_client = AsyncMock()
        mock_client.is_available = False

        with patch(
            "src.services.azure_handler.AzureResourceClient.get_instance",
            AsyncMock(return_value=mock_client),
        ):
            response = await handler.handle_query(
                "list vms in prod-rg",
                requested_by="user-1",
                channel_type="slack",
                channel_target="C123",
            )

        assert "not configured" in response

    @pytest.mark.asyncio
    async def test_unparseable_command_returns_usage_hint(self):
        handler = AzureHandler()
        response = await handler.handle_query(
            "azure please do the thing",
            requested_by="user-1",
            channel_type="slack",
            channel_target="C123",
        )
        assert response.startswith("❓")


class TestMediumHighRiskApprovalGating:
    @pytest.mark.asyncio
    async def test_restart_vm_requires_approval(self):
        handler = AzureHandler()
        handler.approval_manager = AsyncMock()
        handler.approval_manager.request_approval = AsyncMock(return_value="abcd1234-full-id")

        response = await handler.handle_query(
            "restart vm web-01 in prod-rg",
            requested_by="user-1",
            channel_type="slack",
            channel_target="C123",
        )

        handler.approval_manager.request_approval.assert_awaited_once()
        call_kwargs = handler.approval_manager.request_approval.call_args.kwargs
        assert call_kwargs["tool_name"] == "azure_restart_vm"
        assert call_kwargs["tool_params"] == {"resource_group": "prod-rg", "vm_name": "web-01"}
        assert "abcd1234" in response

    @pytest.mark.asyncio
    async def test_deallocate_vm_is_high_risk(self):
        from src.services.approval_manager import RiskLevel

        handler = AzureHandler()
        handler.approval_manager = AsyncMock()
        handler.approval_manager.request_approval = AsyncMock(return_value="full-id-12345678")

        await handler.handle_query(
            "deallocate vm web-01 in prod-rg",
            requested_by="user-1",
            channel_type="slack",
            channel_target="C123",
        )

        call_kwargs = handler.approval_manager.request_approval.call_args.kwargs
        assert call_kwargs["risk_level"] == RiskLevel.HIGH

    @pytest.mark.asyncio
    async def test_fails_closed_when_approval_manager_unavailable(self):
        handler = AzureHandler()
        handler.approval_manager = None

        response = await handler.handle_query(
            "restart vm web-01 in prod-rg",
            requested_by="user-1",
            channel_type="slack",
            channel_target="C123",
        )

        assert "blocked for safety" in response


class TestNaturalLanguageParsing:
    def test_parses_scale_vmss_command(self):
        handler = AzureHandler()
        tool_name, params = handler._parse("scale vmss my-vmss to 3 in prod-rg")
        assert tool_name == "azure_scale_vmss"
        assert params == {"resource_group": "prod-rg", "vmss_name": "my-vmss", "capacity": 3}

    def test_parses_delete_resource_command(self):
        handler = AzureHandler()
        tool_name, params = handler._parse(
            "delete resource /subscriptions/x/resourceGroups/prod-rg/providers/Microsoft.Compute/virtualMachines/web-01"
        )
        assert tool_name == "azure_delete_resource"
        assert "resource_id" in params

    def test_missing_resource_group_raises_value_error(self):
        handler = AzureHandler()
        with pytest.raises(ValueError):
            handler._parse("list vms")
