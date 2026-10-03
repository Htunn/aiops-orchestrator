"""
Async Azure Resource Manager (ARM) client wrapping azure-identity + azure-mgmt-* SDKs.

Provides a singleton client with lazy initialization, supporting managed identity
(when the orchestrator runs inside Azure) or service-principal credentials via
Entra ID, scoped to the subscriptions/resource groups declared in
``config/azure_resources.yml`` (see SPEC-002).

Mirrors the lazy-init / graceful-degrade / background-retry pattern used by
``src/k8s/client.py`` so Azure features are optional and never crash the app
when unconfigured.
"""

import asyncio
import re
from typing import Any

import structlog
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from src.config import get_settings, load_azure_resources_config

logger = structlog.get_logger()

_RETRY_INTERVAL_SECONDS = 30  # seconds between background reinit attempts

# Conservative allowlist for Azure resource-group / resource names, used to
# validate user-supplied values before they are interpolated into ARM SDK calls.
_AZURE_NAME_RE = re.compile(r"^[a-zA-Z0-9._()-]{1,90}$")

_FIELD_RESOURCE_GROUP = "resource group"
_FIELD_VM_NAME = "VM name"


class AzureAuthorizationError(Exception):
    """Raised when ARM denies an operation (HTTP 403 / AuthorizationFailed)."""


class AzureNotFoundError(Exception):
    """Raised when an ARM resource is not found (HTTP 404)."""


class AzureThrottledError(Exception):
    """Raised when ARM throttles the request (HTTP 429) after retries are exhausted."""


class AzureScopeError(Exception):
    """Raised when a request targets a subscription/resource group outside configured scope."""


def _is_throttled(exc: BaseException) -> bool:
    from azure.core.exceptions import HttpResponseError

    return isinstance(exc, HttpResponseError) and getattr(exc, "status_code", None) == 429


class _SubscriptionContext:
    """Per-subscription SDK clients plus the configured resource-group scope."""

    def __init__(
        self,
        subscription_id: str,
        display_name: str,
        resource_group_scope: list[str],
        credential: Any,
    ) -> None:
        self.subscription_id = subscription_id
        self.display_name = display_name
        self.resource_group_scope = set(resource_group_scope or [])
        self.credential = credential
        self.resource_client: Any = None
        self.compute_client: Any = None
        self.web_client: Any = None
        self.aks_client: Any = None
        self.monitor_client: Any = None
        self.resourcehealth_client: Any = None


