"""
VMware vCenter client implementation.

Provides integration with VMware vCenter REST API 7.0+ for VM and ESXi host management.
Supports session-based authentication and resource management operations.
"""

import time
from typing import Any

import httpx
import structlog

from .base_client import (
    BasePlatformClient,
    HostResource,
    PlatformConfig,
    PlatformHealth,
    VMResource,
)

logger = structlog.get_logger()


class VMwareClient(BasePlatformClient):
    """
    VMware vCenter REST API client.

    Implements VM and ESXi host management using vCenter REST API 7.0+.
    Uses session-based authentication for API access.

    Example:
        config = PlatformConfig(
            platform_type=PlatformType.VMWARE,
            endpoint="https://vcenter.example.com",
            username="administrator@vsphere.local",
            password="secret",
            verify_ssl=True
        )
        async with VMwareClient(config) as client:
            vms = await client.list_vms()
    """

    def __init__(self, config: PlatformConfig):
        """Initialize VMware client."""
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._base_url = f"{config.endpoint.rstrip('/')}/rest"

    async def initialize(self) -> None:
        """Initialize HTTP client and create vCenter session."""
        if self._initialized:
            return

        # Create async HTTP client
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            verify=self.config.verify_ssl,
            timeout=self.config.timeout,
        )

        # Create vCenter session
        try:
            await self._create_session()

            # Validate connection
            health = await self.health_check()
            if health.status != "healthy":
                raise ConnectionError(f"vCenter health check failed: {health.message}")

            self._initialized = True
            self.logger.info("vmware_client_initialized", endpoint=self.config.endpoint)

        except Exception as e:
            await self.close()
            raise ConnectionError(f"Failed to initialize VMware client: {e}") from e

    async def _create_session(self) -> None:
        """Create a vCenter session using Basic Auth."""
        if not self._client:
            raise RuntimeError("HTTP client not initialized")

        if not self.config.username or not self.config.password:
            raise ValueError("Username and password required for vCenter authentication")

        try:
            # vCenter REST API uses HTTP Basic Auth to create sessions
            auth = httpx.BasicAuth(self.config.username, self.config.password)
            response = await self._client.post("/com/vmware/cis/session", auth=auth)
            response.raise_for_status()

            # Extract session ID from response
            session_data = response.json()
            self._session_id = session_data.get("value")

            if not self._session_id:
                raise ValueError("Failed to get session ID from vCenter")

            # Set session ID in headers for subsequent requests
            self._client.headers["vmware-api-session-id"] = self._session_id

            self.logger.info("vcenter_session_created", session_id=self._session_id[:8] + "...")

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise ValueError("vCenter authentication failed: Invalid credentials") from e
            raise

    async def _delete_session(self) -> None:
        """Delete the vCenter session."""
        if not self._client or not self._session_id:
            return

        try:
            await self._client.delete("/com/vmware/cis/session")
            self.logger.info("vcenter_session_deleted")
        except Exception as e:
            self.logger.warning("vcenter_session_delete_failed", error=str(e))
        finally:
            self._session_id = None

    async def close(self) -> None:
        """Close HTTP client and delete session."""
        if self._session_id:
            await self._delete_session()

        if self._client:
            await self._client.aclose()
            self._client = None

        self._initialized = False
        self.logger.info("vmware_client_closed")

    async def health_check(self) -> PlatformHealth:
        """Check vCenter health."""
        start_time = time.time()

        try:
            if not self._client or not self._session_id:
                return PlatformHealth(
                    platform="vmware",
                    status="unreachable",
                    message="Client not initialized",
                )

            # Check session validity by listing VMs (quick operation)
            response = await self._client.get("/vcenter/vm")
            response_time = (time.time() - start_time) * 1000

            if response.status_code == 200:
                return PlatformHealth(
                    platform="vmware",
                    status="healthy",
                    message="vCenter is reachable",
                    response_time_ms=response_time,
                )
            else:
                return PlatformHealth(
                    platform="vmware",
                    status="degraded",
                    message=f"HTTP {response.status_code}",
                    response_time_ms=response_time,
                )

        except Exception as e:
            response_time = (time.time() - start_time) * 1000
            return PlatformHealth(
                platform="vmware",
                status="unreachable",
                message=str(e),
                response_time_ms=response_time,
            )

    async def list_vms(
        self,
        cluster: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[VMResource]:
        """List VMs from vCenter."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            # Build query parameters
            params = {}
            if cluster:
                params["filter.clusters"] = cluster

            if filters:
                params.update(filters)

            response = await self._client.get("/vcenter/vm", params=params)
            response.raise_for_status()
            data = response.json()

            vms = []
            for vm_summary in data.get("value", []):
                vm_id = vm_summary.get("vm")

                # Get detailed VM info
                detail_response = await self._client.get(f"/vcenter/vm/{vm_id}")
                detail_response.raise_for_status()
                vm_detail = detail_response.json().get("value", {})

                # Map vCenter power state to standard states
                power_state_map = {
                    "POWERED_ON": "running",
                    "POWERED_OFF": "stopped",
                    "SUSPENDED": "suspended",
                }
                vcenter_state = vm_summary.get("power_state", "UNKNOWN")
                power_state = power_state_map.get(vcenter_state, "unknown")

                # Extract resource info
                cpu_count = vm_detail.get("cpu", {}).get("count")
                memory_mb = vm_detail.get("memory", {}).get("size_MiB")

                # Extract host info
                host_id = vm_detail.get("host")

                # Extract network IPs (requires guest tools)
                ip_addresses = []
                guest = vm_detail.get("guest_OS", {})
                if guest_identity := guest.get("identity"):
                    if ip_address := guest_identity.get("ip_address"):
                        ip_addresses.append(ip_address)

                vms.append(
                    VMResource(
                        id=vm_id,
                        name=vm_summary.get("name", "unknown"),
                        power_state=power_state,
                        cpu_count=cpu_count,
                        memory_mb=memory_mb,
                        host=host_id,
                        cluster=None,  # Cluster info not in VM summary
                        ip_addresses=ip_addresses or None,
                        platform="vmware",
                        metadata={
                            "raw_power_state": vcenter_state,
                            "guest_os": vm_detail.get("guest_OS", {}).get("type"),
                        },
                    )
                )

            self.logger.info("listed_vms", count=len(vms), cluster=cluster)
            return vms

        except httpx.HTTPStatusError as e:
            self.logger.error("list_vms_failed", status_code=e.response.status_code, error=str(e))
            raise
        except Exception as e:
            self.logger.error("list_vms_error", error=str(e))
            raise

    async def get_vm(self, vm_id: str) -> VMResource:
        """Get VM details by ID."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            response = await self._client.get(f"/vcenter/vm/{vm_id}")
            response.raise_for_status()
            vm_detail = response.json().get("value", {})

            # Map power state
            power_state_map = {
                "POWERED_ON": "running",
                "POWERED_OFF": "stopped",
                "SUSPENDED": "suspended",
            }
            vcenter_state = vm_detail.get("power_state", "UNKNOWN")
            power_state = power_state_map.get(vcenter_state, "unknown")

            cpu_count = vm_detail.get("cpu", {}).get("count")
            memory_mb = vm_detail.get("memory", {}).get("size_MiB")
            host_id = vm_detail.get("host")

            # Extract IPs
            ip_addresses = []
            guest = vm_detail.get("guest_OS", {})
            if guest_identity := guest.get("identity"):
                if ip_address := guest_identity.get("ip_address"):
                    ip_addresses.append(ip_address)

            return VMResource(
                id=vm_id,
                name=vm_detail.get("name", "unknown"),
                power_state=power_state,
                cpu_count=cpu_count,
                memory_mb=memory_mb,
                host=host_id,
                cluster=None,
                ip_addresses=ip_addresses or None,
                platform="vmware",
                metadata={
                    "raw_power_state": vcenter_state,
                    "guest_os": vm_detail.get("guest_OS", {}).get("type"),
                    "tools_version": vm_detail.get("tools", {}).get("version"),
                },
            )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ValueError(f"VM not found: {vm_id}") from e
            self.logger.error("get_vm_failed", vm_id=vm_id, status_code=e.response.status_code)
            raise
        except Exception as e:
            self.logger.error("get_vm_error", vm_id=vm_id, error=str(e))
            raise

    async def start_vm(self, vm_id: str) -> bool:
        """Power on a VM."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            # Check current state
            vm = await self.get_vm(vm_id)

            if vm.is_running():
                self.logger.info("vm_already_running", vm_id=vm_id)
                return True

            # Power on the VM
            response = await self._client.post(f"/vcenter/vm/{vm_id}/power/start")
            response.raise_for_status()

            self.logger.info("vm_started", vm_id=vm_id, vm_name=vm.name)
            return True

        except Exception as e:
            self.logger.error("start_vm_failed", vm_id=vm_id, error=str(e))
            raise

    async def stop_vm(self, vm_id: str, force: bool = False) -> bool:
        """Power off a VM."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            # Check current state
            vm = await self.get_vm(vm_id)

            if vm.power_state == "stopped":
                self.logger.info("vm_already_stopped", vm_id=vm_id)
                return True

            # Stop the VM (graceful shutdown if not forced)
            endpoint = (
                f"/vcenter/vm/{vm_id}/power/stop" if force else f"/vcenter/vm/{vm_id}/power/stop"
            )
            response = await self._client.post(endpoint)
            response.raise_for_status()

            self.logger.info("vm_stopped", vm_id=vm_id, vm_name=vm.name, force=force)
            return True

        except Exception as e:
            self.logger.error("stop_vm_failed", vm_id=vm_id, error=str(e))
            raise

    async def restart_vm(self, vm_id: str) -> bool:
        """Restart a VM."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            vm = await self.get_vm(vm_id)

            # Use vCenter's reset operation if VM is running
            if vm.is_running():
                response = await self._client.post(f"/vcenter/vm/{vm_id}/power/reset")
                response.raise_for_status()
            else:
                # If stopped, just start it
                await self.start_vm(vm_id)

            self.logger.info("vm_restarted", vm_id=vm_id, vm_name=vm.name)
            return True

        except Exception as e:
            self.logger.error("restart_vm_failed", vm_id=vm_id, error=str(e))
            raise

    async def list_hosts(self, cluster: str | None = None) -> list[HostResource]:
        """List ESXi hosts from vCenter."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            params = {}
            if cluster:
                params["filter.clusters"] = cluster

            response = await self._client.get("/vcenter/host", params=params)
            response.raise_for_status()
            data = response.json()

            hosts = []
            for host_summary in data.get("value", []):
                host_id = host_summary.get("host")

                # Map vCenter host connection state
                state = host_summary.get("connection_state", "UNKNOWN")
                status_map = {
                    "CONNECTED": "ready",
                    "DISCONNECTED": "not_ready",
                    "NOT_RESPONDING": "not_ready",
                }
                host_status = status_map.get(state, "unknown")

                # Note: Detailed CPU/memory stats require separate API calls
                hosts.append(
                    HostResource(
                        id=host_id,
                        name=host_summary.get("name", "unknown"),
                        status=host_status,
                        cpu_capacity=None,
                        memory_capacity_mb=None,
                        cpu_usage_percent=None,
                        memory_usage_percent=None,
                        vm_count=None,
                        cluster=None,  # Cluster info not in host summary
                        platform="vmware",
                        metadata={
                            "connection_state": state,
                            "power_state": host_summary.get("power_state"),
                        },
                    )
                )

            self.logger.info("listed_hosts", count=len(hosts), cluster=cluster)
            return hosts

        except Exception as e:
            self.logger.error("list_hosts_error", error=str(e))
            raise

    async def get_host(self, host_id: str) -> HostResource:
        """Get ESXi host details by ID."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            response = await self._client.get(f"/vcenter/host/{host_id}")
            response.raise_for_status()
            host_detail = response.json().get("value", {})

            state = host_detail.get("connection_state", "UNKNOWN")
            status_map = {
                "CONNECTED": "ready",
                "DISCONNECTED": "not_ready",
                "NOT_RESPONDING": "not_ready",
            }
            host_status = status_map.get(state, "unknown")

            return HostResource(
                id=host_id,
                name=host_detail.get("name", "unknown"),
                status=host_status,
                cpu_capacity=None,
                memory_capacity_mb=None,
                cpu_usage_percent=None,
                memory_usage_percent=None,
                vm_count=None,
                cluster=None,
                platform="vmware",
                metadata={
                    "connection_state": state,
                    "power_state": host_detail.get("power_state"),
                },
            )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ValueError(f"Host not found: {host_id}") from e
            raise
        except Exception as e:
            self.logger.error("get_host_error", host_id=host_id, error=str(e))
            raise

    async def list_clusters(self) -> list[dict[str, Any]]:
        """List vSphere clusters."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            response = await self._client.get("/vcenter/cluster")
            response.raise_for_status()
            data = response.json()

            clusters = []
            for cluster_summary in data.get("value", []):
                cluster_info = {
                    "id": cluster_summary.get("cluster"),
                    "name": cluster_summary.get("name", "unknown"),
                    "drs_enabled": cluster_summary.get("drs_enabled", False),
                    "ha_enabled": cluster_summary.get("ha_enabled", False),
                }
                clusters.append(cluster_info)

            self.logger.info("listed_clusters", count=len(clusters))
            return clusters

        except Exception as e:
            self.logger.error("list_clusters_error", error=str(e))
            raise
