"""
OpenShift client implementation.

Provides integration with OpenShift 4.x REST API for managing OpenShift-specific
resources (Projects, Routes, BuildConfigs, ImageStreams) in addition to standard
Kubernetes resources.
"""

import time
from typing import Any

import httpx
import structlog

from ..k8s.client import KubernetesClient
from .base_client import (
    BasePlatformClient,
    HostResource,
    PlatformConfig,
    PlatformHealth,
    VMResource,
)

logger = structlog.get_logger()


class OpenShiftClient(BasePlatformClient):
    """
    OpenShift 4.x API client.

    Extends Kubernetes functionality with OpenShift-specific resources.
    Manages Projects (namespace wrapper), Routes (ingress), BuildConfigs,
    Builds, and ImageStreams.

    Note: For standard Kubernetes resources (Pods, Deployments, Services),
    use the KubernetesClient directly. This client focuses on OpenShift extensions.

    Example:
        config = PlatformConfig(
            platform_type=PlatformType.OPENSHIFT,
            endpoint="https://api.openshift.example.com:6443",
            token="sha256~your-token-here",
            verify_ssl=True
        )
        async with OpenShiftClient(config) as client:
            projects = await client.list_projects()
            routes = await client.list_routes(namespace="production")
    """

    def __init__(self, config: PlatformConfig):
        """Initialize OpenShift client."""
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None
        self._k8s_client: KubernetesClient | None = None
        self._base_url = config.endpoint.rstrip("/")

    @property
    def _http(self) -> httpx.AsyncClient:
        """Typed accessor for the HTTP client — call initialize() first."""
        assert self._client is not None, "OpenShiftClient.initialize() must be called first"
        return self._client

    async def initialize(self) -> None:
        """Initialize HTTP client and validate connection."""
        if self._initialized:
            return

        if not self.config.token:
            raise ValueError("Token authentication required for OpenShift")

        # Create async HTTP client with token auth
        headers = {"Authorization": f"Bearer {self.config.token}"}

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            verify=self.config.verify_ssl,
            timeout=self.config.timeout,
        )

        # Validate connection
        try:
            health = await self.health_check()
            if health.status != "healthy":
                raise ConnectionError(f"OpenShift health check failed: {health.message}")

            # Also initialize Kubernetes client for standard resources
            # Note: This is optional and depends on whether you want to use K8s client
            # For now, we'll keep OpenShift client focused on OpenShift-specific APIs

            self._initialized = True
            self.logger.info("openshift_client_initialized", endpoint=self.config.endpoint)

        except Exception as e:
            await self.close()
            raise ConnectionError(f"Failed to initialize OpenShift client: {e}") from e

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

        if self._k8s_client:
            # Close K8s client if initialized
            self._k8s_client = None

        self._initialized = False
        self.logger.info("openshift_client_closed")

    async def health_check(self) -> PlatformHealth:
        """Check OpenShift API health."""
        start_time = time.time()

        try:
            if not self._client:
                return PlatformHealth(
                    platform="openshift",
                    status="unreachable",
                    message="Client not initialized",
                )

            # Check OpenShift API health endpoint
            response = await self._http.get("/healthz")
            response_time = (time.time() - start_time) * 1000

            if response.status_code == 200:
                return PlatformHealth(
                    platform="openshift",
                    status="healthy",
                    message="OpenShift API is reachable",
                    response_time_ms=response_time,
                )
            else:
                return PlatformHealth(
                    platform="openshift",
                    status="degraded",
                    message=f"HTTP {response.status_code}",
                    response_time_ms=response_time,
                )

        except Exception as e:
            response_time = (time.time() - start_time) * 1000
            return PlatformHealth(
                platform="openshift",
                status="unreachable",
                message=str(e),
                response_time_ms=response_time,
            )

    # OpenShift-specific methods

    async def list_projects(self) -> list[dict[str, Any]]:
        """
        List OpenShift projects (namespace wrappers).

        Returns:
            List of project information dicts
        """
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            response = await self._http.get("/apis/project.openshift.io/v1/projects")
            response.raise_for_status()
            data = response.json()

            projects = []
            for item in data.get("items", []):
                metadata = item.get("metadata", {})
                status = item.get("status", {})

                project_info = {
                    "name": metadata.get("name", "unknown"),
                    "display_name": metadata.get("annotations", {}).get(
                        "openshift.io/display-name"
                    ),
                    "description": metadata.get("annotations", {}).get("openshift.io/description"),
                    "phase": status.get("phase", "Unknown"),
                    "created_at": metadata.get("creationTimestamp"),
                }
                projects.append(project_info)

            self.logger.info("listed_projects", count=len(projects))
            return projects

        except Exception as e:
            self.logger.error("list_projects_error", error=str(e))
            raise

    async def get_project(self, name: str) -> dict[str, Any]:
        """
        Get OpenShift project details.

        Args:
            name: Project name

        Returns:
            Project information dict
        """
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            response = await self._http.get(f"/apis/project.openshift.io/v1/projects/{name}")
            response.raise_for_status()
            item = response.json()

            metadata = item.get("metadata", {})
            status = item.get("status", {})

            return {
                "name": metadata.get("name", "unknown"),
                "display_name": metadata.get("annotations", {}).get("openshift.io/display-name"),
                "description": metadata.get("annotations", {}).get("openshift.io/description"),
                "phase": status.get("phase", "Unknown"),
                "created_at": metadata.get("creationTimestamp"),
                "labels": metadata.get("labels", {}),
                "annotations": metadata.get("annotations", {}),
            }

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ValueError(f"Project not found: {name}") from e
            raise
        except Exception as e:
            self.logger.error("get_project_error", project=name, error=str(e))
            raise

    async def list_routes(self, namespace: str) -> list[dict[str, Any]]:
        """
        List OpenShift routes (HTTP/HTTPS ingress) in a namespace.

        Args:
            namespace: Namespace to list routes from

        Returns:
            List of route information dicts
        """
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            response = await self._http.get(
                f"/apis/route.openshift.io/v1/namespaces/{namespace}/routes"
            )
            response.raise_for_status()
            data = response.json()

            routes = []
            for item in data.get("items", []):
                metadata = item.get("metadata", {})
                spec = item.get("spec", {})
                status = item.get("status", {})

                route_info = {
                    "name": metadata.get("name", "unknown"),
                    "namespace": metadata.get("namespace"),
                    "host": spec.get("host"),
                    "path": spec.get("path", "/"),
                    "service": spec.get("to", {}).get("name"),
                    "port": spec.get("port", {}).get("targetPort"),
                    "tls_enabled": spec.get("tls") is not None,
                    "tls_termination": spec.get("tls", {}).get("termination")
                    if spec.get("tls")
                    else None,
                    "admitted": any(
                        ingress.get("conditions", [{}])[0].get("status") == "True"
                        for ingress in status.get("ingress", [])
                    ),
                }
                routes.append(route_info)

            self.logger.info("listed_routes", count=len(routes), namespace=namespace)
            return routes

        except Exception as e:
            self.logger.error("list_routes_error", namespace=namespace, error=str(e))
            raise

    async def list_buildconfigs(self, namespace: str) -> list[dict[str, Any]]:
        """
        List OpenShift build configurations in a namespace.

        Args:
            namespace: Namespace to list buildconfigs from

        Returns:
            List of buildconfig information dicts
        """
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            response = await self._http.get(
                f"/apis/build.openshift.io/v1/namespaces/{namespace}/buildconfigs"
            )
            response.raise_for_status()
            data = response.json()

            buildconfigs = []
            for item in data.get("items", []):
                metadata = item.get("metadata", {})
                spec = item.get("spec", {})

                bc_info = {
                    "name": metadata.get("name", "unknown"),
                    "namespace": metadata.get("namespace"),
                    "strategy": spec.get("strategy", {}).get("type"),
                    "source_type": spec.get("source", {}).get("type"),
                    "output_image": spec.get("output", {}).get("to", {}).get("name"),
                    "triggers": [t.get("type") for t in spec.get("triggers", [])],
                }
                buildconfigs.append(bc_info)

            self.logger.info("listed_buildconfigs", count=len(buildconfigs), namespace=namespace)
            return buildconfigs

        except Exception as e:
            self.logger.error("list_buildconfigs_error", namespace=namespace, error=str(e))
            raise

    async def list_builds(
        self, namespace: str, buildconfig: str | None = None
    ) -> list[dict[str, Any]]:
        """
        List OpenShift builds in a namespace.

        Args:
            namespace: Namespace to list builds from
            buildconfig: Optional buildconfig name to filter builds

        Returns:
            List of build information dicts
        """
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            url = f"/apis/build.openshift.io/v1/namespaces/{namespace}/builds"
            params = {}
            if buildconfig:
                params["labelSelector"] = f"buildconfig={buildconfig}"

            response = await self._http.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            builds = []
            for item in data.get("items", []):
                metadata = item.get("metadata", {})
                spec = item.get("spec", {})
                status = item.get("status", {})

                build_info = {
                    "name": metadata.get("name", "unknown"),
                    "namespace": metadata.get("namespace"),
                    "buildconfig": metadata.get("labels", {}).get("buildconfig"),
                    "phase": status.get("phase", "Unknown"),
                    "strategy": spec.get("strategy", {}).get("type"),
                    "duration": status.get("duration"),
                    "output_image": status.get("outputDockerImageReference"),
                    "started_at": status.get("startTimestamp"),
                    "completed_at": status.get("completionTimestamp"),
                }
                builds.append(build_info)

            self.logger.info("listed_builds", count=len(builds), namespace=namespace)
            return builds

        except Exception as e:
            self.logger.error("list_builds_error", namespace=namespace, error=str(e))
            raise

    async def list_imagestreams(self, namespace: str) -> list[dict[str, Any]]:
        """
        List OpenShift image streams in a namespace.

        Args:
            namespace: Namespace to list imagestreams from

        Returns:
            List of imagestream information dicts
        """
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            response = await self._http.get(
                f"/apis/image.openshift.io/v1/namespaces/{namespace}/imagestreams"
            )
            response.raise_for_status()
            data = response.json()

            imagestreams = []
            for item in data.get("items", []):
                metadata = item.get("metadata", {})
                status = item.get("status", {})

                tags = []
                for tag in status.get("tags", []):
                    tag_info = {
                        "tag": tag.get("tag"),
                        "items_count": len(tag.get("items", [])),
                    }
                    tags.append(tag_info)

                is_info = {
                    "name": metadata.get("name", "unknown"),
                    "namespace": metadata.get("namespace"),
                    "docker_image_repository": status.get("dockerImageRepository"),
                    "tags": tags,
                    "tags_count": len(tags),
                }
                imagestreams.append(is_info)

            self.logger.info("listed_imagestreams", count=len(imagestreams), namespace=namespace)
            return imagestreams

        except Exception as e:
            self.logger.error("list_imagestreams_error", namespace=namespace, error=str(e))
            raise

    # Base interface implementation
    # Note: OpenShift uses Pods/Nodes for VM/Host concepts
    # These map to Kubernetes resources rather than traditional VMs

    async def list_vms(
        self,
        cluster: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[VMResource]:
        """
        List pods as VM-like resources.

        Note: OpenShift doesn't have traditional VMs. This maps to Pods.
        For true VM management, use Nutanix or VMware clients.
        """
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        self.logger.warning(
            "list_vms_called_on_openshift",
            message="OpenShift doesn't have VMs. Consider using list_pods from Kubernetes client instead.",
        )
        return []

    async def get_vm(self, vm_id: str) -> VMResource:
        """
        Get pod as VM-like resource.

        Note: OpenShift doesn't have traditional VMs.
        """
        raise NotImplementedError(
            "OpenShift doesn't have VMs. Use KubernetesClient for pod management."
        )

    async def start_vm(self, vm_id: str) -> bool:
        """Not applicable for OpenShift."""
        raise NotImplementedError("OpenShift doesn't support VM start operations.")

    async def stop_vm(self, vm_id: str, force: bool = False) -> bool:
        """Not applicable for OpenShift."""
        raise NotImplementedError("OpenShift doesn't support VM stop operations.")

    async def restart_vm(self, vm_id: str) -> bool:
        """Not applicable for OpenShift."""
        raise NotImplementedError("OpenShift doesn't support VM restart operations.")

    async def list_hosts(self, cluster: str | None = None) -> list[HostResource]:
        """
        List nodes as host-like resources.

        Note: For full node management, use KubernetesClient.
        """
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            response = await self._http.get("/api/v1/nodes")
            response.raise_for_status()
            data = response.json()

            hosts = []
            for item in data.get("items", []):
                metadata = item.get("metadata", {})
                status = item.get("status", {})

                # Get node conditions
                conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
                node_ready = conditions.get("Ready") == "True"
                node_status = "ready" if node_ready else "not_ready"

                # Get capacity
                capacity = status.get("capacity", {})
                cpu_capacity = int(capacity.get("cpu", 0))
                memory_str = capacity.get("memory", "0Ki")
                # Convert memory string (e.g., "7982136Ki") to MB
                memory_capacity_mb = (
                    int(memory_str.rstrip("Ki")) // 1024 if "Ki" in memory_str else 0
                )

                hosts.append(
                    HostResource(
                        id=metadata.get("uid", ""),
                        name=metadata.get("name", "unknown"),
                        status=node_status,
                        cpu_capacity=cpu_capacity,
                        memory_capacity_mb=memory_capacity_mb,
                        cpu_usage_percent=None,
                        memory_usage_percent=None,
                        vm_count=None,
                        cluster=None,
                        platform="openshift",
                        metadata={
                            "labels": metadata.get("labels", {}),
                            "conditions": conditions,
                        },
                    )
                )

            self.logger.info("listed_hosts", count=len(hosts))
            return hosts

        except Exception as e:
            self.logger.error("list_hosts_error", error=str(e))
            raise

    async def get_host(self, host_id: str) -> HostResource:
        """Get node details by name (OpenShift nodes use name as ID)."""
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        try:
            response = await self._http.get(f"/api/v1/nodes/{host_id}")
            response.raise_for_status()
            item = response.json()

            metadata = item.get("metadata", {})
            status = item.get("status", {})

            conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
            node_ready = conditions.get("Ready") == "True"
            node_status = "ready" if node_ready else "not_ready"

            capacity = status.get("capacity", {})
            cpu_capacity = int(capacity.get("cpu", 0))
            memory_str = capacity.get("memory", "0Ki")
            memory_capacity_mb = int(memory_str.rstrip("Ki")) // 1024 if "Ki" in memory_str else 0

            return HostResource(
                id=metadata.get("uid", ""),
                name=metadata.get("name", "unknown"),
                status=node_status,
                cpu_capacity=cpu_capacity,
                memory_capacity_mb=memory_capacity_mb,
                cpu_usage_percent=None,
                memory_usage_percent=None,
                vm_count=None,
                cluster=None,
                platform="openshift",
                metadata={
                    "labels": metadata.get("labels", {}),
                    "conditions": conditions,
                },
            )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ValueError(f"Node not found: {host_id}") from e
            raise
        except Exception as e:
            self.logger.error("get_host_error", host_id=host_id, error=str(e))
            raise

    async def list_clusters(self) -> list[dict[str, Any]]:
        """
        List clusters.

        Note: OpenShift itself is the cluster. Returns cluster info.
        """
        if not self._initialized:
            raise RuntimeError("Client not initialized. Call initialize() first.")

        # For OpenShift, return basic cluster info
        # This would typically come from cluster version/config APIs
        return [
            {
                "id": "openshift-cluster",
                "name": "OpenShift",
                "platform": "openshift",
            }
        ]
