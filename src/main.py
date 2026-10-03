"""Main application entry point."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import src.monitoring.metrics as _metrics  # noqa: F401 — registers Prometheus metrics on import
from src.ai import AIRouter
from src.api import health_router, incidents_router, limiter, set_message_router, webhook_router
from src.api.middleware import ContentSizeLimitMiddleware, CorrelationIdMiddleware
from src.channels import create_router
from src.config import get_settings, load_agents_config
from src.database import close_db, close_redis, init_db, init_redis
from src.monitoring.tracing import instrument_fastapi, setup_tracing, shutdown_tracing
from src.services import MessageHandler
from src.utils import configure_logging

logger = structlog.get_logger()
settings = get_settings()

# Global instances
router: Any = None
handler: Any = None
watchloop: Any = None
api_watchloop: Any = None
platform_watchloop: Any = None
azure_watchloop: Any = None
approval_manager: Any = None
mcp_manager: Any = None
playbook_executor: Any = None
agent_registry: Any = None


def _describe_event_resource(event_dict: dict[str, Any]) -> str:
    """
    Build a human-readable resource descriptor from a shared event dict (SPEC-003).

    All watch-loop events guarantee `event_type`/`severity`/`message`/`detected_at`
    plus exactly one domain-scoping field (`namespace` for K8s, `platform_name` for
    VM platforms, `resource_group` for Azure) — see docs' shared event contract.
    """
    if "resource_kind" in event_dict:
        scope = f" in `{event_dict['namespace']}`" if event_dict.get("namespace") else ""
        return (
            f"`{event_dict['resource_kind']}/{event_dict.get('resource_name', 'unknown')}`{scope}"
        )
    if "platform_name" in event_dict:
        return f"platform `{event_dict['platform_name']}`"
    if "resource_group" in event_dict:
        kind = event_dict.get("resource_type", "resource")
        return f"`{kind}/{event_dict.get('resource_name', 'unknown')}` in `{event_dict['resource_group']}`"
    return f"`{event_dict.get('resource_name', 'unknown')}`"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    global \
        router, \
        handler, \
        watchloop, \
        api_watchloop, \
        platform_watchloop, \
        azure_watchloop, \
        approval_manager, \
        mcp_manager, \
        playbook_executor, \
        agent_registry

    logger.info("starting_application", environment=settings.environment)

    # Configure logging
    configure_logging(settings.log_level)

    # Initialise OpenTelemetry tracing (no-op when otel_enabled=False)
    if settings.otel_enabled:
        setup_tracing(settings)
        instrument_fastapi(app)
        logger.info("otel_tracing_enabled", service=settings.otel_service_name)

    # Run database migrations (idempotent — applies only pending revisions)
    logger.info("running_db_migrations")
    try:
        alembic_cfg = AlembicConfig("alembic.ini")
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: alembic_command.upgrade(alembic_cfg, "head")
        )
        logger.info("db_migrations_complete")
    except Exception as e:
        logger.warning("db_migrations_failed", error=str(e))

    # Initialize database
    logger.info("initializing_database")
    await init_db()

    # Initialize Redis
    logger.info("initializing_redis")
    await init_redis()

    # Create message router and adapters
    logger.info("creating_message_router")
    router = create_router()

    # Create AI router (dispatches to GitHub Models or Gemini based on model prefix)
    logger.info("initializing_ai_router")
    ai_client = AIRouter()

    # Create message handler
    logger.info("creating_message_handler")
    handler = MessageHandler(router, ai_client)

    # Set handler for router
    router.set_message_handler(handler.handle_message)

    # Set router for webhook endpoints
    set_message_router(router)

    # ──────────────────────────────────────────────────────────────
    # AIOps: initialise approval manager (requires Redis + MCPManager)
    # ──────────────────────────────────────────────────────────────
    try:
        from src.database.redis import get_redis
        from src.mcp.mcp_manager import MCPManager
        from src.services.approval_manager import ApprovalManager

        mcp_manager = MCPManager()
        await mcp_manager.start()

        approval_manager = ApprovalManager(redis_client=get_redis(), mcp_manager=mcp_manager)
        # Expose on handler so NLP layer can forward approval responses
        handler.approval_manager = approval_manager
        # Azure ARM actions (SPEC-002) are gated by the same approval manager
        handler.azure_handler.approval_manager = approval_manager
        logger.info("approval_manager_initialized")
    except Exception as e:
        logger.warning("approval_manager_init_failed", error=str(e))
        approval_manager = None

    # ──────────────────────────────────────────────────────────────
    # A2A: Expose task delegator to message handler (if enabled)
    # ──────────────────────────────────────────────────────────────
    if settings.a2a_enabled:
        try:
            from src.services.task_delegator import get_task_delegator

            handler.task_delegator = get_task_delegator()
            logger.info("a2a_task_delegator_exposed_to_handler")
        except Exception as e:
            logger.warning("a2a_task_delegator_init_failed", error=str(e))

    # ──────────────────────────────────────────────────────────────
    # AIOps: Rule Engine + Playbook Executor (SPEC-003 — shared by every
    # watch-loop: K8s, API backends, VM platforms, Azure. Built unconditionally
    # so any one domain's watch-loop can be enabled independently of the others.
    # ──────────────────────────────────────────────────────────────
    try:
        from src.aiops.playbooks import PlaybookExecutor, PlaybookRegistry
        from src.aiops.rule_engine import RuleEngine

        rule_engine = RuleEngine()
        # Hoist registry + executor once at startup — not per-event
        _pb_registry = PlaybookRegistry()
        playbook_executor = PlaybookExecutor(
            registry=_pb_registry,
            mcp_manager=mcp_manager,
            approval_manager=approval_manager,
            notify_callback=router.send_message if router else None,
        )

        # ── AIOps intelligence layer (SPEC-006): RCA + log analysis + incident
        # persistence, generalized across all four domains and shared by every
        # watch-loop via _on_cluster_event below.
        from src.aiops.log_analyzer import LogAnalyzer
        from src.aiops.rca_engine import RCAEngine

        rca_engine = RCAEngine(ai_client=ai_client)
        log_analyzer = LogAnalyzer()

        async def _on_cluster_event(event: Any) -> None:
            """Route watch-loop events → rule engine → approval / auto-remediation."""
            try:
                event_dict = event.to_dict()
                matches = rule_engine.evaluate(event_dict)
                if not matches:
                    return

                # ── RCA + log analysis + incident persistence (SPEC-006) ──
                # Independently try/excepted so a diagnosis/persistence failure
                # never blocks the alert + remediation flow below.
                rca_report = None
                log_result = None
                incident_id = None
                try:
                    rca_report = await rca_engine.analyze(event_dict)

                    log_text = event_dict.get("logs") or event_dict.get("activity_log")
                    if log_text:
                        if not isinstance(log_text, str):
                            log_text = "\n".join(str(line) for line in log_text)
                        source_id = _describe_event_resource(event_dict).strip("`")
                        log_result = await log_analyzer.analyze_text_with_ai(
                            source_id=source_id,
                            source_label=source_id,
                            logs=log_text,
                            ai_client=ai_client,
                            context_label=f"log output for {source_id}",
                        )

                    if settings.aiops_incident_persistence_enabled:
                        from datetime import UTC, datetime

                        from src.database.models import Incident
                        from src.database.postgres import get_db_session

                        extra_data = {
                            k: v
                            for k, v in {
                                "platform_name": event_dict.get("platform_name"),
                                "resource_group": event_dict.get("resource_group"),
                                "subscription_id": event_dict.get("subscription_id"),
                                "failure_pattern": (
                                    rca_report.failure_pattern if rca_report else None
                                ),
                                "log_summary": log_result.summary if log_result else None,
                            }.items()
                            if v is not None
                        }
                        async with get_db_session() as session:
                            incident = Incident(
                                title=(
                                    f"{event.event_type}: {_describe_event_resource(event_dict)}"
                                ),
                                severity=event.severity,
                                status="investigating",
                                event_type=event.event_type,
                                namespace=event_dict.get("namespace"),
                                resource_kind=event_dict.get("resource_kind"),
                                resource_name=event_dict.get("resource_name"),
                                root_cause=rca_report.root_cause if rca_report else None,
                                rca_confidence=rca_report.confidence if rca_report else None,
                                extra_data=extra_data,
                            )
                            session.add(incident)
                            await session.flush()
                            incident_id = incident.id
                except Exception as rca_exc:
                    logger.error("aiops_rca_pipeline_error", error=str(rca_exc))

                # Notify AIOps channel about detected issue
                if settings.aiops_notification_channel:
                    parts = settings.aiops_notification_channel.split(":", 1)
                    if len(parts) == 2:
                        ch_type, ch_id = parts
                        icon = {
                            "critical": "🚨",
                            "high": "🔴",
                            "medium": "🟡",
                            "low": "🔵",
                        }.get(event.severity, "⚠️")
                        playbook_names = [r for _, r in matches]
                        alert_msg = (
                            f"{icon} **AIOps Alert** [{event.severity.upper()}]\n"
                            f"Type: `{event.event_type}`\n"
                            f"Resource: {_describe_event_resource(event_dict)}"
                            f"\n{event.message}"
                        )
                        if matches:
                            alert_msg += f"\n\n🔧 Playbooks queued: `{', '.join(playbook_names)}`"
                            if approval_manager:
                                alert_msg += "\nHigh-risk steps will require your approval."
                        if rca_report:
                            alert_msg += f"\n\n{rca_report.to_markdown()}"
                        if log_result and log_result.detected_patterns:
                            alert_msg += f"\n\n{log_result.to_markdown()}"
                        await router.send_message(ch_type, ch_id, alert_msg)

                # Execute playbooks via PlaybookExecutor
                any_playbook_completed = False
                if settings.auto_remediation_enabled and playbook_executor:
                    ch_type, ch_id = "", ""
                    if settings.aiops_notification_channel:
                        parts = settings.aiops_notification_channel.split(":", 1)
                        if len(parts) == 2:
                            ch_type, ch_id = parts

                    for _, playbook_id in matches:
                        try:
                            run = await playbook_executor.execute(
                                playbook_id=playbook_id,
                                incident_context=event_dict,
                                channel_type=ch_type,
                                channel_target=ch_id,
                                requested_by="watchloop",
                            )
                            if run.status == "completed":
                                any_playbook_completed = True
                            logger.info(
                                "playbook_run_finished",
                                playbook=playbook_id,
                                status=run.status,
                                steps_done=len(run.step_outputs),
                            )
                        except Exception as pb_exc:
                            logger.error(
                                "playbook_execution_error",
                                playbook=playbook_id,
                                error=str(pb_exc),
                            )

                if incident_id and any_playbook_completed:
                    try:
                        from datetime import UTC, datetime

                        from src.database.models import Incident
                        from src.database.postgres import get_db_session

                        async with get_db_session() as session:
                            incident = await session.get(Incident, incident_id)
                            if incident:
                                incident.status = "resolved"
                                incident.resolved_at = datetime.now(UTC)
                    except Exception as resolve_exc:
                        logger.error("aiops_incident_resolve_error", error=str(resolve_exc))
            except Exception as exc:
                logger.error("watchloop_event_handler_error", error=str(exc))

        logger.info("aiops_rule_engine_and_playbook_executor_initialized")
    except Exception as e:
        logger.warning("aiops_pipeline_init_failed", error=str(e))
        rule_engine = None
        playbook_executor = None
        rca_engine = None
        log_analyzer = None
        _on_cluster_event = None

    # ──────────────────────────────────────────────────────────────
    # AIOps: K8s watch-loop (proactive cluster health polling)
    # ──────────────────────────────────────────────────────────────
    if settings.k8s_watchloop_enabled and _on_cluster_event:
        try:
            from src.monitoring.watchloop import K8sWatchLoop

            watchloop = K8sWatchLoop(
                event_callback=_on_cluster_event,
                interval=settings.k8s_watchloop_interval,
            )
            asyncio.create_task(watchloop.start())
            logger.info(
                "k8s_watchloop_started",
                interval=settings.k8s_watchloop_interval,
            )
        except Exception as e:
            logger.warning("k8s_watchloop_init_failed", error=str(e))
            watchloop = None

    # AIOps: API Backend watch-loop (external service health monitoring)
    if settings.api_backend_monitoring_enabled and _on_cluster_event:
        try:
            from src.config import load_api_backend_configs
            from src.monitoring.api_watchloop import ApiBackendWatchLoop

            api_backends = load_api_backend_configs()
            if not api_backends:
                logger.info(
                    "api_watchloop_no_backends",
                    msg="No API backends configured in config/api_backends.yml",
                )
            else:
                # Reuse the same event callback as K8s watchloop — unified event handling
                api_watchloop = ApiBackendWatchLoop(
                    backends=api_backends,
                    event_callback=_on_cluster_event,  # Same callback handles both K8s and API events
                )
                asyncio.create_task(api_watchloop.start())
                logger.info(
                    "api_watchloop_started",
                    backend_count=len(api_backends),
                )
        except Exception as e:
            logger.warning("api_watchloop_init_failed", error=str(e))
            api_watchloop = None

    # ──────────────────────────────────────────────────────────────
    # AIOps: Platform (Nutanix/VMware/OpenShift) watch-loop (SPEC-003)
    # ──────────────────────────────────────────────────────────────
    if settings.platform_watchloop_enabled and _on_cluster_event:
        try:
            from src.monitoring.platform_watchloop import PlatformWatchLoop

            platform_watchloop = PlatformWatchLoop(
                event_callback=_on_cluster_event,  # Same callback handles every domain
                interval=settings.platform_watchloop_interval,
            )
            asyncio.create_task(platform_watchloop.start())
            logger.info(
                "platform_watchloop_started",
                interval=settings.platform_watchloop_interval,
            )
        except Exception as e:
            logger.warning("platform_watchloop_init_failed", error=str(e))
            platform_watchloop = None

    # ──────────────────────────────────────────────────────────────
    # AIOps: Azure resource watch-loop (SPEC-003)
    # ──────────────────────────────────────────────────────────────
    if (
        settings.azure_watchloop_enabled
        and settings.azure_integration_enabled
        and _on_cluster_event
    ):
        try:
            from src.monitoring.azure_watchloop import AzureWatchLoop

            azure_watchloop = AzureWatchLoop(event_callback=_on_cluster_event)
            asyncio.create_task(azure_watchloop.start())
            logger.info("azure_watchloop_started")
        except Exception as e:
            logger.warning("azure_watchloop_init_failed", error=str(e))
            azure_watchloop = None

        try:
            from src.services.agent_registry import get_agent_registry

            agent_registry = get_agent_registry()
            logger.info("agent_registry_initialized")

            # Load and register agents from config file if auto-register enabled
            agents_config = load_agents_config()
            if agents_config and agents_config.get("settings", {}).get(
                "auto_register_on_startup", False
            ):
                from src.database.postgres import get_db_session

                async with get_db_session() as db:
                    registered_count = 0
                    for agent_config in agents_config.get("agents", []):
                        try:
                            from src.models.agent import AgentCapability

                            # Convert capabilities dict to AgentCapability objects
                            capabilities = [
                                AgentCapability(**cap)
                                for cap in agent_config.get("capabilities", [])
                            ]

                            # Check if agent already registered
                            existing = await agent_registry.get_agent(
                                agent_id=agent_config["agent_id"], db=db
                            )

                            if not existing:
                                # Register new agent
                                agent_info, api_key = await agent_registry.register_agent(
                                    agent_id=agent_config["agent_id"],
                                    name=agent_config["name"],
                                    url=agent_config["url"],
                                    capabilities=capabilities,
                                    webhook_url=agent_config.get("webhook_url"),
                                    version=agent_config.get("version", "1.0.0"),
                                    metadata=agent_config.get("metadata", {}),
                                    db=db,
                                )
                                registered_count += 1
                                logger.info(
                                    "a2a_agent_registered_from_config",
                                    agent_id=agent_config["agent_id"],
                                    name=agent_config["name"],
                                )
                            else:
                                logger.debug(
                                    "a2a_agent_already_registered",
                                    agent_id=agent_config["agent_id"],
                                )
                        except Exception as agent_err:
                            logger.warning(
                                "a2a_agent_registration_failed",
                                agent_id=agent_config.get("agent_id", "unknown"),
                                error=str(agent_err),
                            )

                    logger.info("a2a_agents_loaded_from_config", count=registered_count)

        except Exception as e:
            logger.warning("a2a_init_failed", error=str(e))
            agent_registry = None

    # Start all channel adapters
    logger.info("starting_channel_adapters")
    asyncio.create_task(router.start_all())

    logger.info("application_started_successfully")

    yield

    # Shutdown
    logger.info("shutting_down_application")

    # Stop K8s watch-loop
    if watchloop:
        await watchloop.stop()
        logger.info("k8s_watchloop_stopped")

    # Stop API backend watch-loop
    if api_watchloop:
        await api_watchloop.stop()
        logger.info("api_watchloop_stopped")

    # Stop Platform watch-loop
    if platform_watchloop:
        await platform_watchloop.stop()
        logger.info("platform_watchloop_stopped")

    # Stop Azure watch-loop
    if azure_watchloop:
        await azure_watchloop.stop()
        logger.info("azure_watchloop_stopped")

    # Stop channel adapters
    await router.stop_all()

    # Close MCP manager and all servers
    if mcp_manager:
        await mcp_manager.stop()

    # Flush OTel spans before closing connections
    if settings.otel_enabled:
        shutdown_tracing()

    # Close database connections
    await close_db()

    # Close Redis connection
    await close_redis()

    logger.info("application_shutdown_complete")


def get_agent_registry() -> Any:
    """Return current agent registry instance (for health checks)."""
    return agent_registry


# Create FastAPI application
app = FastAPI(
    title="AIOps Orchestrator",
    description="Multi-channel AI agent with GitHub Models integration",
    version="0.1.0",
    lifespan=lifespan,
)

# Add request middleware (outermost first)
app.add_middleware(ContentSizeLimitMiddleware)
app.add_middleware(CorrelationIdMiddleware)

# Add rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

# Include routers
app.include_router(health_router, tags=["Health"])
app.include_router(webhook_router, prefix="/api", tags=["Webhooks"])
app.include_router(incidents_router)


def get_watchloop() -> Any:
    """Return current watchloop instance (for health checks)."""
    return watchloop


def get_api_watchloop() -> Any:
    """Return current API backend watchloop instance (for health checks)."""
    return api_watchloop


def get_platform_watchloop() -> Any:
    """Return current platform watchloop instance (for health checks)."""
    return platform_watchloop


def get_azure_watchloop() -> Any:
    """Return current Azure watchloop instance (for health checks)."""
    return azure_watchloop


def get_approval_manager() -> Any:
    """Return current approval manager instance."""
    return approval_manager


@app.get("/metrics", include_in_schema=False)
async def metrics() -> PlainTextResponse:
    """Prometheus metrics endpoint."""
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {
        "name": "AIOps Orchestrator",
        "version": "0.1.0",
        "status": "running",
        "environment": settings.environment,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.environment == "development",
    )
