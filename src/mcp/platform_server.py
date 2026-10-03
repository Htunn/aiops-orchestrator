"""
Platform MCP Server - stdio-based MCP server for VM platform operations.

Implements the MCP specification for Nutanix/VMware/OpenShift VM management
(SPEC-003), exposing ``PlatformRegistry``/``BasePlatformClient`` VM operations
as MCP tools so they are playbook-executable like Kubernetes and Azure tools.
Mirrors ``src/mcp/azure_server.py``'s structure and conventions. Kubernetes is
intentionally excluded — it keeps its existing, richer ``kubernetes_server.py``.
"""

import asyncio
import logging
import sys
from typing import Any

import structlog

from src.mcp.stdio_transport import StdioTransport
from src.services.platform_registry import get_platform_registry

# Configure logging to stderr only (stdout is for JSON-RPC)
logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.INFO)

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(colors=False),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

# Risk tier per tool, per SPEC-003 API Contracts table. Consumed by
# ApprovalManager/PlaybookExecutor to decide whether approval gating is required.
TOOL_RISK_LEVELS: dict[str, str] = {
    "platform_list_vms": "low",
    "platform_get_vm": "low",
    "platform_list_hosts": "low",
    "platform_restart_vm": "medium",
    "platform_start_vm": "medium",
    "platform_stop_vm": "high",
}


