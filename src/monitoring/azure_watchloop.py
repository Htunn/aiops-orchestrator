"""
Azure Watchloop - background health monitoring for Azure ARM resources (SPEC-003).

Periodically discovers in-scope Azure resources (VMs, App Services, AKS clusters)
from ``config/azure_resources.yml`` and polls their Resource Health / Activity Log
via ``AzureResourceClient``, emitting events for the shared Rule Engine → Playbook
→ Approval pipeline — mirrors ``src/monitoring/platform_watchloop.py``'s structure.

Resource discovery is cached and refreshed only every
``azure_watchloop_discovery_refresh_ticks`` ticks (not every tick) to bound ARM
API call volume, per SPEC-003's resolved Open Question.
"""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from src.config import get_settings, load_azure_resources_config

logger = structlog.get_logger()

# Azure Resource Health availability states considered unhealthy.
_UNHEALTHY_STATES = {"Unavailable", "Degraded"}
# Activity Log operations indicating a VM was deallocated/stopped.
_DEALLOCATE_OPERATIONS = ("deallocate", "poweroff")


@dataclass
class AzureEvent:
    """A detected Azure resource health/activity event."""

    event_type: str  # azure_resource_unhealthy | azure_vm_deallocated | azure_resource_recovered
    severity: str  # critical | warning | info
    resource_group: str
    resource_name: str
    resource_type: str  # vm | app_service | aks
    message: str
    subscription_id: str | None = None
    resource_id: str | None = None
    availability_state: str | None = None
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "severity": self.severity,
            "resource_group": self.resource_group,
            "resource_name": self.resource_name,
            "resource_type": self.resource_type,
            "message": self.message,
            "subscription_id": self.subscription_id,
            "resource_id": self.resource_id,
            "availability_state": self.availability_state,
            "detected_at": self.detected_at.isoformat(),
        }


