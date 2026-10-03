"""
Azure MCP Server - stdio-based MCP server for Azure Resource Manager (ARM) operations.

Implements the MCP specification for Azure resource management (SPEC-002),
exposing ARM operations as MCP tools backed by ``src.azure.client.AzureResourceClient``.
Mirrors ``src/mcp/kubernetes_server.py``'s structure and conventions.
"""

import asyncio
import logging
import sys
from typing import Any

import structlog

from src.azure.client import (
    AzureAuthorizationError,
    AzureNotFoundError,
    AzureResourceClient,
    AzureScopeError,
    AzureThrottledError,
)
from src.mcp.stdio_transport import StdioTransport

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

# Risk tier per tool, per SPEC-002 API Contracts table. Consumed by
# src/services/azure_handler.py to decide whether ApprovalManager gating is required.
TOOL_RISK_LEVELS: dict[str, str] = {
    "azure_list_resource_groups": "low",
    "azure_list_vms": "low",
    "azure_get_vm": "low",
    "azure_list_vmss": "low",
    "azure_list_app_services": "low",
    "azure_list_aks_clusters": "low",
    "azure_resource_health": "low",
    "azure_activity_log": "low",
    "azure_resource_metrics": "low",
    "azure_restart_vm": "medium",
    "azure_scale_vmss": "medium",
    "azure_scale_app_service_plan": "medium",
    "azure_restart_aks_nodepool": "medium",
    "azure_deallocate_vm": "high",
    "azure_delete_resource": "high",
}