class PlatformMCPServer:
    """
    MCP Server for VM platform (Nutanix/VMware/OpenShift) operations.

    Protocol methods:
    - initialize: Initialize the server
    - tools/list: List available tools
    - tools/call: Execute a tool
    """

    def __init__(self) -> None:
        self.server_info = {"name": "platform-mcp-server", "version": "1.0.0"}
        self.capabilities: dict[str, Any] = {"tools": {}}
        self.transport = StdioTransport()
        self.initialized = False
        self.tools = self._define_tools()
        logger.info("platform_mcp_server_initialized", tools_count=len(self.tools))

    def _define_tools(self) -> list[dict[str, Any]]:
        """Define available platform tools with their schemas."""
        platform_name_prop = {
            "type": "string",
            "description": "Configured platform name (e.g. nutanix-prod, vmware-env)",
        }
        vm_id_prop = {"type": "string", "description": "Platform-specific VM identifier"}
        return [
            {
                "name": "platform_list_vms",
                "description": "List VMs on a Nutanix/VMware/OpenShift platform",
                "inputSchema": {
                    "type": "object",
                    "properties": {"platform_name": platform_name_prop},
                    "required": ["platform_name"],
                },
            },
            {
                "name": "platform_get_vm",
                "description": "Get a single VM's details including power state",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "platform_name": platform_name_prop,
                        "vm_id": vm_id_prop,
                    },
                    "required": ["platform_name", "vm_id"],
                },
            },
            {
                "name": "platform_list_hosts",
                "description": "List hosts/nodes on a platform",
                "inputSchema": {
                    "type": "object",
                    "properties": {"platform_name": platform_name_prop},
                    "required": ["platform_name"],
                },
            },
            {
                "name": "platform_restart_vm",
                "description": "Restart a VM. MEDIUM risk — requires approval.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "platform_name": platform_name_prop,
                        "vm_id": vm_id_prop,
                    },
                    "required": ["platform_name", "vm_id"],
                },
            },
            {
                "name": "platform_start_vm",
                "description": "Power on a VM. MEDIUM risk — requires approval.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "platform_name": platform_name_prop,
                        "vm_id": vm_id_prop,
                    },
                    "required": ["platform_name", "vm_id"],
                },
            },
            {
                "name": "platform_stop_vm",
                "description": "Power off a VM, optionally forced. HIGH risk — requires explicit approval.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "platform_name": platform_name_prop,
                        "vm_id": vm_id_prop,
                        "force": {
                            "type": "boolean",
                            "description": "Force shutdown without guest OS cooperation",
                        },
                    },
                    "required": ["platform_name", "vm_id"],
                },
            },
        ]

    async def start(self) -> None:
        """Start the MCP server on stdio."""
        logger.info("starting_platform_mcp_server")
        await self.transport.start(self._handle_request)

    async def _handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        method = request.get("method")
        params = request.get("params", {})
        request_id = request.get("id")

        logger.debug("handling_request", method=method, id=request_id)

        try:
            if method == "initialize":
                result = await self._handle_initialize(params)
            elif method == "tools/list":
                result = await self._handle_tools_list()
            elif method == "tools/call":
                result = await self._handle_tools_call(params)
            else:
                return self._create_error_response(
                    request_id, -32601, f"Method not found: {method}"
                )

            return {"jsonrpc": "2.0", "id": request_id, "result": result}

        except Exception as e:
            logger.error("request_handling_error", method=method, error=str(e))
            return self._create_error_response(request_id, -32603, "Internal error", str(e))

    async def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        self.initialized = True
        logger.info("server_initialized", client_info=params.get("clientInfo"))
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": self.capabilities,
            "serverInfo": self.server_info,
        }

    async def _handle_tools_list(self) -> dict[str, Any]:
        logger.debug("listing_tools", count=len(self.tools))
        return {"tools": self.tools}

    async def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        tool_name = params.get("name")
        arguments = params.get("arguments", {}) or {}

        logger.info("calling_tool", tool=tool_name, args=arguments)

        handler_map: dict[str, Any] = {
            "platform_list_vms": self._platform_list_vms,
            "platform_get_vm": self._platform_get_vm,
            "platform_list_hosts": self._platform_list_hosts,
            "platform_restart_vm": self._platform_restart_vm,
            "platform_start_vm": self._platform_start_vm,
            "platform_stop_vm": self._platform_stop_vm,
        }

        if not tool_name or tool_name not in handler_map:
            raise ValueError(f"Unknown tool: {tool_name}")

        result = await self._run(handler_map[tool_name](arguments))
        return {"content": [{"type": "text", "text": result}]}

    async def _get_client(self, platform_name: str) -> Any:
        registry = await get_platform_registry()
        return await registry.get_client(platform_name)

    async def _run(self, coro: Any) -> str:
        """Execute a platform client call, mapping errors to tool-friendly text."""
        try:
            result = await coro
            return str(result)
        except ValueError as e:
            return f"Error: {e}"
        except ConnectionError as e:
            return f"Error: unable to connect to platform — {e}"
        except Exception as e:
            logger.error("platform_tool_execution_error", error=str(e))
            return f"Error: {e}"

    # Tool implementations

    async def _platform_list_vms(self, args: dict[str, Any]) -> str:
        client = await self._get_client(args["platform_name"])
        vms = await client.list_vms()
        return str([vm.__dict__ for vm in vms])

    async def _platform_get_vm(self, args: dict[str, Any]) -> str:
        client = await self._get_client(args["platform_name"])
        vm = await client.get_vm(args["vm_id"])
        return str(vm.__dict__)

    async def _platform_list_hosts(self, args: dict[str, Any]) -> str:
        client = await self._get_client(args["platform_name"])
        hosts = await client.list_hosts()
        return str([host.__dict__ for host in hosts])

    async def _platform_restart_vm(self, args: dict[str, Any]) -> str:
        client = await self._get_client(args["platform_name"])
        ok = await client.restart_vm(args["vm_id"])
        return str(
            {
                "status": "succeeded" if ok else "failed",
                "operation": "restart_vm",
                "vm_id": args["vm_id"],
            }
        )

    async def _platform_start_vm(self, args: dict[str, Any]) -> str:
        client = await self._get_client(args["platform_name"])
        ok = await client.start_vm(args["vm_id"])
        return str(
            {
                "status": "succeeded" if ok else "failed",
                "operation": "start_vm",
                "vm_id": args["vm_id"],
            }
        )

    async def _platform_stop_vm(self, args: dict[str, Any]) -> str:
        client = await self._get_client(args["platform_name"])
        ok = await client.stop_vm(args["vm_id"], args.get("force", False))
        return str(
            {
                "status": "succeeded" if ok else "failed",
                "operation": "stop_vm",
                "vm_id": args["vm_id"],
            }
        )

    def _create_error_response(
        self, request_id: Any, code: int, message: str, data: Any | None = None
    ) -> dict[str, Any]:
        """Create a JSON-RPC error response."""
        response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        if data is not None:
            response["error"]["data"] = data
        return response


async def main() -> None:
    """Main entry point for the MCP server."""
    server = PlatformMCPServer()
    await server.start()


if __name__ == "__main__":
    asyncio.run(main())
