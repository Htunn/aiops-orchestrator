"""
Platform Watchloop - background health monitoring for multi-cloud platforms.

Periodically checks health of all configured platforms (Nutanix, VMware, OpenShift)
and detects degraded or unreachable platforms for proactive alerting.
"""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from src.config import get_settings
from src.platforms import PlatformHealth
from src.services.platform_registry import get_platform_registry

logger = structlog.get_logger()
settings = get_settings()


@dataclass
class PlatformEvent:
    """A platform health event."""

    platform_name: str
    platform_type: str
    event_type: str  # health_degraded | health_recovered | unreachable | timeout
    severity: str  # critical | warning | info
    status: str  # healthy | degraded | unreachable
    message: str
    response_time_ms: float | None = None
    previous_status: str | None = None
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform_name": self.platform_name,
            "platform_type": self.platform_type,
            "event_type": self.event_type,
            "severity": self.severity,
            "status": self.status,
            "message": self.message,
            "response_time_ms": self.response_time_ms,
            "previous_status": self.previous_status,
            "detected_at": self.detected_at.isoformat(),
        }


class PlatformWatchLoop:
    """
    Background watchloop for platform health monitoring.

    On each tick:
    1. Check health of all configured platforms
    2. Detect status changes (healthy → degraded → unreachable)
    3. Measure response times for performance monitoring
    4. Publish events for degraded or unreachable platforms

    Usage:
        loop = PlatformWatchLoop(event_callback=my_handler)
        await loop.start()
        # ... application runs ...
        await loop.stop()
    """

    # Health check thresholds
    _RESPONSE_TIME_WARNING_MS = 2000  # Warn if response > 2s
    _RESPONSE_TIME_CRITICAL_MS = 5000  # Critical if response > 5s

    def __init__(
        self,
        event_callback: Callable[[PlatformEvent], Coroutine] | None = None,
        interval: int | None = None,
    ) -> None:
        """
        Initialize platform watchloop.

        Args:
            event_callback: Async callback for platform events
            interval: Check interval in seconds (default: 60)
        """
        self._event_callback = event_callback
        self._interval = interval or 60  # Default: check every minute
        self._task: asyncio.Task | None = None
        self._running = False
        self._platform_states: dict[str, str] = {}  # Track previous health status

    async def start(self) -> None:
        """Start the watchloop."""
        if self._running:
            logger.warning("platform_watchloop_already_running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("platform_watchloop_started", interval=self._interval)

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

        logger.info("platform_watchloop_stopped")

    async def _run_loop(self) -> None:
        """Main watchloop execution."""
        while self._running:
            try:
                await self._check_platforms()
            except Exception as e:
                logger.error("platform_watchloop_tick_failed", error=str(e), exc_info=True)

            # Sleep until next check
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def _check_platforms(self) -> None:
        """Check health of all platforms."""
        try:
            registry = await get_platform_registry()
            health_results = await registry.check_all_health()

            for platform_name, health in health_results.items():
                await self._process_health_result(platform_name, health)

            logger.info(
                "platform_health_check_completed",
                platforms_checked=len(health_results),
                healthy=sum(1 for h in health_results.values() if h.status == "healthy"),
                degraded=sum(1 for h in health_results.values() if h.status == "degraded"),
                unreachable=sum(1 for h in health_results.values() if h.status == "unreachable"),
            )

        except Exception as e:
            logger.error("platform_health_check_failed", error=str(e))

    async def _process_health_result(self, platform_name: str, health: PlatformHealth) -> None:
        """
        Process health check result and generate events.

        Args:
            platform_name: Name of the platform
            health: Health check result
        """
        previous_status = self._platform_states.get(platform_name)
        current_status = health.status

        # Update state
        self._platform_states[platform_name] = current_status

        # Detect status changes
        if previous_status and previous_status != current_status:
            await self._handle_status_change(platform_name, health, previous_status, current_status)

        # Check response time thresholds
        if health.response_time_ms is not None:
            await self._check_response_time(platform_name, health)

    async def _handle_status_change(
        self,
        platform_name: str,
        health: PlatformHealth,
        previous_status: str,
        current_status: str,
    ) -> None:
        """
        Handle platform status changes.

        Args:
            platform_name: Name of the platform
            health: Health check result
            previous_status: Previous health status
            current_status: Current health status
        """
        # Determine event type and severity
        if current_status == "unreachable":
            event_type = "unreachable"
            severity = "critical"
            message = f"Platform {platform_name} is unreachable (was {previous_status})"
        elif current_status == "degraded":
            event_type = "health_degraded"
            severity = "warning"
            message = f"Platform {platform_name} health degraded (was {previous_status})"
        elif current_status == "healthy" and previous_status in ("degraded", "unreachable"):
            event_type = "health_recovered"
            severity = "info"
            message = f"Platform {platform_name} health recovered (was {previous_status})"
        else:
            # No significant change
            return

        # Create event
        event = PlatformEvent(
            platform_name=platform_name,
            platform_type=health.platform,
            event_type=event_type,
            severity=severity,
            status=current_status,
            message=message,
            response_time_ms=health.response_time_ms,
            previous_status=previous_status,
        )

        logger.info(
            "platform_status_changed",
            platform_name=platform_name,
            previous_status=previous_status,
            current_status=current_status,
            event_type=event_type,
        )

        # Publish event
        if self._event_callback:
            try:
                await self._event_callback(event)
            except Exception as e:
                logger.error(
                    "platform_event_callback_failed",
                    platform_name=platform_name,
                    error=str(e),
                )

    async def _check_response_time(self, platform_name: str, health: PlatformHealth) -> None:
        """
        Check response time and generate events if thresholds exceeded.

        Args:
            platform_name: Name of the platform
            health: Health check result
        """
        if health.response_time_ms is None:
            return

        response_time = health.response_time_ms

        # Check critical threshold
        if response_time > self._RESPONSE_TIME_CRITICAL_MS:
            event = PlatformEvent(
                platform_name=platform_name,
                platform_type=health.platform,
                event_type="timeout",
                severity="critical",
                status=health.status,
                message=f"Platform {platform_name} response time critical: {response_time:.1f}ms",
                response_time_ms=response_time,
            )

            logger.warning(
                "platform_response_time_critical",
                platform_name=platform_name,
                response_time_ms=response_time,
                threshold_ms=self._RESPONSE_TIME_CRITICAL_MS,
            )

            if self._event_callback:
                try:
                    await self._event_callback(event)
                except Exception as e:
                    logger.error(
                        "platform_event_callback_failed",
                        platform_name=platform_name,
                        error=str(e),
                    )

        # Check warning threshold
        elif response_time > self._RESPONSE_TIME_WARNING_MS:
            logger.info(
                "platform_response_time_slow",
                platform_name=platform_name,
                response_time_ms=response_time,
                threshold_ms=self._RESPONSE_TIME_WARNING_MS,
            )

    def get_platform_states(self) -> dict[str, str]:
        """
        Get current platform health states.

        Returns:
            Dictionary mapping platform names to health status
        """
        return self._platform_states.copy()

    def is_running(self) -> bool:
        """
        Check if watchloop is running.

        Returns:
            True if running, False otherwise
        """
        return self._running
