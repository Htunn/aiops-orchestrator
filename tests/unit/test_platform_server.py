"""Unit tests for PlatformMCPServer (SPEC-003)."""

from unittest.mock import AsyncMock, patch

import pytest

from src.mcp.platform_server import TOOL_RISK_LEVELS, PlatformMCPServer


class TestToolDefinitions:
    def test_tools_list_matches_spec_table(self):
        server = PlatformMCPServer()
        names = {tool["name"] for tool in server.tools}
        assert names == set(TOOL_RISK_LEVELS.keys())
        assert len(names) == 6

    def test_every_tool_has_required_schema_fields(self):
        server = PlatformMCPServer()
        for tool in server.tools:
            assert "name" in tool
            assert "description" in tool
            assert tool["inputSchema"]["type"] == "object"

    @pytest.mark.parametrize(
        "tool_name,risk",
        [
            ("platform_list_vms", "low"),
            ("platform_get_vm", "low"),
            ("platform_list_hosts", "low"),
            ("platform_restart_vm", "medium"),
            ("platform_start_vm", "medium"),
            ("platform_stop_vm", "high"),
        ],
    )
    def test_risk_levels_match_spec_table(self, tool_name, risk):
        assert TOOL_RISK_LEVELS[tool_name] == risk


class TestToolsListProtocol:
    @pytest.mark.asyncio
    async def test_handle_tools_list_returns_all_tools(self):
        server = PlatformMCPServer()
        result = await server._handle_tools_list()
        assert len(result["tools"]) == 6

    @pytest.mark.asyncio
    async def test_handle_initialize_sets_initialized_flag(self):
        server = PlatformMCPServer()
        result = await server._handle_initialize({"clientInfo": {"name": "test"}})
        assert server.initialized is True
        assert result["serverInfo"]["name"] == "platform-mcp-server"


class TestToolsCallDispatch:
    @pytest.mark.asyncio
    async def test_unknown_tool_raises(self):
        server = PlatformMCPServer()
        with pytest.raises(ValueError, match="Unknown tool"):
            await server._handle_tools_call({"name": "not_a_real_tool", "arguments": {}})

    @pytest.mark.asyncio
    async def test_call_tool_when_platform_not_found_returns_error_text(self):
        server = PlatformMCPServer()
        mock_registry = AsyncMock()
        mock_registry.get_client = AsyncMock(side_effect=ValueError("Platform not found: nutanix-prod"))

        with patch(
            "src.mcp.platform_server.get_platform_registry",
            AsyncMock(return_value=mock_registry),
        ):
            result = await server._handle_tools_call(
                {"name": "platform_list_vms", "arguments": {"platform_name": "nutanix-prod"}}
            )

        text = result["content"][0]["text"]
        assert "Error" in text
        assert "not found" in text

    @pytest.mark.asyncio
    async def test_call_tool_dispatches_to_client_method(self):
        server = PlatformMCPServer()
        mock_client = AsyncMock()
        mock_vm = type("VM", (), {"__dict__": {"id": "vm-1", "name": "web-01", "power_state": "running"}})()
        mock_client.list_vms = AsyncMock(return_value=[mock_vm])

        mock_registry = AsyncMock()
        mock_registry.get_client = AsyncMock(return_value=mock_client)

        with patch(
            "src.mcp.platform_server.get_platform_registry",
            AsyncMock(return_value=mock_registry),
        ):
            result = await server._handle_tools_call(
                {"name": "platform_list_vms", "arguments": {"platform_name": "nutanix-prod"}}
            )

        mock_registry.get_client.assert_awaited_once_with("nutanix-prod")
        mock_client.list_vms.assert_awaited_once()
        assert "web-01" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_restart_vm_dispatches_with_vm_id(self):
        server = PlatformMCPServer()
        mock_client = AsyncMock()
        mock_client.restart_vm = AsyncMock(return_value=True)

        mock_registry = AsyncMock()
        mock_registry.get_client = AsyncMock(return_value=mock_client)

        with patch(
            "src.mcp.platform_server.get_platform_registry",
            AsyncMock(return_value=mock_registry),
        ):
            result = await server._handle_tools_call(
                {
                    "name": "platform_restart_vm",
                    "arguments": {"platform_name": "vmware-env", "vm_id": "vm-123"},
                }
            )

        mock_client.restart_vm.assert_awaited_once_with("vm-123")
        assert "succeeded" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_stop_vm_passes_force_flag(self):
        server = PlatformMCPServer()
        mock_client = AsyncMock()
        mock_client.stop_vm = AsyncMock(return_value=True)

        mock_registry = AsyncMock()
        mock_registry.get_client = AsyncMock(return_value=mock_client)

        with patch(
            "src.mcp.platform_server.get_platform_registry",
            AsyncMock(return_value=mock_registry),
        ):
            await server._handle_tools_call(
                {
                    "name": "platform_stop_vm",
                    "arguments": {"platform_name": "vmware-env", "vm_id": "vm-123", "force": True},
                }
            )

        mock_client.stop_vm.assert_awaited_once_with("vm-123", True)

    @pytest.mark.asyncio
    async def test_connection_error_surfaces_friendly_message(self):
        server = PlatformMCPServer()
        mock_registry = AsyncMock()
        mock_registry.get_client = AsyncMock(side_effect=ConnectionError("timed out"))

        with patch(
            "src.mcp.platform_server.get_platform_registry",
            AsyncMock(return_value=mock_registry),
        ):
            result = await server._handle_tools_call(
                {"name": "platform_list_hosts", "arguments": {"platform_name": "nutanix-prod"}}
            )

        assert "unable to connect" in result["content"][0]["text"]
