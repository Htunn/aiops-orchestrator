"""
Azure resource management handler — natural language command parsing and
risk-gated execution for Azure ARM operations (SPEC-002).

LOW-risk read tools execute immediately against AzureResourceClient.
MEDIUM/HIGH-risk tools are always routed through ApprovalManager — if the
approval system is unavailable, the action is blocked rather than silently
executed (fail closed).
"""

import re
from typing import Any

import structlog

from src.azure.client import (
    AzureAuthorizationError,
    AzureNotFoundError,
    AzureResourceClient,
    AzureScopeError,
    AzureThrottledError,
)
from src.mcp.azure_server import TOOL_RISK_LEVELS

logger = structlog.get_logger()

# Azure keywords for detection — mirrors the K8S_KEYWORDS/PLATFORM_KEYWORDS
# convention in src/services/message_handler.py.
AZURE_KEYWORDS = [
    "azure",
    "resource group",
    "vmss",
    "scale set",
    "app service",
    "aks cluster",
    "aks clusters",
    "managed cluster",
    "subscription",
    "arm resource",
]

_RG_RE = re.compile(r"(?:in|from|on)\s+(?:resource\s+group\s+)?([a-zA-Z0-9][a-zA-Z0-9._()-]{0,89})")
_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._()-]{0,89}")

_USAGE = (
    "Try: 'list resource groups', 'list vms in <rg>', 'get vm <name> in <rg>', "
    "'restart vm <name> in <rg>', 'deallocate vm <name> in <rg>', "
    "'scale vmss <name> to <n> in <rg>', 'scale app service plan <name> to <n> in <rg>', "
    "'list aks clusters', 'delete resource <resource_id>'."
)