class AzureMCPServer:
    """
    MCP Server for Azure Resource Manager operations.

    Protocol methods:
    - initialize: Initialize the server
    - tools/list: List available tools
    - tools/call: Execute a tool
    """

    def __init__(self) -> None:
        self.server_info = {"name": "azure-mcp-server", "version": "1.0.0"}
        self.capabilities: dict[str, Any] = {"tools": {}}
        self.transport = StdioTransport()
        self.initialized = False
        self.tools = self._define_tools()
        logger.info("azure_mcp_server_initialized", tools_count=len(self.tools))

    def _define_tools(self) -> list[dict[str, Any]]:
        """Define available Azure tools with their schemas."""
        rg_prop = {"type": "string", "description": "Azure resource group name"}
        sub_prop = {
            "type": "string",
            "description": "Azure subscription ID (defaults to the first configured subscription)",
        }
        vm_name_prop = {"type": "string", "description": "VM name"}
        resource_id_prop = {"type": "string", "description": "Full ARM resource ID"}
        return [
            {
                "name": "azure_list_resource_groups",
                "description": "List Azure resource groups in scope for a subscription",
                "inputSchema": {
                    "type": "object",
                    "properties": {"subscription_id": sub_prop},
                },
            },
            {
                "name": "azure_list_vms",
                "description": "List Azure VMs in a resource group",
                "inputSchema": {
                    "type": "object",
                    "properties": {"resource_group": rg_prop, "subscription_id": sub_prop},
                    "required": ["resource_group"],
                },
            },
            {
                "name": "azure_get_vm",
                "description": "Get a single Azure VM's details including power state",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "resource_group": rg_prop,
                        "vm_name": vm_name_prop,
                        "subscription_id": sub_prop,
                    },
                    "required": ["resource_group", "vm_name"],
                },
            },
            {
                "name": "azure_list_vmss",
                "description": "List Azure VM Scale Sets in a resource group",
                "inputSchema": {
                    "type": "object",
                    "properties": {"resource_group": rg_prop, "subscription_id": sub_prop},
                    "required": ["resource_group"],
                },
            },
            {
                "name": "azure_list_app_services",
                "description": "List Azure App Services (Web Apps) in a resource group",
                "inputSchema": {
                    "type": "object",
                    "properties": {"resource_group": rg_prop, "subscription_id": sub_prop},
                    "required": ["resource_group"],
                },
            },
            {
                "name": "azure_list_aks_clusters",
                "description": "List AKS clusters, optionally scoped to a resource group",
                "inputSchema": {
                    "type": "object",
                    "properties": {"resource_group": rg_prop, "subscription_id": sub_prop},
                },
            },
            {
                "name": "azure_resource_health",
                "description": "Get the current Azure Resource Health status for a resource",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "resource_id": resource_id_prop,
                        "subscription_id": sub_prop,
                    },
                    "required": ["resource_id"],
                },
            },
            {
                "name": "azure_activity_log",
                "description": "Get recent Azure Activity Log entries for a resource group",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "resource_group": rg_prop,
                        "lookback_hours": {
                            "type": "integer",
                            "description": "How many hours back to query (default: 24)",
                        },
                        "subscription_id": sub_prop,
                    },
                    "required": ["resource_group"],
                },
            },
            {
                "name": "azure_resource_metrics",
                "description": "Get Azure Monitor time-series metrics (e.g. CPU, memory) for a resource",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "resource_id": resource_id_prop,
                        "metric_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Metric names, e.g. ['Percentage CPU']",
                        },
                        "subscription_id": sub_prop,
                    },
                    "required": ["resource_id", "metric_names"],
                },
            },
            {
                "name": "azure_restart_vm",
                "description": "Restart an Azure VM. MEDIUM risk — requires approval.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "resource_group": rg_prop,
                        "vm_name": vm_name_prop,
                        "subscription_id": sub_prop,
                    },
                    "required": ["resource_group", "vm_name"],
                },
            },
            {
                "name": "azure_scale_vmss",
                "description": "Change a VM Scale Set's instance count. MEDIUM risk — requires approval.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "resource_group": rg_prop,
                        "vmss_name": {"type": "string", "description": "VM Scale Set name"},
                        "capacity": {"type": "integer", "description": "Target instance count"},
                        "subscription_id": sub_prop,
                    },
                    "required": ["resource_group", "vmss_name", "capacity"],
                },
            },
            {
                "name": "azure_scale_app_service_plan",
                "description": "Scale an App Service Plan's instance count. MEDIUM risk — requires approval.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "resource_group": rg_prop,
                        "plan_name": {"type": "string", "description": "App Service Plan name"},
                        "capacity": {"type": "integer", "description": "Target instance count"},
                        "subscription_id": sub_prop,
                    },
                    "required": ["resource_group", "plan_name", "capacity"],
                },
            },
            {
                "name": "azure_restart_aks_nodepool",
                "description": "Rolling-restart an AKS node pool (ARM-only). MEDIUM risk — requires approval.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "resource_group": rg_prop,
                        "cluster_name": {"type": "string", "description": "AKS cluster name"},
                        "nodepool_name": {"type": "string", "description": "Node pool name"},
                        "subscription_id": sub_prop,
                    },
                    "required": ["resource_group", "cluster_name", "nodepool_name"],
                },
            },
            {
                "name": "azure_deallocate_vm",
                "description": "Stop/deallocate an Azure VM. HIGH risk — requires explicit approval.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "resource_group": rg_prop,
                        "vm_name": vm_name_prop,
                        "subscription_id": sub_prop,
                    },
                    "required": ["resource_group", "vm_name"],
                },
            },
            {
                "name": "azure_delete_resource",
                "description": "Delete an ARM resource by ID. HIGH risk — requires explicit approval.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "resource_id": resource_id_prop,
                        "subscription_id": sub_prop,
                    },
                    "required": ["resource_id"],
                },
            },
        ]

    async def start(self) -> None:
        """Start the MCP server on stdio."""
        logger.info("starting_azure_mcp_server")
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
            "azure_list_resource_groups": self._azure_list_resource_groups,
            "azure_list_vms": self._azure_list_vms,
            "azure_get_vm": self._azure_get_vm,
            "azure_list_vmss": self._azure_list_vmss,
            "azure_list_app_services": self._azure_list_app_services,
            "azure_list_aks_clusters": self._azure_list_aks_clusters,
            "azure_resource_health": self._azure_resource_health,
            "azure_activity_log": self._azure_activity_log,
            "azure_resource_metrics": self._azure_resource_metrics,
            "azure_restart_vm": self._azure_restart_vm,
            "azure_scale_vmss": self._azure_scale_vmss,
            "azure_scale_app_service_plan": self._azure_scale_app_service_plan,
            "azure_restart_aks_nodepool": self._azure_restart_aks_nodepool,
            "azure_deallocate_vm": self._azure_deallocate_vm,
            "azure_delete_resource": self._azure_delete_resource,
        }

        if not tool_name or tool_name not in handler_map:
            raise ValueError(f"Unknown tool: {tool_name}")

        result = await self._run(handler_map[tool_name](arguments))
        return {"content": [{"type": "text", "text": result}]}

    async def _get_client(self) -> AzureResourceClient:
        client = await AzureResourceClient.get_instance()
        if not client.is_available:
            raise RuntimeError(
                "Azure integration is not configured (config/azure_resources.yml "
                "has no subscriptions, or Entra ID credentials are missing)."
            )
        return client

    async def _run(self, coro: Any) -> str:
        """Execute an AzureResourceClient call, mapping typed errors to tool-friendly text."""
        try:
            result = await coro
            return str(result)
        except AzureAuthorizationError as e:
            return f"Error: denied by Azure RBAC — {e}"
        except AzureNotFoundError as e:
            return f"Error: resource not found — {e}"
        except AzureThrottledError as e:
            return f"Error: Azure ARM throttled the request — {e}"
        except AzureScopeError as e:
            return f"Error: out of configured scope — {e}"
        except RuntimeError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error("azure_tool_execution_error", error=str(e))
            return f"Error: {e}"

    # Tool implementations

    async def _azure_list_resource_groups(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(client.list_resource_groups(args.get("subscription_id")))

    async def _azure_list_vms(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(client.list_vms(args["resource_group"], args.get("subscription_id")))

    async def _azure_get_vm(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(
            client.get_vm(args["resource_group"], args["vm_name"], args.get("subscription_id"))
        )

    async def _azure_list_vmss(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(
            client.list_vmss(args["resource_group"], args.get("subscription_id"))
        )

    async def _azure_list_app_services(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(
            client.list_app_services(args["resource_group"], args.get("subscription_id"))
        )

    async def _azure_list_aks_clusters(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(
            client.list_aks_clusters(args.get("resource_group"), args.get("subscription_id"))
        )

    async def _azure_resource_health(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(
            client.resource_health(args["resource_id"], args.get("subscription_id"))
        )

    async def _azure_activity_log(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(
            client.activity_log(
                args["resource_group"],
                args.get("lookback_hours", 24),
                args.get("subscription_id"),
            )
        )

    async def _azure_resource_metrics(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(
            client.resource_metrics(
                args["resource_id"], args["metric_names"], args.get("subscription_id")
            )
        )

    async def _azure_restart_vm(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(
            client.restart_vm(args["resource_group"], args["vm_name"], args.get("subscription_id"))
        )

    async def _azure_scale_vmss(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(
            client.scale_vmss(
                args["resource_group"],
                args["vmss_name"],
                args["capacity"],
                args.get("subscription_id"),
            )
        )

    async def _azure_scale_app_service_plan(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(
            client.scale_app_service_plan(
                args["resource_group"],
                args["plan_name"],
                args["capacity"],
                args.get("subscription_id"),
            )
        )

    async def _azure_restart_aks_nodepool(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(
            client.restart_aks_nodepool(
                args["resource_group"],
                args["cluster_name"],
                args["nodepool_name"],
                args.get("subscription_id"),
            )
        )

    async def _azure_deallocate_vm(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(
            client.deallocate_vm(
                args["resource_group"], args["vm_name"], args.get("subscription_id")
            )
        )

    async def _azure_delete_resource(self, args: dict[str, Any]) -> str:
        client = await self._get_client()
        return await self._run(
            client.delete_resource(args["resource_id"], args.get("subscription_id"))
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
    server = AzureMCPServer()
    await server.start()


if __name__ == "__main__":
    asyncio.run(main())
