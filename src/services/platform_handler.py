"""
Platform handler service.

Executes platform operations (VM start/stop, host management, etc.) and provides
natural language command parsing for AI integration.
"""

import re
from typing import Any
from uuid import UUID

import structlog

from src.platforms import VMResource
from src.services.platform_registry import get_platform_registry

logger = structlog.get_logger()


class PlatformHandler:
    """
    Handles platform operations with natural language support.

    Provides methods to:
    - Execute VM power operations (start, stop, restart)
    - List and query VMs across platforms
    - Monitor host/node health
    - Parse natural language commands

    Example:
        handler = PlatformHandler()

        # Execute command
        result = await handler.execute_command(
            "restart the production-api VM",
            user_id=user_id
        )

        # Direct operation
        result = await handler.start_vm(
            platform_name="production-nutanix",
            vm_id="abc-123",
            user_id=user_id
        )
    """

    # Platform-related keywords
    PLATFORM_KEYWORDS = [
        "vm",
        "vms",
        "virtual machine",
        "virtual machines",
        "nutanix",
        "vmware",
        "vcenter",
        "esxi",
        "prism",
        "openshift",
        "host",
        "hosts",
        "hypervisor",
        "cluster",
        "clusters",
        "datacenter",
        "infrastructure",
        # Operations
        "start",
        "stop",
        "restart",
        "reboot",
        "power on",
        "power off",
        "shutdown",
        "boot",
        "list",
        "show",
        "get",
        "status",
    ]

    def __init__(self):
        """Initialize platform handler."""
        pass

    async def execute_command(
        self,
        command: str,
        user_id: UUID | None = None,
    ) -> dict[str, Any]:
        """
        Execute a natural language platform command.

        Args:
            command: Natural language command
            user_id: User ID for audit logging

        Returns:
            Operation result dictionary

        Example commands:
            - "restart the production-api VM"
            - "start the database VM on nutanix"
            - "stop VM vm-123"
            - "list all VMs"
            - "show VM status for production cluster"
        """
        command_lower = command.lower()

        # Parse operation type
        if any(word in command_lower for word in ["restart", "reboot"]):
            return await self._handle_restart_command(command, user_id)
        elif any(word in command_lower for word in ["start", "power on", "boot"]):
            return await self._handle_start_command(command, user_id)
        elif any(word in command_lower for word in ["stop", "power off", "shutdown"]):
            return await self._handle_stop_command(command, user_id)
        elif (
            any(word in command_lower for word in ["list", "show", "get"]) and "vm" in command_lower
        ):
            return await self._handle_list_vms_command(command, user_id)
        elif any(word in command_lower for word in ["list", "show"]) and "host" in command_lower:
            return await self._handle_list_hosts_command(command, user_id)
        else:
            return {
                "success": False,
                "error": "Unable to parse platform command. Try: 'start VM <name>', 'list VMs', etc.",
            }

    async def _handle_restart_command(self, command: str, user_id: UUID | None) -> dict[str, Any]:
        """Handle VM restart command."""
        # Extract VM identifier
        vm_identifier = self._extract_vm_identifier(command)
        if not vm_identifier:
            return {
                "success": False,
                "error": "Could not identify VM name or ID. Try: 'restart VM <name>'",
            }

        # Find VM across platforms
        platform_name, vm = await self._find_vm(vm_identifier)
        if not vm:
            return {
                "success": False,
                "error": f"VM not found: {vm_identifier}",
            }

        # Restart VM
        try:
            registry = await get_platform_registry()
            client = await registry.get_client(platform_name)
            success = await client.restart_vm(vm.id)

            # Log operation
            await registry.log_operation(
                platform_name=platform_name,
                operation_type="restart_vm",
                resource_type="vm",
                resource_id=vm.id,
                resource_name=vm.name,
                initiator="ai_agent",
                user_id=user_id,
                status="completed" if success else "failed",
            )

            return {
                "success": success,
                "operation": "restart_vm",
                "platform": platform_name,
                "vm_id": vm.id,
                "vm_name": vm.name,
                "message": f"VM '{vm.name}' restarted successfully on {platform_name}",
            }

        except Exception as e:
            logger.error("restart_vm_failed", vm_name=vm.name, error=str(e))

            # Log failure
            registry = await get_platform_registry()
            await registry.log_operation(
                platform_name=platform_name,
                operation_type="restart_vm",
                resource_type="vm",
                resource_id=vm.id,
                resource_name=vm.name,
                initiator="ai_agent",
                user_id=user_id,
                status="failed",
                error_message=str(e),
            )

            return {
                "success": False,
                "error": f"Failed to restart VM: {e}",
            }

    async def _handle_start_command(self, command: str, user_id: UUID | None) -> dict[str, Any]:
        """Handle VM start command."""
        vm_identifier = self._extract_vm_identifier(command)
        if not vm_identifier:
            return {
                "success": False,
                "error": "Could not identify VM name or ID. Try: 'start VM <name>'",
            }

        platform_name, vm = await self._find_vm(vm_identifier)
        if not vm:
            return {
                "success": False,
                "error": f"VM not found: {vm_identifier}",
            }

        try:
            registry = await get_platform_registry()
            client = await registry.get_client(platform_name)
            success = await client.start_vm(vm.id)

            await registry.log_operation(
                platform_name=platform_name,
                operation_type="start_vm",
                resource_type="vm",
                resource_id=vm.id,
                resource_name=vm.name,
                initiator="ai_agent",
                user_id=user_id,
                status="completed" if success else "failed",
            )

            return {
                "success": success,
                "operation": "start_vm",
                "platform": platform_name,
                "vm_id": vm.id,
                "vm_name": vm.name,
                "message": f"VM '{vm.name}' started successfully on {platform_name}",
            }

        except Exception as e:
            logger.error("start_vm_failed", vm_name=vm.name, error=str(e))
            return {
                "success": False,
                "error": f"Failed to start VM: {e}",
            }

    async def _handle_stop_command(self, command: str, user_id: UUID | None) -> dict[str, Any]:
        """Handle VM stop command."""
        vm_identifier = self._extract_vm_identifier(command)
        if not vm_identifier:
            return {
                "success": False,
                "error": "Could not identify VM name or ID. Try: 'stop VM <name>'",
            }

        # Check for force shutdown flag
        force = "force" in command.lower()

        platform_name, vm = await self._find_vm(vm_identifier)
        if not vm:
            return {
                "success": False,
                "error": f"VM not found: {vm_identifier}",
            }

        try:
            registry = await get_platform_registry()
            client = await registry.get_client(platform_name)
            success = await client.stop_vm(vm.id, force=force)

            await registry.log_operation(
                platform_name=platform_name,
                operation_type="stop_vm",
                resource_type="vm",
                resource_id=vm.id,
                resource_name=vm.name,
                initiator="ai_agent",
                user_id=user_id,
                status="completed" if success else "failed",
                result={"force": force},
            )

            return {
                "success": success,
                "operation": "stop_vm",
                "platform": platform_name,
                "vm_id": vm.id,
                "vm_name": vm.name,
                "force": force,
                "message": f"VM '{vm.name}' stopped successfully on {platform_name}",
            }

        except Exception as e:
            logger.error("stop_vm_failed", vm_name=vm.name, error=str(e))
            return {
                "success": False,
                "error": f"Failed to stop VM: {e}",
            }

    async def _handle_list_vms_command(self, command: str, user_id: UUID | None) -> dict[str, Any]:
        """Handle list VMs command."""
        # Extract platform name if specified
        platform_name = self._extract_platform_name(command)

        # Extract cluster name if specified
        cluster = self._extract_cluster_name(command)

        try:
            registry = await get_platform_registry()

            if platform_name:
                # List VMs from specific platform
                client = await registry.get_client(platform_name)
                vms = await client.list_vms(cluster=cluster)

                platforms_checked = {platform_name: vms}
            else:
                # List VMs from all platforms
                clients = await registry.get_all_clients()
                platforms_checked = {}

                for pname, client in clients.items():
                    try:
                        vms = await client.list_vms(cluster=cluster)
                        platforms_checked[pname] = vms
                    except Exception as e:
                        logger.warning("list_vms_failed", platform=pname, error=str(e))

            # Format results
            total_vms = sum(len(vms) for vms in platforms_checked.values())
            running_vms = sum(
                sum(1 for vm in vms if vm.is_running()) for vms in platforms_checked.values()
            )

            vm_list = []
            for pname, vms in platforms_checked.items():
                for vm in vms:
                    vm_list.append(
                        {
                            "name": vm.name,
                            "id": vm.id,
                            "power_state": vm.power_state,
                            "platform": pname,
                            "cluster": vm.cluster,
                            "cpu_count": vm.cpu_count,
                            "memory_mb": vm.memory_mb,
                        }
                    )

            return {
                "success": True,
                "operation": "list_vms",
                "total": total_vms,
                "running": running_vms,
                "stopped": total_vms - running_vms,
                "platforms_checked": list(platforms_checked.keys()),
                "vms": vm_list,
                "message": f"Found {total_vms} VMs ({running_vms} running, {total_vms - running_vms} stopped)",
            }

        except Exception as e:
            logger.error("list_vms_failed", error=str(e))
            return {
                "success": False,
                "error": f"Failed to list VMs: {e}",
            }

    async def _handle_list_hosts_command(
        self, command: str, user_id: UUID | None
    ) -> dict[str, Any]:
        """Handle list hosts command."""
        platform_name = self._extract_platform_name(command)

        try:
            registry = await get_platform_registry()

            if platform_name:
                client = await registry.get_client(platform_name)
                hosts = await client.list_hosts()
                platforms_checked = {platform_name: hosts}
            else:
                clients = await registry.get_all_clients()
                platforms_checked = {}

                for pname, client in clients.items():
                    try:
                        hosts = await client.list_hosts()
                        platforms_checked[pname] = hosts
                    except Exception as e:
                        logger.warning("list_hosts_failed", platform=pname, error=str(e))

            # Format results
            total_hosts = sum(len(hosts) for hosts in platforms_checked.values())
            healthy_hosts = sum(
                sum(1 for host in hosts if host.is_healthy())
                for hosts in platforms_checked.values()
            )

            host_list = []
            for pname, hosts in platforms_checked.items():
                for host in hosts:
                    host_list.append(
                        {
                            "name": host.name,
                            "id": host.id,
                            "status": host.status,
                            "platform": pname,
                            "cluster": host.cluster,
                            "cpu_capacity": host.cpu_capacity,
                            "memory_capacity_mb": host.memory_capacity_mb,
                        }
                    )

            return {
                "success": True,
                "operation": "list_hosts",
                "total": total_hosts,
                "healthy": healthy_hosts,
                "unhealthy": total_hosts - healthy_hosts,
                "platforms_checked": list(platforms_checked.keys()),
                "hosts": host_list,
                "message": f"Found {total_hosts} hosts ({healthy_hosts} healthy, {total_hosts - healthy_hosts} unhealthy)",
            }

        except Exception as e:
            logger.error("list_hosts_failed", error=str(e))
            return {
                "success": False,
                "error": f"Failed to list hosts: {e}",
            }

    def _extract_vm_identifier(self, command: str) -> str | None:
        """Extract VM name or ID from command."""
        # Try to find VM identifier after keywords
        patterns = [
            r"vm\s+([a-zA-Z0-9\-_.]+)",
            r"virtual\s+machine\s+([a-zA-Z0-9\-_.]+)",
            r"'([^']+)'",
            r'"([^"]+)"',
        ]

        for pattern in patterns:
            match = re.search(pattern, command, re.IGNORECASE)
            if match:
                return match.group(1)

        return None

    def _extract_platform_name(self, command: str) -> str | None:
        """Extract platform name from command."""
        command_lower = command.lower()

        # Check for platform keywords
        if "nutanix" in command_lower or "prism" in command_lower:
            # Try to find specific platform name
            match = re.search(r"(nutanix-\w+|prism-\w+|\w+-nutanix)", command_lower)
            if match:
                return match.group(1)
            return "nutanix"  # Default Nutanix platform name
        elif "vmware" in command_lower or "vcenter" in command_lower or "esxi" in command_lower:
            match = re.search(r"(vmware-\w+|vcenter-\w+|\w+-vmware)", command_lower)
            if match:
                return match.group(1)
            return "vmware"
        elif "openshift" in command_lower:
            match = re.search(r"(openshift-\w+|\w+-openshift)", command_lower)
            if match:
                return match.group(1)
            return "openshift"

        return None

    def _extract_cluster_name(self, command: str) -> str | None:
        """Extract cluster name from command."""
        patterns = [
            r"cluster\s+([a-zA-Z0-9\-_.]+)",
            r"in\s+([a-zA-Z0-9\-_.]+)\s+cluster",
        ]

        for pattern in patterns:
            match = re.search(pattern, command, re.IGNORECASE)
            if match:
                return match.group(1)

        return None

    async def _find_vm(self, identifier: str) -> tuple[str | None, VMResource | None]:
        """
        Find a VM by name or ID across all platforms.

        Args:
            identifier: VM name or ID

        Returns:
            Tuple of (platform_name, vm_resource) or (None, None) if not found
        """
        registry = await get_platform_registry()
        clients = await registry.get_all_clients()

        for platform_name, client in clients.items():
            try:
                # Try as ID first
                try:
                    vm = await client.get_vm(identifier)
                    return platform_name, vm
                except (ValueError, Exception):
                    pass

                # Try finding by name
                vms = await client.list_vms()
                for vm in vms:
                    if (
                        vm.name.lower() == identifier.lower()
                        or identifier.lower() in vm.name.lower()
                    ):
                        return platform_name, vm

            except Exception as e:
                logger.debug("vm_search_failed", platform=platform_name, error=str(e))

        return None, None

    @classmethod
    def is_platform_command(cls, message: str) -> bool:
        """
        Check if a message contains platform-related keywords.

        Args:
            message: Message to check

        Returns:
            True if message appears to be a platform command
        """
        message_lower = message.lower()
        return any(keyword in message_lower for keyword in cls.PLATFORM_KEYWORDS)