class AzureResourceClient:
    """
    Singleton async Azure Resource Manager client.

    Usage:
        client = await AzureResourceClient.get_instance()
        rgs = await client.list_resource_groups()
    """

    _instance: "AzureResourceClient | None" = None
    _lock = asyncio.Lock()

    def __init__(self) -> None:
        self._subscriptions: dict[str, _SubscriptionContext] = {}
        self._initialized = False
        self._init_attempted = False
        self._retry_task: asyncio.Task | None = None

    @classmethod
    async def get_instance(cls) -> "AzureResourceClient":
        """Get or create the singleton client instance."""
        async with cls._lock:
            if cls._instance is None or (
                not cls._instance._initialized and not cls._instance._init_attempted
            ):
                instance = cls()
                await instance._initialize()
                cls._instance = instance
            return cls._instance

    async def _initialize(self) -> None:
        """Load config, build an Entra ID credential, and register configured subscriptions."""
        self._init_attempted = True

        if not get_settings().azure_integration_enabled:
            logger.info("azure_client_integration_disabled")
            self._initialized = False
            return

        config = load_azure_resources_config()
        subscriptions_cfg = config.get("azure", {}).get("subscriptions", [])

        if not subscriptions_cfg:
            logger.info("azure_client_no_subscriptions_configured")
            self._initialized = False
            return

        try:
            credential = self._build_credential(config)
            for sub_cfg in subscriptions_cfg:
                subscription_id = sub_cfg.get("subscription_id")
                if not subscription_id:
                    continue
                self._subscriptions[subscription_id] = _SubscriptionContext(
                    subscription_id=subscription_id,
                    display_name=sub_cfg.get("display_name", subscription_id),
                    resource_group_scope=sub_cfg.get("resource_group_scope", []),
                    credential=credential,
                )

            self._initialized = bool(self._subscriptions)
            logger.info(
                "azure_client_initialized",
                subscriptions=list(self._subscriptions.keys()),
            )
        except Exception as e:
            logger.warning("azure_client_init_failed", error=str(e))
            self._initialized = False
            # Do not re-raise — callers check is_available; Azure features are optional
            if self._retry_task is None or self._retry_task.done():
                self._retry_task = asyncio.create_task(
                    self._retry_loop(), name="azure-client-retry"
                )

    async def _retry_loop(self) -> None:
        """Background task that retries Azure init at a fixed interval until successful."""
        while not self._initialized:
            await asyncio.sleep(_RETRY_INTERVAL_SECONDS)
            logger.info("azure_client_retry_attempt")
            self._init_attempted = False
            await self._initialize()
        self._retry_task = None
        logger.info("azure_client_retry_succeeded")

    @staticmethod
    def _build_credential(config: dict[str, Any]) -> Any:
        """Build an Entra ID credential: managed identity if configured, else service principal."""
        from azure.identity.aio import ClientSecretCredential, DefaultAzureCredential

        settings = get_settings()
        use_managed_identity = (
            config.get("azure", {}).get("auth", {}).get("use_managed_identity", False)
        )

        if use_managed_identity:
            return DefaultAzureCredential()

        if settings.azure_tenant_id and settings.azure_client_id and settings.azure_client_secret:
            return ClientSecretCredential(
                tenant_id=settings.azure_tenant_id,
                client_id=settings.azure_client_id,
                client_secret=settings.azure_client_secret,
            )

        # Fall back to the full DefaultAzureCredential chain (env vars, CLI, managed identity, ...)
        return DefaultAzureCredential()

    @property
    def is_available(self) -> bool:
        return self._initialized

    async def close(self) -> None:
        """Close all per-subscription SDK clients and credentials."""
        for sub in self._subscriptions.values():
            for client in (
                sub.resource_client,
                sub.compute_client,
                sub.web_client,
                sub.aks_client,
                sub.monitor_client,
                sub.resourcehealth_client,
            ):
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        pass
            try:
                await sub.credential.close()
            except Exception:
                pass

    # ── Scope enforcement ───────────────────────────────────────────────────

    def _get_subscription(self, subscription_id: str | None = None) -> _SubscriptionContext:
        if not self._subscriptions:
            raise RuntimeError("Azure client not initialized or no subscriptions configured")
        if subscription_id is None:
            subscription_id = next(iter(self._subscriptions))
        sub = self._subscriptions.get(subscription_id)
        if sub is None:
            raise AzureScopeError(f"Subscription not configured: {subscription_id}")
        return sub

    @staticmethod
    def _check_resource_group_scope(sub: _SubscriptionContext, resource_group: str) -> None:
        if sub.resource_group_scope and resource_group not in sub.resource_group_scope:
            raise AzureScopeError(
                f"Resource group '{resource_group}' is outside the configured scope "
                f"for subscription '{sub.subscription_id}'"
            )

    @staticmethod
    def _validate_name(value: str, field: str = "name") -> None:
        if not value or not _AZURE_NAME_RE.match(value):
            raise ValueError(f"Invalid Azure {field}: {value!r}")

    # ── Lazy per-subscription SDK client accessors ──────────────────────────

    @staticmethod
    def _get_resource_client(sub: _SubscriptionContext) -> Any:
        if sub.resource_client is None:
            from azure.mgmt.resource.resources.aio import ResourceManagementClient

            sub.resource_client = ResourceManagementClient(sub.credential, sub.subscription_id)
        return sub.resource_client

    @staticmethod
    def _get_compute_client(sub: _SubscriptionContext) -> Any:
        if sub.compute_client is None:
            from azure.mgmt.compute.aio import ComputeManagementClient

            sub.compute_client = ComputeManagementClient(sub.credential, sub.subscription_id)
        return sub.compute_client

    @staticmethod
    def _get_web_client(sub: _SubscriptionContext) -> Any:
        if sub.web_client is None:
            from azure.mgmt.web.aio import WebSiteManagementClient

            sub.web_client = WebSiteManagementClient(sub.credential, sub.subscription_id)
        return sub.web_client

    @staticmethod
    def _get_aks_client(sub: _SubscriptionContext) -> Any:
        if sub.aks_client is None:
            from azure.mgmt.containerservice.aio import ContainerServiceClient

            sub.aks_client = ContainerServiceClient(sub.credential, sub.subscription_id)
        return sub.aks_client

    @staticmethod
    def _get_monitor_client(sub: _SubscriptionContext) -> Any:
        if sub.monitor_client is None:
            from azure.mgmt.monitor.aio import MonitorManagementClient

            sub.monitor_client = MonitorManagementClient(sub.credential, sub.subscription_id)
        return sub.monitor_client

    @staticmethod
    def _get_resourcehealth_client(sub: _SubscriptionContext) -> Any:
        if sub.resourcehealth_client is None:
            from azure.mgmt.resourcehealth.aio import ResourceHealthMgmtClient

            sub.resourcehealth_client = ResourceHealthMgmtClient(
                sub.credential, sub.subscription_id
            )
        return sub.resourcehealth_client

    # ── Error-mapped call wrapper ────────────────────────────────────────────

    @staticmethod
    async def _call(func: Any, *args: Any, **kwargs: Any) -> Any:
        """Execute an ARM SDK call, mapping errors and retrying 429s with backoff."""
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception(_is_throttled),
                stop=stop_after_attempt(4),
                wait=wait_exponential(multiplier=1, min=1, max=10),
                reraise=True,
            ):
                with attempt:
                    return await func(*args, **kwargs)
        except ResourceNotFoundError as e:
            raise AzureNotFoundError(str(e)) from e
        except HttpResponseError as e:
            status = getattr(e, "status_code", None)
            if status == 403:
                raise AzureAuthorizationError(str(e)) from e
            if status == 429:
                raise AzureThrottledError(str(e)) from e
            raise

    @staticmethod
    async def _call_lro(poller_factory: Any, *args: Any, **kwargs: Any) -> Any:
        """Execute an ARM long-running-operation call and await it to terminal state."""
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

        try:
            poller = await poller_factory(*args, **kwargs)
            return await poller.result()
        except ResourceNotFoundError as e:
            raise AzureNotFoundError(str(e)) from e
        except HttpResponseError as e:
            status = getattr(e, "status_code", None)
            if status == 403:
                raise AzureAuthorizationError(str(e)) from e
            if status == 429:
                raise AzureThrottledError(str(e)) from e
            raise

    # ── Read operations (LOW risk) ───────────────────────────────────────────

    async def list_resource_groups(
        self, subscription_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List resource groups in scope for a subscription."""
        sub = self._get_subscription(subscription_id)
        client = self._get_resource_client(sub)
        groups = []
        async for rg in client.resource_groups.list():
            if sub.resource_group_scope and rg.name not in sub.resource_group_scope:
                continue
            groups.append({"name": rg.name, "location": rg.location, "tags": rg.tags or {}})
        return groups

    async def list_vms(
        self, resource_group: str, subscription_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List VMs in a resource group."""
        self._validate_name(resource_group, _FIELD_RESOURCE_GROUP)
        sub = self._get_subscription(subscription_id)
        self._check_resource_group_scope(sub, resource_group)
        client = self._get_compute_client(sub)
        vms = []
        async for vm in client.virtual_machines.list(resource_group):
            vms.append(self._vm_to_dict(vm))
        return vms

    async def get_vm(
        self, resource_group: str, vm_name: str, subscription_id: str | None = None
    ) -> dict[str, Any]:
        """Get a single VM with its power state (instance view)."""
        self._validate_name(resource_group, _FIELD_RESOURCE_GROUP)
        self._validate_name(vm_name, _FIELD_VM_NAME)
        sub = self._get_subscription(subscription_id)
        self._check_resource_group_scope(sub, resource_group)
        client = self._get_compute_client(sub)
        vm = await self._call(
            client.virtual_machines.get, resource_group, vm_name, expand="instanceView"
        )
        return self._vm_to_dict(vm, include_power_state=True)

    async def list_vmss(
        self, resource_group: str, subscription_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List VM Scale Sets in a resource group."""
        self._validate_name(resource_group, _FIELD_RESOURCE_GROUP)
        sub = self._get_subscription(subscription_id)
        self._check_resource_group_scope(sub, resource_group)
        client = self._get_compute_client(sub)
        result = []
        async for vmss in client.virtual_machine_scale_sets.list(resource_group):
            result.append(
                {
                    "name": vmss.name,
                    "location": vmss.location,
                    "capacity": vmss.sku.capacity if vmss.sku else None,
                    "vm_size": vmss.sku.name if vmss.sku else None,
                }
            )
        return result

    async def list_app_services(
        self, resource_group: str, subscription_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List App Services (Web Apps) in a resource group."""
        self._validate_name(resource_group, _FIELD_RESOURCE_GROUP)
        sub = self._get_subscription(subscription_id)
        self._check_resource_group_scope(sub, resource_group)
        client = self._get_web_client(sub)
        result = []
        async for app in client.web_apps.list_by_resource_group(resource_group):
            result.append(
                {
                    "name": app.name,
                    "location": app.location,
                    "state": getattr(app, "state", None),
                    "default_hostname": getattr(app, "default_host_name", None),
                }
            )
        return result

    async def list_aks_clusters(
        self, resource_group: str | None = None, subscription_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List AKS clusters, optionally scoped to a resource group."""
        sub = self._get_subscription(subscription_id)
        client = self._get_aks_client(sub)
        result = []
        if resource_group:
            self._validate_name(resource_group, _FIELD_RESOURCE_GROUP)
            self._check_resource_group_scope(sub, resource_group)
            iterator = client.managed_clusters.list_by_resource_group(resource_group)
        else:
            iterator = client.managed_clusters.list()
        async for cluster in iterator:
            rg_name = self._resource_group_from_id(cluster.id)
            if sub.resource_group_scope and rg_name not in sub.resource_group_scope:
                continue
            result.append(
                {
                    "name": cluster.name,
                    "location": cluster.location,
                    "kubernetes_version": getattr(cluster, "kubernetes_version", None),
                    "provisioning_state": getattr(cluster, "provisioning_state", None),
                    "resource_group": rg_name,
                }
            )
        return result

    async def resource_health(
        self, resource_id: str, subscription_id: str | None = None
    ) -> dict[str, Any]:
        """Get current availability status for a resource."""
        sub = self._get_subscription(subscription_id)
        client = self._get_resourcehealth_client(sub)
        status = await self._call(
            client.availability_statuses.get_by_resource, resource_uri=resource_id
        )
        props = getattr(status, "properties", None)
        return {
            "resource_id": resource_id,
            "availability_state": getattr(props, "availability_state", "Unknown"),
            "summary": getattr(props, "summary", None),
            "reason_type": getattr(props, "reason_type", None),
        }

    async def activity_log(
        self,
        resource_group: str,
        lookback_hours: int = 24,
        subscription_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent Activity Log entries for a resource group."""
        from datetime import UTC, datetime, timedelta

        self._validate_name(resource_group, _FIELD_RESOURCE_GROUP)
        sub = self._get_subscription(subscription_id)
        self._check_resource_group_scope(sub, resource_group)
        client = self._get_monitor_client(sub)

        since = (datetime.now(UTC) - timedelta(hours=lookback_hours)).isoformat()
        filter_str = f"eventTimestamp ge '{since}' and resourceGroupName eq '{resource_group}'"
        events = []
        async for event in client.activity_logs.list(filter=filter_str):
            events.append(
                {
                    "event_name": getattr(event.event_name, "value", None)
                    if event.event_name
                    else None,
                    "operation_name": getattr(event.operation_name, "value", None)
                    if event.operation_name
                    else None,
                    "status": getattr(event.status, "value", None) if event.status else None,
                    "resource_id": event.resource_id,
                    "event_timestamp": event.event_timestamp.isoformat()
                    if event.event_timestamp
                    else None,
                    "caller": event.caller,
                }
            )
        return events

    async def resource_metrics(
        self,
        resource_id: str,
        metric_names: list[str],
        subscription_id: str | None = None,
    ) -> dict[str, Any]:
        """Get time-series metrics (e.g. CPU, memory) for a resource."""
        sub = self._get_subscription(subscription_id)
        client = self._get_monitor_client(sub)
        result = await self._call(
            client.metrics.list,
            resource_id,
            metricnames=",".join(metric_names),
        )
        series: dict[str, list[dict[str, Any]]] = {}
        for metric in result.value:
            points = []
            for ts in metric.timeseries:
                for dp in ts.data:
                    points.append(
                        {
                            "timestamp": dp.time_stamp.isoformat() if dp.time_stamp else None,
                            "average": dp.average,
                        }
                    )
            series[metric.name.value if metric.name else "unknown"] = points
        return {"resource_id": resource_id, "metrics": series}

    # ── Mutating operations (MEDIUM / HIGH risk — approval-gated by caller) ──

    async def restart_vm(
        self, resource_group: str, vm_name: str, subscription_id: str | None = None
    ) -> dict[str, Any]:
        """Restart a VM. MEDIUM risk — caller must gate via ApprovalManager."""
        self._validate_name(resource_group, _FIELD_RESOURCE_GROUP)
        self._validate_name(vm_name, _FIELD_VM_NAME)
        sub = self._get_subscription(subscription_id)
        self._check_resource_group_scope(sub, resource_group)
        client = self._get_compute_client(sub)
        await self._call_lro(client.virtual_machines.begin_restart, resource_group, vm_name)
        return {"status": "succeeded", "operation": "restart_vm", "vm_name": vm_name}

    async def scale_vmss(
        self,
        resource_group: str,
        vmss_name: str,
        capacity: int,
        subscription_id: str | None = None,
    ) -> dict[str, Any]:
        """Change a VM Scale Set's instance count. MEDIUM risk."""
        self._validate_name(resource_group, _FIELD_RESOURCE_GROUP)
        self._validate_name(vmss_name, "VMSS name")
        if capacity < 0:
            raise ValueError("capacity must be >= 0")
        sub = self._get_subscription(subscription_id)
        self._check_resource_group_scope(sub, resource_group)
        client = self._get_compute_client(sub)
        await self._call_lro(
            client.virtual_machine_scale_sets.begin_update,
            resource_group,
            vmss_name,
            {"sku": {"capacity": capacity}},
        )
        return {
            "status": "succeeded",
            "operation": "scale_vmss",
            "vmss_name": vmss_name,
            "capacity": capacity,
        }

    async def scale_app_service_plan(
        self,
        resource_group: str,
        plan_name: str,
        capacity: int,
        subscription_id: str | None = None,
    ) -> dict[str, Any]:
        """Change an App Service Plan's worker/instance count. MEDIUM risk."""
        self._validate_name(resource_group, _FIELD_RESOURCE_GROUP)
        self._validate_name(plan_name, "App Service Plan name")
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        sub = self._get_subscription(subscription_id)
        self._check_resource_group_scope(sub, resource_group)
        client = self._get_web_client(sub)
        plan = await self._call(client.app_service_plans.get, resource_group, plan_name)
        plan.sku.capacity = capacity
        await self._call(client.app_service_plans.create_or_update, resource_group, plan_name, plan)
        return {
            "status": "succeeded",
            "operation": "scale_app_service_plan",
            "plan_name": plan_name,
            "capacity": capacity,
        }

    async def restart_aks_nodepool(
        self,
        resource_group: str,
        cluster_name: str,
        nodepool_name: str,
        subscription_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Rolling-restart an AKS node pool via node-image upgrade (ARM-only — see
        SPEC-002 Open Questions resolution; does not touch the K8s API server).
        MEDIUM risk.
        """
        self._validate_name(resource_group, _FIELD_RESOURCE_GROUP)
        self._validate_name(cluster_name, "AKS cluster name")
        self._validate_name(nodepool_name, "node pool name")
        sub = self._get_subscription(subscription_id)
        self._check_resource_group_scope(sub, resource_group)
        client = self._get_aks_client(sub)
        await self._call_lro(
            client.agent_pools.begin_upgrade_node_image_version,
            resource_group,
            cluster_name,
            nodepool_name,
        )
        return {
            "status": "succeeded",
            "operation": "restart_aks_nodepool",
            "cluster_name": cluster_name,
            "nodepool_name": nodepool_name,
        }

    async def deallocate_vm(
        self, resource_group: str, vm_name: str, subscription_id: str | None = None
    ) -> dict[str, Any]:
        """Stop/deallocate a VM. HIGH risk."""
        self._validate_name(resource_group, _FIELD_RESOURCE_GROUP)
        self._validate_name(vm_name, _FIELD_VM_NAME)
        sub = self._get_subscription(subscription_id)
        self._check_resource_group_scope(sub, resource_group)
        client = self._get_compute_client(sub)
        await self._call_lro(client.virtual_machines.begin_deallocate, resource_group, vm_name)
        return {"status": "succeeded", "operation": "deallocate_vm", "vm_name": vm_name}

    async def delete_resource(
        self, resource_id: str, subscription_id: str | None = None
    ) -> dict[str, Any]:
        """Delete an ARM resource by its full resource ID. HIGH risk."""
        resource_group = self._resource_group_from_id(resource_id)
        if resource_group:
            sub = self._get_subscription(subscription_id)
            self._check_resource_group_scope(sub, resource_group)
        else:
            sub = self._get_subscription(subscription_id)
        client = self._get_resource_client(sub)
        await self._call_lro(
            client.resources.begin_delete_by_id, resource_id, api_version="2021-04-01"
        )
        return {"status": "succeeded", "operation": "delete_resource", "resource_id": resource_id}

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _resource_group_from_id(resource_id: str | None) -> str | None:
        """Extract the resource group name from an ARM resource ID."""
        if not resource_id:
            return None
        match = re.search(r"/resourceGroups/([^/]+)/", resource_id, re.IGNORECASE)
        return match.group(1) if match else None

    @staticmethod
    def _vm_to_dict(vm: Any, include_power_state: bool = False) -> dict[str, Any]:
        power_state = None
        if include_power_state:
            instance_view = getattr(vm, "instance_view", None)
            statuses = getattr(instance_view, "statuses", None) or []
            for status in statuses:
                code = getattr(status, "code", "") or ""
                if code.startswith("PowerState/"):
                    power_state = code.split("/", 1)[1]
                    break
        return {
            "name": vm.name,
            "location": vm.location,
            "vm_size": vm.hardware_profile.vm_size if vm.hardware_profile else None,
            "power_state": power_state,
            "tags": vm.tags or {},
        }
