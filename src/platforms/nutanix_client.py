"""
Nutanix Prism Central client implementation.

Provides integration with Nutanix Prism Central API v3 for VM and cluster management.
Supports both production Prism Central and mock server for testing.
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


class NutanixClient(BasePlatformClient):
    """
    Nutanix Prism Central API v3 client.

    Implements VM and host management using Prism Central REST API.
    Supports both HTTP Basic Auth and API token authentication.

    Example:
        config = PlatformConfig(
            platform_type=PlatformType.NUTANIX,
            endpoint="https://prism-central.example.com:9440",
            username="admin",
            password="secret",
            verify_ssl=True
        )
        async with NutanixClient(config) as client:
            vms = await client.list_vms()
    """

    def __init__(self, config: PlatformConfig):
        """Initialize Nutanix client."""
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None
        self._base_url = f"{config.endpoint.rstrip('/')}/api/nutanix/v3"

    @property
    def _http(self) -> httpx.AsyncClient:
        """Typed accessor for the HTTP client — call initialize() first."""
        assert self._client is not None, "NutanixClient.initialize() must be called first"
        return self._client

    async def initialize(self) -> None:
        """Initialize HTTP client and validate connection."""
        if self._initialized:
            return

        # Create async HTTP client with auth
        auth = None
        headers = {}

        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        elif self.config.username and self.config.password:
            auth = httpx.BasicAuth(self.config.username, self.config.password)
        else:
            raise ValueError("Either token or username/password required")

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=auth,
            headers=headers,
            verify=self.config.verify_ssl,
            timeout=self.config.timeout,
        )

        # Validate connection with health check
        try:
            health = await self.health_check()
            if health.status != "healthy":
                raise ConnectionError(f"Nutanix health check failed: {health.message}")

            self._initialized = True
            self.logger.info("nutanix_client_initialized", endpoint=self.config.endpoint)

        except Exception as e:
            await self.close()
            raise ConnectionError(f"Failed to initialize Nutanix client: {e}") from e

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._initialized = False
        self.logger.info("nutanix_client_closed")

    async def health_check(self) -> PlatformHealth:
        """Check Prism Central health."""
        start_time = time.time()

        try:
            if not self._client:
                return PlatformHealth(
                    platform="nutanix",
                    status="unreachable",
                    message="Client not initialized",
                )

            response = await self._http.post("/clusters/list", json={"kind": "cluster"})
            response_time = (time.time() - start_time) * 1000

            if response.status_code == 200:
                return PlatformHealth(
                    platform="nutanix",
                    status="healthy",
                    message="Prism Central is reachable",
                    response_time_ms=response_time,
                )
            else:
                return PlatformHealth(
                    platform="nutanix",
                    status="degraded",
                    message=f"HTTP {response.status_code}",
                    response_time_ms=response_time,
                )

        except Exception as e:
            response_time = (time.time() - start_time) * 1000
            return PlatformHealth(
                platform="nutanix",
                status="unreachable",
                message=str(e),
                response_time_ms=response_time,
            )

    async def list_vms(
        self,
        cluster: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[VMResource]:
        """List VMs from Prism Central."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        # Build Prism Central v3 query
        payload: dict[str, Any] = {"kind": "vm"}

        # Add cluster filter if specified
        if cluster:
            payload["filter"] = f"cluster_name=={cluster}"

        # Add custom filters
        if filters:
            if "filter" in payload:
                payload["filter"] += f";{filters.get('filter', '')}"
            else:
                payload["filter"] = filters.get("filter", "")

        try:
            response = await self._http.post("/vms/list", json=payload)
            response.raise_for_status()
            data = response.json()

            vms = []
            for entity in data.get("entities", []):
                spec = entity.get("spec", {})
                status = entity.get("status", {})
                resources = spec.get("resources", {})

                # Extract VM details
                vm_id = entity.get("metadata", {}).get("uuid", "")
                name = status.get("name", spec.get("name", "unknown"))

                # Map Nutanix power state to standard states
                power_state_map = {
                    "ON": "running",
                    "OFF": "stopped",
                    "PAUSED": "suspended",
                }
                nutanix_state = resources.get("power_state", "UNKNOWN")
                power_state = power_state_map.get(nutanix_state, "unknown")

                # Extract resource specs
                cpu_count = resources.get("num_sockets", 0) * resources.get(
                    "num_vcpus_per_socket", 1
                )
                memory_mb = resources.get("memory_size_mib", 0)

                # Extract network info
                ip_addresses = []
                for nic in status.get("resources", {}).get("nic_list", []):
                    for ip_endpoint in nic.get("ip_endpoint_list", []):
                        if ip := ip_endpoint.get("ip"):
                            ip_addresses.append(ip)

                # Get cluster reference
                cluster_ref = status.get("cluster_reference", {})
                cluster_name = cluster_ref.get("name")

                vms.append(
                    VMResource(
                        id=vm_id,
                        name=name,
                        power_state=power_state,
                        cpu_count=cpu_count,
                        memory_mb=memory_mb,
                        host=None,  # Nutanix doesn't expose host in VM API
                        cluster=cluster_name,
                        ip_addresses=ip_addresses or None,
                        platform="nutanix",
                        metadata={
                            "raw_power_state": nutanix_state,
                            "categories": entity.get("metadata", {}).get("categories", {}),
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
        """Get VM details by UUID."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            response = await self._http.get(f"/vms/{vm_id}")
            response.raise_for_status()
            entity = response.json()

            spec = entity.get("spec", {})
            status = entity.get("status", {})
            resources = spec.get("resources", {})

            name = status.get("name", spec.get("name", "unknown"))

            # Map power state
            power_state_map = {
                "ON": "running",
                "OFF": "stopped",
                "PAUSED": "suspended",
            }
            nutanix_state = resources.get("power_state", "UNKNOWN")
            power_state = power_state_map.get(nutanix_state, "unknown")

            cpu_count = resources.get("num_sockets", 0) * resources.get("num_vcpus_per_socket", 1)
            memory_mb = resources.get("memory_size_mib", 0)

            # Extract IPs
            ip_addresses = []
            for nic in status.get("resources", {}).get("nic_list", []):
                for ip_endpoint in nic.get("ip_endpoint_list", []):
                    if ip := ip_endpoint.get("ip"):
                        ip_addresses.append(ip)

            cluster_ref = status.get("cluster_reference", {})
            cluster_name = cluster_ref.get("name")

            return VMResource(
                id=vm_id,
                name=name,
                power_state=power_state,
                cpu_count=cpu_count,
                memory_mb=memory_mb,
                host=None,
                cluster=cluster_name,
                ip_addresses=ip_addresses or None,
                platform="nutanix",
                metadata={
                    "raw_power_state": nutanix_state,
                    "categories": entity.get("metadata", {}).get("categories", {}),
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
            # First get current VM state
            vm = await self.get_vm(vm_id)

            if vm.is_running():
                self.logger.info("vm_already_running", vm_id=vm_id)
                return True

            # Update power state to ON
            payload = {"spec": {"resources": {"power_state": "ON"}}}

            response = await self._http.put(f"/vms/{vm_id}", json=payload)
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
            # First get current VM state
            vm = await self.get_vm(vm_id)

            if vm.power_state == "stopped":
                self.logger.info("vm_already_stopped", vm_id=vm_id)
                return True

            # Update power state to OFF
            # Note: Nutanix doesn't have separate graceful/force shutdown in v3 API
            # The force parameter is provided for interface compatibility
            payload = {"spec": {"resources": {"power_state": "OFF"}}}

            response = await self._http.put(f"/vms/{vm_id}", json=payload)
            response.raise_for_status()

            self.logger.info("vm_stopped", vm_id=vm_id, vm_name=vm.name, force=force)
            return True

        except Exception as e:
            self.logger.error("stop_vm_failed", vm_id=vm_id, error=str(e))
            raise

    async def restart_vm(self, vm_id: str) -> bool:
        """Restart a VM (stop then start)."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            vm = await self.get_vm(vm_id)

            # Stop the VM if running
            if vm.is_running():
                await self.stop_vm(vm_id)

            # Start the VM
            await self.start_vm(vm_id)

            self.logger.info("vm_restarted", vm_id=vm_id, vm_name=vm.name)
            return True

        except Exception as e:
            self.logger.error("restart_vm_failed", vm_id=vm_id, error=str(e))
            raise

    async def list_hosts(self, cluster: str | None = None) -> list[HostResource]:
        """List hosts (Nutanix nodes) from Prism Central."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        payload: dict[str, Any] = {"kind": "host"}

        if cluster:
            payload["filter"] = f"cluster_name=={cluster}"

        try:
            response = await self._http.post("/hosts/list", json=payload)
            response.raise_for_status()
            data = response.json()

            hosts = []
            for entity in data.get("entities", []):
                status = entity.get("status", {})
                resources = status.get("resources", {})

                host_id = entity.get("metadata", {}).get("uuid", "")
                name = status.get("name", "unknown")

                # Map Nutanix host state
                state = resources.get("hypervisor_state", "UNKNOWN")
                status_map = {
                    "NORMAL": "ready",
                    "ACROPOLIS_NORMAL": "ready",
                    "ENTERING_MAINTENANCE_MODE": "maintenance",
                    "MAINTENANCE_MODE": "maintenance",
                }
                host_status = status_map.get(state, "unknown")

                # Get resource stats
                cpu_capacity = resources.get("cpu_capacity_hz", 0) // 1_000_000  # Convert to MHz
                memory_capacity_mb = resources.get("memory_capacity_mib", 0)

                # Get cluster reference
                cluster_ref = status.get("cluster_reference", {})
                cluster_name = cluster_ref.get("name")

                # Note: CPU/Memory usage requires separate stats API calls
                # For now, we'll leave them as None
                hosts.append(
                    HostResource(
                        id=host_id,
                        name=name,
                        status=host_status,
                        cpu_capacity=cpu_capacity,
                        memory_capacity_mb=memory_capacity_mb,
                        cpu_usage_percent=None,
                        memory_usage_percent=None,
                        vm_count=None,  # Would need separate query
                        cluster=cluster_name,
                        platform="nutanix",
                        metadata={
                            "hypervisor_state": state,
                            "hypervisor_type": resources.get("hypervisor_type"),
                        },
                    )
                )

            self.logger.info("listed_hosts", count=len(hosts), cluster=cluster)
            return hosts

        except Exception as e:
            self.logger.error("list_hosts_error", error=str(e))
            raise

    async def get_host(self, host_id: str) -> HostResource:
        """Get host details by UUID."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            response = await self._http.get(f"/hosts/{host_id}")
            response.raise_for_status()
            entity = response.json()

            status = entity.get("status", {})
            resources = status.get("resources", {})

            name = status.get("name", "unknown")
            state = resources.get("hypervisor_state", "UNKNOWN")

            status_map = {
                "NORMAL": "ready",
                "ACROPOLIS_NORMAL": "ready",
                "ENTERING_MAINTENANCE_MODE": "maintenance",
                "MAINTENANCE_MODE": "maintenance",
            }
            host_status = status_map.get(state, "unknown")

            cpu_capacity = resources.get("cpu_capacity_hz", 0) // 1_000_000
            memory_capacity_mb = resources.get("memory_capacity_mib", 0)

            cluster_ref = status.get("cluster_reference", {})
            cluster_name = cluster_ref.get("name")

            return HostResource(
                id=host_id,
                name=name,
                status=host_status,
                cpu_capacity=cpu_capacity,
                memory_capacity_mb=memory_capacity_mb,
                cpu_usage_percent=None,
                memory_usage_percent=None,
                vm_count=None,
                cluster=cluster_name,
                platform="nutanix",
                metadata={
                    "hypervisor_state": state,
                    "hypervisor_type": resources.get("hypervisor_type"),
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
        """List Nutanix clusters."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            response = await self._http.post("/clusters/list", json={"kind": "cluster"})
            response.raise_for_status()
            data = response.json()

            clusters = []
            for entity in data.get("entities", []):
                status = entity.get("status", {})
                resources = status.get("resources", {})

                cluster_info = {
                    "id": entity.get("metadata", {}).get("uuid", ""),
                    "name": status.get("name", "unknown"),
                    "state": resources.get("state", "UNKNOWN"),
                    "hypervisor_types": resources.get("config", {}).get(
                        "supported_hypervisor_types", []
                    ),
                    "num_nodes": len(resources.get("nodes", {}).get("node_list", [])),
                }
                clusters.append(cluster_info)

            self.logger.info("listed_clusters", count=len(clusters))
            return clusters

        except Exception as e:
            self.logger.error("list_clusters_error", error=str(e))
            raise