class AzureHandler:
    """
    Handles Azure ARM operations via natural language chat commands.

    Usage:
        handler = AzureHandler()
        handler.approval_manager = approval_manager  # set once ApprovalManager is ready
        response = await handler.handle_query(
            "restart vm web-01 in prod-rg",
            requested_by=user_id, channel_type="slack", channel_target=chat_id,
            send_message_callback=router.send_message,
        )
    """

    def __init__(self) -> None:
        self.approval_manager: Any = None  # Set by MessageHandler/main.py once available
        logger.info("azure_handler_initialized")

    def is_azure_query(self, message: str) -> bool:
        """Check if a message is an Azure-related query."""
        message_lower = message.lower()
        if not any(keyword in message_lower for keyword in AZURE_KEYWORDS):
            return False
        action_verbs = [
            "list",
            "show",
            "get",
            "restart",
            "scale",
            "deallocate",
            "stop",
            "delete",
            "status",
            "health",
        ]
        return any(verb in message_lower for verb in action_verbs)

    async def handle_query(
        self,
        message: str,
        requested_by: str,
        channel_type: str,
        channel_target: str,
        send_message_callback: Any = None,
    ) -> str:
        """Parse a natural-language Azure command and execute it (directly or via approval)."""
        try:
            tool_name, params = self._parse(message)
        except ValueError as e:
            return f"❓ {e}"

        risk = TOOL_RISK_LEVELS.get(tool_name, "high")

        if risk == "low":
            return await self._execute_direct(tool_name, params)

        return await self._request_approval(
            tool_name,
            params,
            risk,
            requested_by,
            channel_type,
            channel_target,
            send_message_callback,
        )

    async def _request_approval(
        self,
        tool_name: str,
        params: dict[str, Any],
        risk: str,
        requested_by: str,
        channel_type: str,
        channel_target: str,
        send_message_callback: Any,
    ) -> str:
        # Fail closed: MEDIUM/HIGH risk Azure actions never execute without ApprovalManager.
        if not self.approval_manager:
            logger.warning("azure_action_blocked_no_approval_manager", tool=tool_name)
            return (
                "⚠️ This Azure action requires human approval, but the approval system "
                "is not currently available. Action blocked for safety."
            )

        from src.services.approval_manager import RiskLevel

        risk_level = RiskLevel.HIGH if risk == "high" else RiskLevel.MEDIUM
        description = self._describe(tool_name, params)
        approval_id = await self.approval_manager.request_approval(
            tool_name=tool_name,
            tool_params=params,
            risk_level=risk_level,
            description=description,
            requested_by=requested_by,
            channel_type=channel_type,
            channel_target=channel_target,
            send_message_callback=send_message_callback,
        )
        return f"⏳ Approval requested (`{approval_id[:8]}`) for: {description}"

    async def _execute_direct(self, tool_name: str, params: dict[str, Any]) -> str:
        """Execute a LOW-risk read tool directly against AzureResourceClient."""
        client = await AzureResourceClient.get_instance()
        if not client.is_available:
            return (
                "⚠️ Azure integration is not configured (no subscriptions in "
                "config/azure_resources.yml, or missing Entra ID credentials)."
            )

        method_name = tool_name.removeprefix("azure_")
        method = getattr(client, method_name, None)
        if method is None:
            return f"❓ Unknown Azure tool: {tool_name}"

        try:
            result = await method(**params)
            return f"✅ {tool_name}:\n```\n{result}\n```"
        except AzureAuthorizationError:
            return "❌ Denied by Azure RBAC — the orchestrator's identity lacks permission for this resource."
        except AzureNotFoundError:
            return "❌ Resource not found."
        except AzureThrottledError:
            return "❌ Azure ARM throttled the request. Please retry shortly."
        except AzureScopeError as e:
            return f"❌ {e}"
        except ValueError as e:
            return f"❓ {e}"
        except Exception as e:
            logger.error("azure_direct_execution_error", tool=tool_name, error=str(e))
            return f"❌ Azure operation failed: {e}"

    @staticmethod
    def _describe(tool_name: str, params: dict[str, Any]) -> str:
        """Human-readable description for the approval prompt."""
        if tool_name == "azure_restart_vm":
            return f"Restart VM `{params['vm_name']}` in `{params['resource_group']}`"
        if tool_name == "azure_deallocate_vm":
            return f"Deallocate (stop) VM `{params['vm_name']}` in `{params['resource_group']}`"
        if tool_name == "azure_scale_vmss":
            return (
                f"Scale VM Scale Set `{params['vmss_name']}` in `{params['resource_group']}` "
                f"to {params['capacity']} instance(s)"
            )
        if tool_name == "azure_scale_app_service_plan":
            return (
                f"Scale App Service Plan `{params['plan_name']}` in `{params['resource_group']}` "
                f"to {params['capacity']} instance(s)"
            )
        if tool_name == "azure_restart_aks_nodepool":
            return (
                f"Restart AKS node pool `{params['nodepool_name']}` on cluster "
                f"`{params['cluster_name']}` in `{params['resource_group']}`"
            )
        if tool_name == "azure_delete_resource":
            return f"Delete Azure resource `{params['resource_id']}`"
        return f"{tool_name} with {params}"

    # ── Natural language parsing ──────────────────────────────────────────

    def _parse(self, message: str) -> tuple[str, dict[str, Any]]:
        text = message.strip().lower()

        if re.search(r"\blist\b.*\bresource\s+groups?\b", text):
            return "azure_list_resource_groups", {}

        if re.search(r"\b(vmss|scale\s+sets?)\b", text) and re.search(r"\blist|show|get\b", text):
            rg = self._require_rg(text)
            return "azure_list_vmss", {"resource_group": rg}

        if "app service" in text and re.search(r"\blist|show|get\b", text):
            rg = self._require_rg(text)
            return "azure_list_app_services", {"resource_group": rg}

        if re.search(r"\baks\b", text) and re.search(r"\blist|show|get\b", text):
            maybe_rg = self._extract_rg(text)
            return "azure_list_aks_clusters", {"resource_group": maybe_rg} if maybe_rg else {}

        if re.search(r"\bvms\b", text) and re.search(r"\blist|show|get\b", text):
            rg = self._require_rg(text)
            return "azure_list_vms", {"resource_group": rg}

        scale_vmss_match = re.search(
            r"scale\s+(?:vmss|scale\s+set)\s+([a-z0-9][a-z0-9._()-]*)\s+to\s+(\d+)", text
        )
        if scale_vmss_match:
            rg = self._require_rg(text)
            return "azure_scale_vmss", {
                "resource_group": rg,
                "vmss_name": scale_vmss_match.group(1),
                "capacity": int(scale_vmss_match.group(2)),
            }

        scale_asp_match = re.search(
            r"scale\s+app\s+service\s+plan\s+([a-z0-9][a-z0-9._()-]*)\s+to\s+(\d+)", text
        )
        if scale_asp_match:
            rg = self._require_rg(text)
            return "azure_scale_app_service_plan", {
                "resource_group": rg,
                "plan_name": scale_asp_match.group(1),
                "capacity": int(scale_asp_match.group(2)),
            }

        nodepool_match = re.search(
            r"restart\s+(?:aks\s+)?node\s*pool\s+([a-z0-9][a-z0-9._()-]*)\s+"
            r"(?:on|in)\s+cluster\s+([a-z0-9][a-z0-9._()-]*)",
            text,
        )
        if nodepool_match:
            rg = self._require_rg(text)
            return "azure_restart_aks_nodepool", {
                "resource_group": rg,
                "cluster_name": nodepool_match.group(2),
                "nodepool_name": nodepool_match.group(1),
            }

        deallocate_match = re.search(
            r"(?:deallocate|stop)\s+(?:vm\s+)?([a-z0-9][a-z0-9._()-]*)", text
        )
        if deallocate_match:
            rg = self._require_rg(text)
            return "azure_deallocate_vm", {
                "resource_group": rg,
                "vm_name": deallocate_match.group(1),
            }

        restart_match = re.search(r"restart\s+(?:vm\s+)?([a-z0-9][a-z0-9._()-]*)", text)
        if restart_match:
            rg = self._require_rg(text)
            return "azure_restart_vm", {"resource_group": rg, "vm_name": restart_match.group(1)}

        get_vm_match = re.search(r"(?:get|show|describe)\s+vm\s+([a-z0-9][a-z0-9._()-]*)", text)
        if get_vm_match:
            rg = self._require_rg(text)
            return "azure_get_vm", {"resource_group": rg, "vm_name": get_vm_match.group(1)}

        health_match = re.search(r"(?:health|resource\s+health)(?:\s+of)?\s+(\S+)", text)
        if health_match:
            return "azure_resource_health", {"resource_id": health_match.group(1)}

        activity_match = re.search(r"activity\s+log(?:\s+for)?", text)
        if activity_match:
            rg = self._require_rg(text)
            return "azure_activity_log", {"resource_group": rg}

        delete_match = re.search(r"delete\s+resource\s+(\S+)", text)
        if delete_match:
            return "azure_delete_resource", {"resource_id": delete_match.group(1)}

        raise ValueError(f"Could not parse Azure command. {_USAGE}")

    @staticmethod
    def _extract_rg(text: str) -> str | None:
        match = _RG_RE.search(text)
        return match.group(1) if match else None

    def _require_rg(self, text: str) -> str:
        rg = self._extract_rg(text)
        if not rg:
            raise ValueError(f"Could not identify a resource group. {_USAGE}")
        return rg
