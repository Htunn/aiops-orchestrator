"""Health check endpoints."""

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from src.config import get_settings
from src.database import get_redis
from src.database.postgres import engine

logger = structlog.get_logger()
router = APIRouter()
settings = get_settings()


class HealthResponse(BaseModel):
    """Health check response model."""

    status: str
    database: str
    redis: str
    kubernetes: str
    prometheus: str
    watchloop: str
    api_backends: str
    pending_approvals: int
    active_incidents: int


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Health check endpoint — covers DB, Redis, K8s, Prometheus, and AIOps subsystems."""
    db_status = "healthy"
    redis_status = "healthy"
    k8s_status = "disabled"
    prometheus_status = "disabled"
    api_backends_status = "disabled"
    watchloop_status = "disabled"
    pending_approvals = 0
    active_incidents = 0

    # ── Database ──────────────────────────────────────────────────
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"

    # ── Redis ─────────────────────────────────────────────────────
    try:
        redis_client = get_redis()
        await redis_client.ping()
    except Exception as e:
        redis_status = f"unhealthy: {str(e)}"

    # ── Kubernetes ───────────────────────────────────────────────
    if settings.k8s_watchloop_enabled:
        try:
            from src.k8s.client import get_k8s_client

            k8s_client = await get_k8s_client()
            ns_list = await k8s_client.list_namespaces()
            k8s_status = f"healthy ({len(ns_list)} namespaces)"
        except Exception as e:
            k8s_status = f"unhealthy: {str(e)}"

    # ── Watchloop ────────────────────────────────────────────────
    if settings.k8s_watchloop_enabled:
        try:
            from src.main import get_watchloop

            wl = get_watchloop()
            if wl is None:
                watchloop_status = "not_started"
            else:
                watchloop_status = "running" if wl.is_running else "stopped"
        except Exception as e:
            logger.error(f"Failed to check platform watchloop: {e}")
            watchloop_status = "error"

    # API Backends
    if settings.api_backend_monitoring_enabled:
        try:
            from src.main import get_api_watchloop

            api_wl = get_api_watchloop()
            if api_wl is None:
                api_backends_status = "not_started"
            else:
                if api_wl.is_running:
                    backend_status = api_wl.get_status()
                    total = len(backend_status)
                    up = sum(1 for s in backend_status.values() if s.is_up)
                    api_backends_status = f"running ({up}/{total} up)"
                else:
                    api_backends_status = "stopped"
        except Exception as e:
            api_backends_status = f"error: {str(e)}"

    # ──    watchloop_status = f"error: {str(e)}"

    # ── Prometheus ───────────────────────────────────────────────
    if settings.prometheus_url:
        try:
            from src.monitoring.prometheus import PrometheusClient

            prom = PrometheusClient()
            await prom.get_cluster_health_summary()
            # just confirm we got something without an exception
            prometheus_status = "healthy"
        except Exception as e:
            prometheus_status = f"unhealthy: {str(e)}"

    # ── Pending approvals (Redis scan) ───────────────────────────
    try:
        redis_client = get_redis()
        keys = await redis_client.keys("approval:*")
        pending_approvals = len(keys)
    except Exception:
        pending_approvals = 0

    # ── Active incidents (DB count) ───────────────────────────────
    if db_status == "healthy":
        try:
            async with engine.connect() as conn:
                row = await conn.execute(
                    text("SELECT COUNT(*) FROM incidents WHERE status = 'open'")
                )
                active_incidents = row.scalar() or 0
        except Exception:
            active_incidents = 0

    # ── Overall status ────────────────────────────────────────────
    degraded = any(v.startswith("unhealthy") for v in (db_status, redis_status))
    overall_status = "unhealthy" if degraded else "healthy"

    if overall_status == "unhealthy":
        raise HTTPException(status_code=503, detail="Service unhealthy")

    return HealthResponse(
        status=overall_status,
        database=db_status,
        redis=redis_status,
        kubernetes=k8s_status,
        prometheus=prometheus_status,
        watchloop=watchloop_status,
        api_backends=api_backends_status,
        pending_approvals=pending_approvals,
        active_incidents=active_incidents,
    )


@router.get("/ready")
async def readiness_check() -> dict:
    """Readiness check endpoint."""
    return {"ready": True}


@router.get("/health/aiops")
async def aiops_health() -> dict[str, Any]:
    """
    Detailed AIOps subsystem status.

    Returns watchloop state, Prometheus metrics, and pending approval IDs.
    """
    result: dict[str, Any] = {
        "watchloop_running": False,
        "prometheus_reachable": False,
        "grafana_reachable": False,
        "pending_approvals": [],
        "cluster_health": {},
    }

    # Watchloop
    try:
        from src.main import get_watchloop

        wl = get_watchloop()
        result["watchloop_running"] = bool(wl and wl.is_running)
    except Exception as e:
        logger.debug("aiops_health_watchloop_error", error=str(e))

    # Prometheus cluster health summary
    if settings.prometheus_url:
        try:
            from src.monitoring.prometheus import PrometheusClient

            prom = PrometheusClient()
            result["cluster_health"] = await prom.get_cluster_health_summary()
            result["prometheus_reachable"] = True
        except Exception as e:
            logger.debug("aiops_health_prometheus_error", error=str(e))

    # Grafana reachability (just a HEAD on the base URL)
    if settings.grafana_url:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{settings.grafana_url}/api/health")
                result["grafana_reachable"] = resp.status_code < 500
        except Exception as e:
            logger.debug("aiops_health_grafana_error", error=str(e))

    # Pending approvals
    try:
        redis_client = get_redis()
        keys = await redis_client.keys("approval:*")
        result["pending_approvals"] = [k.split(":")[-1] for k in keys]
    except Exception:
        pass

    return result


@router.get("/health/api-backends")
async def api_backends_health() -> dict[str, Any]:
    """
    Detailed API backend monitoring status.

    Returns status for each monitored API endpoint including:
    - Health (up/down)
    - Last check time
    - Latency metrics
    - Error rate
    - Consecutive failures
    """
    result: dict[str, Any] = {
        "monitoring_enabled": settings.api_backend_monitoring_enabled,
        "backends": {},
        "summary": {
            "total": 0,
            "up": 0,
            "down": 0,
            "degraded": 0,
        },
    }

    if not settings.api_backend_monitoring_enabled:
        return result

    try:
        from src.main import get_api_watchloop

        api_wl = get_api_watchloop()
        if api_wl and api_wl.is_running:
            backend_status = api_wl.get_status()
            result["summary"]["total"] = len(backend_status)

            for name, status in backend_status.items():
                is_degraded = (
                    status.error_rate > 0.05
                    or (status.last_latency_ms or 0) > 1000
                    or status.consecutive_failures > 0
                )

                if status.is_up:
                    if is_degraded:
                        result["summary"]["degraded"] += 1
                    else:
                        result["summary"]["up"] += 1
                else:
                    result["summary"]["down"] += 1

                result["backends"][name] = {
                    "url": status.url,
                    "is_up": status.is_up,
                    "status": "up"
                    if status.is_up and not is_degraded
                    else ("degraded" if status.is_up else "down"),
                    "last_check_time": status.last_check_time,
                    "last_latency_ms": status.last_latency_ms,
                    "consecutive_failures": status.consecutive_failures,
                    "error_rate": round(status.error_rate, 4),
                    "last_error": status.last_error,
                }
        else:
            result["monitoring_enabled"] = False
            result["reason"] = "Watchloop not running"

    except Exception as e:
        logger.debug("api_backends_health_error", error=str(e))
        result["error"] = str(e)

    return result


@router.get("/health/a2a")
async def a2a_health() -> dict[str, Any]:
    """
    Detailed A2A (Agent-to-Agent) status.

    Returns:
        - registered_agents: Total number of registered agents
        - online_agents: Number of online agents
        - capabilities: List of available capabilities
        - recent_delegations: Count of recent task delegations
    """
    result: dict[str, Any] = {
        "enabled": settings.a2a_enabled,
        "registered_agents": 0,
        "online_agents": 0,
        "capabilities": [],
        "recent_delegations_24h": 0,
    }

    if not settings.a2a_enabled:
        return result

    try:
        from src.main import get_agent_registry
        from src.database.postgres import get_db_session
        from src.models.agent import AgentStatus
        from sqlalchemy import select, func
        from src.database.models import AgentTask
        from datetime import datetime, timedelta, UTC

        registry = get_agent_registry()
        if registry is None:
            result["error"] = "Agent registry not initialized"
            return result

        async with get_db_session() as db:
            # Get all agents
            agents = await registry.list_agents(db=db)
            result["registered_agents"] = len(agents)
            result["online_agents"] = sum(
                1 for agent in agents if agent.status == AgentStatus.ONLINE
            )

            # Collect all unique capabilities
            capabilities_set = set()
            for agent in agents:
                for cap in agent.capabilities:
                    capabilities_set.add(cap.name)
            result["capabilities"] = sorted(list(capabilities_set))

            # Count recent delegations (last 24 hours)
            cutoff = datetime.now(UTC) - timedelta(hours=24)
            recent_count_result = await db.execute(
                select(func.count(AgentTask.id)).where(AgentTask.created_at >= cutoff)
            )
            result["recent_delegations_24h"] = recent_count_result.scalar() or 0

    except Exception as e:
        logger.error("a2a_health_check_failed", error=str(e))
        result["error"] = str(e)

    return result
