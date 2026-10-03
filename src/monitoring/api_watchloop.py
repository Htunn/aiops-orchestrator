"""
API Backend Watchloop - background polling for external API health monitoring.

Monitors configured API endpoints for downtime, high latency, and error rates.
Publishes events to the same alert queue as K8s watchloop for unified remediation.
"""

import asyncio
import os
from collections import defaultdict, deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from src.models.api_backend import ApiBackendConfig, ApiBackendStatus

logger = structlog.get_logger()


@dataclass
class ApiBackendEvent:
    """A detected API backend anomaly event."""

    event_type: str  # api_backend_down | api_high_latency | api_high_error_rate | api_ssl_expiring
    severity: str  # critical | warning | info
    endpoint_name: str
    url: str
    message: str
    tags: dict[str, Any] = field(default_factory=dict)
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for rule engine evaluation."""
        return {
            "event_type": self.event_type,
            "severity": self.severity,
            "endpoint_name": self.endpoint_name,
            "url": self.url,
            "message": self.message,
            "tags": self.tags,
            "detected_at": self.detected_at.isoformat(),
            "metadata": self.metadata,
            # Compatibility with K8s event structure
            "namespace": self.tags.get("namespace", ""),
            "resource_kind": "ApiBackend",
            "resource_name": self.endpoint_name,
        }


class ApiBackendWatchLoop:
    """
    Background watchloop for monitoring external API backends.

    Performs periodic health checks on configured endpoints and emits events
    when issues are detected (downtime, high latency, elevated error rates).

    Usage:
        loop = ApiBackendWatchLoop(
            backends=[ApiBackendConfig(...)],
            event_callback=my_handler
        )
        await loop.start()
        # ... application runs ...
        await loop.stop()
    """

    def __init__(
        self,
        backends: list[ApiBackendConfig],
        event_callback: Callable[[ApiBackendEvent], Coroutine] | None = None,
    ) -> None:
        self._backends = {b.name: b for b in backends if b.enabled}
        self._event_callback = event_callback
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._event_queue: asyncio.Queue[ApiBackendEvent] = asyncio.Queue(maxsize=100)
        self._consumer_task: asyncio.Task | None = None

        # State tracking per endpoint
        self._status: dict[str, ApiBackendStatus] = {}
        self._consecutive_failures: dict[str, int] = defaultdict(int)
        self._known_issues: dict[str, datetime] = {}  # endpoint_name:event_type -> first_seen
        self._latency_history: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=10)
        )  # Rolling window for p95 calculation
        self._error_history: dict[str, deque[bool]] = defaultdict(
            lambda: deque(maxlen=10)
        )  # Rolling window for error rate

    async def start(self) -> None:
        """Start the watchloop background tasks."""
        if self._running:
            return

        if not self._backends:
            logger.info("api_watchloop_no_backends", msg="No API backends configured")
            return

        self._running = True

        # Start consumer task
        self._consumer_task = asyncio.create_task(
            self._consume_events(), name="api-watchloop-consumer"
        )

        # Start monitoring task for each backend
        for backend in self._backends.values():
            task = asyncio.create_task(
                self._monitor_backend(backend), name=f"api-watchloop-{backend.name}"
            )
            self._tasks.append(task)

        logger.info(
            "api_watchloop_started",
            backend_count=len(self._backends),
            endpoints=[b.name for b in self._backends.values()],
        )

    async def stop(self) -> None:
        """Stop the watchloop."""
        self._running = False

        # Cancel all monitoring tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Cancel consumer task
        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

        logger.info("api_watchloop_stopped")

    @property
    def is_running(self) -> bool:
        """Check if watchloop is running."""
        return self._running and bool(self._tasks) and any(not t.done() for t in self._tasks)

    def get_status(self, endpoint_name: str | None = None) -> dict[str, ApiBackendStatus]:
        """Get current status of monitored endpoints."""
        if endpoint_name:
            return (
                {endpoint_name: self._status[endpoint_name]}
                if endpoint_name in self._status
                else {}
            )
        return self._status.copy()

    async def _monitor_backend(self, backend: ApiBackendConfig) -> None:
        """Monitor a single API backend continuously."""
        logger.info(
            "api_backend_monitoring_started",
            name=backend.name,
            url=str(backend.url),
            interval=backend.check_interval_seconds,
        )

        while self._running:
            try:
                await self._check_backend(backend)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "api_backend_check_error",
                    name=backend.name,
                    error=str(e),
                    error_type=type(e).__name__,
                )

            await asyncio.sleep(backend.check_interval_seconds)

    async def _check_backend(self, backend: ApiBackendConfig) -> None:
        """Perform a single health check on an API backend."""
        start_time = datetime.now(UTC)
        check_url = f"{str(backend.url).rstrip('/')}{backend.health_path}"

        # Resolve environment variables in headers
        resolved_headers = {}
        for key, value in backend.headers.items():
            if value.startswith("${") and value.endswith("}"):
                env_var = value[2:-1]
                resolved_headers[key] = os.getenv(env_var, "")
            else:
                resolved_headers[key] = value

        try:
            async with httpx.AsyncClient(
                timeout=backend.timeout_seconds, verify=backend.ssl_verify
            ) as client:
                response = await client.get(check_url, headers=resolved_headers)

                latency_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
                is_success = 200 <= response.status_code < 300
                is_error = response.status_code >= 500

                # Update status
                self._status[backend.name] = ApiBackendStatus(
                    name=backend.name,
                    url=str(backend.url),
                    is_up=is_success,
                    last_check_time=start_time.isoformat(),
                    last_latency_ms=latency_ms,
                    consecutive_failures=0
                    if is_success
                    else self._consecutive_failures[backend.name] + 1,
                    last_error=None if is_success else f"HTTP {response.status_code}",
                    metadata={"status_code": response.status_code},
                )

                # Update history
                self._latency_history[backend.name].append(latency_ms)
                self._error_history[backend.name].append(is_error)

                # Check for issues and emit events
                if is_success:
                    # Clear consecutive failures on success
                    self._consecutive_failures[backend.name] = 0
                    # Clear resolved issues
                    issue_key = f"{backend.name}:api_backend_down"
                    self._known_issues.pop(issue_key, None)
                else:
                    # Increment consecutive failures
                    self._consecutive_failures[backend.name] += 1

                    # Check if we've crossed the threshold
                    if (
                        self._consecutive_failures[backend.name]
                        >= backend.consecutive_failures_threshold
                    ):
                        issue_key = f"{backend.name}:api_backend_down"
                        if issue_key not in self._known_issues:
                            self._known_issues[issue_key] = start_time
                            await self._emit_event(
                                ApiBackendEvent(
                                    event_type="api_backend_down",
                                    severity="critical",
                                    endpoint_name=backend.name,
                                    url=str(backend.url),
                                    message=f"API backend {backend.name} is down (HTTP {response.status_code}, {self._consecutive_failures[backend.name]} consecutive failures)",
                                    tags=backend.tags,
                                    metadata={
                                        "status_code": response.status_code,
                                        "consecutive_failures": self._consecutive_failures[
                                            backend.name
                                        ],
                                        "latency_ms": latency_ms,
                                    },
                                )
                            )

                # Check latency threshold (p95 from last 10 checks)
                if len(self._latency_history[backend.name]) >= 5:
                    sorted_latencies = sorted(self._latency_history[backend.name])
                    p95_latency = sorted_latencies[int(len(sorted_latencies) * 0.95)]
                    if p95_latency > backend.latency_threshold_ms:
                        issue_key = f"{backend.name}:api_high_latency"
                        if issue_key not in self._known_issues:
                            self._known_issues[issue_key] = start_time
                            await self._emit_event(
                                ApiBackendEvent(
                                    event_type="api_high_latency",
                                    severity="warning",
                                    endpoint_name=backend.name,
                                    url=str(backend.url),
                                    message=f"API backend {backend.name} has high latency (p95: {p95_latency:.0f}ms, threshold: {backend.latency_threshold_ms}ms)",
                                    tags=backend.tags,
                                    metadata={
                                        "p95_latency_ms": p95_latency,
                                        "threshold_ms": backend.latency_threshold_ms,
                                        "current_latency_ms": latency_ms,
                                    },
                                )
                            )
                    else:
                        # Clear resolved latency issue
                        issue_key = f"{backend.name}:api_high_latency"
                        self._known_issues.pop(issue_key, None)

                # Check error rate (over last 10 checks)
                if len(self._error_history[backend.name]) >= 5:
                    error_count = sum(self._error_history[backend.name])
                    error_rate = error_count / len(self._error_history[backend.name])
                    self._status[backend.name].error_rate = error_rate

                    if error_rate > backend.error_rate_threshold:
                        issue_key = f"{backend.name}:api_high_error_rate"
                        if issue_key not in self._known_issues:
                            self._known_issues[issue_key] = start_time
                            await self._emit_event(
                                ApiBackendEvent(
                                    event_type="api_high_error_rate",
                                    severity="critical",
                                    endpoint_name=backend.name,
                                    url=str(backend.url),
                                    message=f"API backend {backend.name} has high error rate ({error_rate:.1%}, threshold: {backend.error_rate_threshold:.1%})",
                                    tags=backend.tags,
                                    metadata={
                                        "error_rate": error_rate,
                                        "threshold": backend.error_rate_threshold,
                                        "errors_in_window": error_count,
                                        "window_size": len(self._error_history[backend.name]),
                                    },
                                )
                            )
                    else:
                        # Clear resolved error rate issue
                        issue_key = f"{backend.name}:api_high_error_rate"
                        self._known_issues.pop(issue_key, None)

        except (httpx.TimeoutException, httpx.ConnectTimeout, httpx.ReadTimeout):
            # Timeout is treated as a failure
            self._consecutive_failures[backend.name] += 1
            latency_ms = backend.timeout_seconds * 1000

            self._status[backend.name] = ApiBackendStatus(
                name=backend.name,
                url=str(backend.url),
                is_up=False,
                last_check_time=start_time.isoformat(),
                last_latency_ms=latency_ms,
                consecutive_failures=self._consecutive_failures[backend.name],
                last_error=f"Timeout after {backend.timeout_seconds}s",
            )

            if self._consecutive_failures[backend.name] >= backend.consecutive_failures_threshold:
                issue_key = f"{backend.name}:api_backend_down"
                if issue_key not in self._known_issues:
                    self._known_issues[issue_key] = start_time
                    await self._emit_event(
                        ApiBackendEvent(
                            event_type="api_backend_down",
                            severity="critical",
                            endpoint_name=backend.name,
                            url=str(backend.url),
                            message=f"API backend {backend.name} is unreachable (timeout after {backend.timeout_seconds}s, {self._consecutive_failures[backend.name]} consecutive failures)",
                            tags=backend.tags,
                            metadata={
                                "error_type": "timeout",
                                "timeout_seconds": backend.timeout_seconds,
                                "consecutive_failures": self._consecutive_failures[backend.name],
                            },
                        )
                    )

        except (httpx.ConnectError, httpx.NetworkError) as e:
            # Network error
            self._consecutive_failures[backend.name] += 1

            self._status[backend.name] = ApiBackendStatus(
                name=backend.name,
                url=str(backend.url),
                is_up=False,
                last_check_time=start_time.isoformat(),
                consecutive_failures=self._consecutive_failures[backend.name],
                last_error=f"Network error: {str(e)}",
            )

            if self._consecutive_failures[backend.name] >= backend.consecutive_failures_threshold:
                issue_key = f"{backend.name}:api_backend_down"
                if issue_key not in self._known_issues:
                    self._known_issues[issue_key] = start_time
                    await self._emit_event(
                        ApiBackendEvent(
                            event_type="api_backend_down",
                            severity="critical",
                            endpoint_name=backend.name,
                            url=str(backend.url),
                            message=f"API backend {backend.name} is unreachable (network error: {str(e)}, {self._consecutive_failures[backend.name]} consecutive failures)",
                            tags=backend.tags,
                            metadata={
                                "error_type": "network",
                                "error_message": str(e),
                                "consecutive_failures": self._consecutive_failures[backend.name],
                            },
                        )
                    )

    async def _emit_event(self, event: ApiBackendEvent) -> None:
        """Emit an event to the queue for processing."""
        try:
            self._event_queue.put_nowait(event)
            logger.info(
                "api_backend_event_emitted",
                event_type=event.event_type,
                endpoint=event.endpoint_name,
                severity=event.severity,
            )
        except asyncio.QueueFull:
            logger.warning(
                "api_backend_event_queue_full",
                endpoint=event.endpoint_name,
                event_type=event.event_type,
            )

    async def _consume_events(self) -> None:
        """Consume events from the queue and call the event callback."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                if self._event_callback:
                    try:
                        await self._event_callback(event)
                    except Exception as e:
                        logger.error(
                            "api_backend_event_callback_error",
                            endpoint=event.endpoint_name,
                            error=str(e),
                        )
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break
