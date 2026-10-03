"""Unit tests for AzureMCPServer (SPEC-002)."""

from unittest.mock import AsyncMock, patch

import pytest

from src.mcp.azure_server import TOOL_RISK_LEVELS, AzureMCPServer


class TestToolDefinitions:
    def test_tools_list_matches_spec_table(self):
        server = AzureMCPServer()
        names = {tool["name"] for tool in server.tools}
        assert names == set(TOOL_RISK_LEVELS.keys())
        assert len(names) == 15

    def test_every_tool_has_required_schema_fields(self):
        server = AzureMCPServer()
        for tool in server.tools:
            assert "name" in tool
            assert "description" in tool
            assert tool["inputSchema"]["type"] == "object"

    @pytest.mark.parametrize(
        "tool_name,risk",
        [
            ("azure_list_resource_groups", "low"),
            ("azure_restart_vm", "medium"),
            ("azure_scale_vmss", "medium"),
            ("azure_deallocate_vm", "high"),
            ("azure_delete_resource", "high"),
        ],
    )
    def test_risk_levels_match_spec_table(self, tool_name, risk):
        assert TOOL_RISK_LEVELS[tool_name] == risk


class TestToolsListProtocol:
    @pytest.mark.asyncio
    async def test_handle_tools_list_returns_all_tools(self):
        server = AzureMCPServer()
        result = await server._handle_tools_list()
        assert len(result["tools"]) == 15

    @pytest.mark.asyncio
    async def test_handle_initialize_sets_initialized_flag(self):
        server = AzureMCPServer()
        result = await server._handle_initialize({"clientInfo": {"name": "test"}})
        assert server.initialized is True
        assert result["serverInfo"]["name"] == "azure-mcp-server"


class TestToolsCallDispatch:
    @pytest.mark.asyncio
    async def test_unknown_tool_raises(self):
        server = AzureMCPServer()
        with pytest.raises(ValueError, match="Unknown tool"):
            await server._handle_tools_call({"name": "not_a_real_tool", "arguments": {}})

    @pytest.mark.asyncio
    async def test_call_tool_when_azure_not_configured_returns_error_text(self):
        server = AzureMCPServer()
        mock_client = AsyncMock()
        mock_client.is_available = False

        with patch(
            "src.mcp.azure_server.AzureResourceClient.get_instance",
            AsyncMock(return_value=mock_client),
        ):
            result = await server._handle_tools_call(
                {"name": "azure_list_resource_groups", "arguments": {}}
            )

        text = result["content"][0]["text"]
        assert "Error" in text
        assert "not configured" in text

    @pytest.mark.asyncio
    async def test_call_tool_dispatches_to_client_method(self):
        server = AzureMCPServer()
        mock_client = AsyncMock()
        mock_client.is_available = True
        mock_client.list_resource_groups = AsyncMock(
            return_value=[{"name": "prod-rg", "location": "eastus", "tags": {}}]
        )

        with patch(
            "src.mcp.azure_server.AzureResourceClient.get_instance",
            AsyncMock(return_value=mock_client),
        ):
            result = await server._handle_tools_call(
                {"name": "azure_list_resource_groups", "arguments": {"subscription_id": "sub-1"}}
            )

        mock_client.list_resource_groups.assert_awaited_once_with("sub-1")
        assert "prod-rg" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_authorization_error_surfaces_rbac_message(self):
        from src.azure.client import AzureAuthorizationError

        server = AzureMCPServer()
        mock_client = AsyncMock()
        mock_client.is_available = True
        mock_client.list_vms = AsyncMock(side_effect=AzureAuthorizationError("denied"))

        with patch(
            "src.mcp.azure_server.AzureResourceClient.get_instance",
            AsyncMock(return_value=mock_client),
        ):
            result = await server._handle_tools_call(
                {"name": "azure_list_vms", "arguments": {"resource_group": "prod-rg"}}
            )

        assert "Azure RBAC" in result["content"][0]["text"]