class AzureWatchLoop:
    """
    Background watchloop for Azure ARM resource health monitoring.

    On each tick:
    1. Re-discover in-scope resources every N ticks (cached otherwise)
    2. Check Resource Health for each discovered resource
    3. Check Activity Log for critical deallocate/poweroff entries
    4. Publish events for unhealthy resources or VM deallocations

    Usage:
        loop = AzureWatchLoop(event_callback=my_handler)
        await loop.start()
        # ... application runs ...
        await loop.stop()
    """

    def __init__(
        self,
        event_callback: Callable[[AzureEvent], Coroutine] | None = None,
        interval: int | None = None,
        discovery_refresh_ticks: int | None = None,
    ) -> None:
        """
        Initialize Azure watchloop.

        Args:
            event_callback: Async callback for Azure events
            interval: Check interval in seconds (default: config's metrics_poll_interval_seconds)
            discovery_refresh_ticks: Re-discover resources every N ticks (default: settings)
        """
        settings = get_settings()
        self._event_callback = event_callback
        self._interval = interval or self._default_interval()
        self._discovery_refresh_ticks = (
            discovery_refresh_ticks or settings.azure_watchloop_discovery_refresh_ticks
        )
        self._task: asyncio.Task | None = None
        self._running = False
        self._tick_count = 0
        self._discovered_resources: list[dict[str, Any]] = []
        self._resource_states: dict[str, str] = {}  # resource_id -> last availability_state
        self._known_deallocations: set[str] = set()  # resource_id:event_name already alerted

    @staticmethod
    def _default_interval() -> int:
        config = load_azure_resources_config()
        return int(
            config.get("azure", {}).get("monitoring", {}).get("metrics_poll_interval_seconds", 300)
        )

    async def start(self) -> None:
        """Start the watchloop."""
        if self._running:
            logger.warning("azure_watchloop_already_running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("azure_watchloop_started", interval=self._interval)

    async def stop(self) -> None:
        """Stop the watchloop."""
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("azure_watchloop_stopped")

    def is_running(self) -> bool:
        """Check if watchloop is running."""
        return self._running

    async def _run_loop(self) -> None:
        """Main watchloop execution."""
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("azure_watchloop_tick_failed", error=str(e), exc_info=True)

            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        """Perform one discovery/health-check cycle."""
        from src.azure.client import AzureResourceClient

        client = await AzureResourceClient.get_instance()
        if not client.is_available:
            return

        if not self._discovered_resources or self._tick_count % self._discovery_refresh_ticks == 0:
            await self._discover_resources(client)
        self._tick_count += 1

        for resource in self._discovered_resources:
            await self._check_resource_health(client, resource)

        await self._check_activity_logs(client)

    async def _discover_resources(self, client: Any) -> None:
        """Discover in-scope VMs/App Services/AKS clusters per config/azure_resources.yml."""
        config = load_azure_resources_config()
        subscriptions = config.get("azure", {}).get("subscriptions", [])
        discovered: list[dict[str, Any]] = []

        for sub_cfg in subscriptions:
            sub_id = sub_cfg.get("subscription_id")
            for rg in sub_cfg.get("resource_group_scope", []):
                try:
                    for vm in await client.list_vms(rg, sub_id):
                        discovered.append(
                            {
                                "resource_type": "vm",
                                "resource_group": rg,
                                "subscription_id": sub_id,
                                "name": vm["name"],
                                "id": vm.get("id"),
                            }
                        )
                except Exception as e:
                    logger.warning(
                        "azure_watchloop_discover_vms_failed", resource_group=rg, error=str(e)
                    )

                try:
                    for app in await client.list_app_services(rg, sub_id):
                        discovered.append(
                            {
                                "resource_type": "app_service",
                                "resource_group": rg,
                                "subscription_id": sub_id,
                                "name": app["name"],
                                "id": app.get("id"),
                            }
                        )
                except Exception as e:
                    logger.warning(
                        "azure_watchloop_discover_app_services_failed",
                        resource_group=rg,
                        error=str(e),
                    )

                try:
                    for cluster in await client.list_aks_clusters(rg, sub_id):
                        discovered.append(
                            {
                                "resource_type": "aks",
                                "resource_group": rg,
                                "subscription_id": sub_id,
                                "name": cluster["name"],
                                "id": cluster.get("id"),
                            }
                        )
                except Exception as e:
                    logger.warning(
                        "azure_watchloop_discover_aks_failed", resource_group=rg, error=str(e)
                    )

        self._discovered_resources = [r for r in discovered if r.get("id")]
        logger.info("azure_watchloop_resources_discovered", count=len(self._discovered_resources))

    async def _check_resource_health(self, client: Any, resource: dict[str, Any]) -> None:
        """Check Resource Health for a single discovered resource and emit on change."""
        resource_id = resource["id"]
        try:
            health = await client.resource_health(resource_id, resource["subscription_id"])
        except Exception as e:
            logger.warning(
                "azure_watchloop_health_check_failed", resource_id=resource_id, error=str(e)
            )
            return

        state = health.get("availability_state", "Unknown")
        previous_state = self._resource_states.get(resource_id)
        self._resource_states[resource_id] = state

        if previous_state == state:
            return

        if state in _UNHEALTHY_STATES:
            event = AzureEvent(
                event_type="azure_resource_unhealthy",
                severity="critical",
                resource_group=resource["resource_group"],
                resource_name=resource["name"],
                resource_type=resource["resource_type"],
                subscription_id=resource["subscription_id"],
                resource_id=resource_id,
                availability_state=state,
                message=f"Azure {resource['resource_type']} {resource['name']} is {state} "
                f"(was {previous_state or 'unknown'})",
            )
            await self._emit(event)
        elif previous_state in _UNHEALTHY_STATES and state == "Available":
            event = AzureEvent(
                event_type="azure_resource_recovered",
                severity="info",
                resource_group=resource["resource_group"],
                resource_name=resource["name"],
                resource_type=resource["resource_type"],
                subscription_id=resource["subscription_id"],
                resource_id=resource_id,
                availability_state=state,
                message=f"Azure {resource['resource_type']} {resource['name']} recovered "
                f"(was {previous_state})",
            )
            await self._emit(event)

    async def _check_activity_logs(self, client: Any) -> None:
        """Check Activity Log per resource group for VM deallocate/poweroff entries."""
        config = load_azure_resources_config()
        lookback_hours = (
            config.get("azure", {}).get("monitoring", {}).get("activity_log_lookback_hours", 24)
        )
        subscriptions = config.get("azure", {}).get("subscriptions", [])

        for sub_cfg in subscriptions:
            sub_id = sub_cfg.get("subscription_id")
            for rg in sub_cfg.get("resource_group_scope", []):
                try:
                    entries = await client.activity_log(rg, lookback_hours, sub_id)
                except Exception as e:
                    logger.warning(
                        "azure_watchloop_activity_log_failed", resource_group=rg, error=str(e)
                    )
                    continue

                for entry in entries:
                    operation = (entry.get("operation_name") or "").lower()
                    if entry.get("status") != "Succeeded" or not any(
                        op in operation for op in _DEALLOCATE_OPERATIONS
                    ):
                        continue

                    resource_id = entry.get("resource_id") or ""
                    issue_key = f"{resource_id}:{entry.get('event_timestamp')}"
                    if issue_key in self._known_deallocations:
                        continue
                    self._known_deallocations.add(issue_key)

                    resource_name = resource_id.rsplit("/", 1)[-1] if resource_id else "unknown"
                    event = AzureEvent(
                        event_type="azure_vm_deallocated",
                        severity="critical",
                        resource_group=rg,
                        resource_name=resource_name,
                        resource_type="vm",
                        subscription_id=sub_id,
                        resource_id=resource_id,
                        message=f"Azure VM {resource_name} was deallocated/powered off "
                        f"(operation: {entry.get('operation_name')})",
                    )
                    await self._emit(event)

    async def _emit(self, event: AzureEvent) -> None:
        logger.info(
            "azure_event_detected",
            event_type=event.event_type,
            resource_name=event.resource_name,
            resource_group=event.resource_group,
        )
        if self._event_callback:
            try:
                await self._event_callback(event)
            except Exception as e:
                logger.error(
                    "azure_event_callback_failed",
                    resource_name=event.resource_name,
                    error=str(e),
                )
