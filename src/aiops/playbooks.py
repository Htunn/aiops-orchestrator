"""
AIOps Playbooks - structured remediation step sequences.

Each playbook consists of ordered steps. Steps carry a risk level.
The PlaybookExecutor runs steps sequentially, routing high-risk steps
through the ApprovalManager before execution.
"""

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

import structlog

from src.config import get_settings
from src.monitoring.tracing import get_tracer

logger = structlog.get_logger()
_tracer = get_tracer(__name__)


class RiskLevel(StrEnum):
    LOW = "low"  # Execute immediately, notify after
    MEDIUM = "medium"  # Require human approval
    HIGH = "high"  # Require human approval + explicit confirmation


@dataclass
class PlaybookStep:
    """A single step in a remediation playbook."""

    name: str
    description: str
    risk_level: RiskLevel
    # Tool name + kwargs to call via MCPManager
    tool_name: str
    tool_params_template: dict[str, Any]  # May contain {placeholders} from context
    # Optional: expected output pattern to verify success
    success_pattern: str | None = None

    def resolve_params(self, context: dict[str, Any]) -> dict[str, Any]:
        """Fill template placeholders from incident context."""
        resolved = {}
        for k, v in self.tool_params_template.items():
            if isinstance(v, str):
                try:
                    resolved[k] = v.format(**context)
                except KeyError:
                    resolved[k] = v
            else:
                resolved[k] = v
        return resolved


@dataclass
class Playbook:
    """A named collection of remediation steps."""

    id: str
    name: str
    description: str
    steps: list[PlaybookStep]
    # Callback invoked on playbook completion/failure
    on_complete: Callable[[str, bool, str], Coroutine] | None = None


@dataclass
class PlaybookRun:
    """A running instance of a playbook for a specific incident."""

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    playbook_id: str = ""
    incident_context: dict[str, Any] = field(default_factory=dict)
    current_step: int = 0
    status: str = "pending"  # pending | running | awaiting_approval | completed | failed
    step_outputs: list[str] = field(default_factory=list)
    error: str | None = None


class PlaybookRegistry:
    """
    Registry of available remediation playbooks.

    Built-in playbooks cover common K8s failure scenarios.
    Custom playbooks can be registered at runtime.
    """

    def __init__(self) -> None:
        self._playbooks: dict[str, Playbook] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register built-in remediation playbooks."""

        self.register(
            Playbook(
                id="crash_loop_remediation",
                name="CrashLoop Remediation",
                description="Diagnose and remediate a CrashLoopBackOff pod",
                steps=[
                    PlaybookStep(
                        name="Describe Pod",
                        description="Gather pod conditions and events",
                        risk_level=RiskLevel.LOW,
                        tool_name="k8s_describe_resource",
                        tool_params_template={
                            "resource_type": "pod",
                            "resource_name": "{resource_name}",
                            "namespace": "{namespace}",
                        },
                    ),
                    PlaybookStep(
                        name="Fetch Recent Logs",
                        description="Get last 100 lines of logs for error analysis",
                        risk_level=RiskLevel.LOW,
                        tool_name="k8s_analyze_logs",
                        tool_params_template={
                            "pod_name": "{resource_name}",
                            "namespace": "{namespace}",
                            "tail_lines": 100,
                        },
                    ),
                    PlaybookStep(
                        name="Restart Pod",
                        description="Delete pod to trigger fresh restart (controller will recreate)",
                        risk_level=RiskLevel.MEDIUM,
                        tool_name="k8s_restart_pod",
                        tool_params_template={
                            "pod_name": "{resource_name}",
                            "namespace": "{namespace}",
                        },
                    ),
                    PlaybookStep(
                        name="Verify Recovery",
                        description="Check pod status after restart",
                        risk_level=RiskLevel.LOW,
                        tool_name="k8s_get_pods",
                        tool_params_template={"namespace": "{namespace}", "label_selector": ""},
                    ),
                ],
            )
        )

        self.register(
            Playbook(
                id="oom_kill_remediation",
                name="OOMKill Remediation",
                description="Increase memory limits for OOM-killed pods",
                steps=[
                    PlaybookStep(
                        name="Get Current Limits",
                        description="Describe deployment to see current memory limits",
                        risk_level=RiskLevel.LOW,
                        tool_name="k8s_describe_resource",
                        tool_params_template={
                            "resource_type": "deployment",
                            "resource_name": "{resource_name}",
                            "namespace": "{namespace}",
                        },
                    ),
                    PlaybookStep(
                        name="Increase Memory Limit",
                        description="Patch deployment to increase memory limit by 50%",
                        risk_level=RiskLevel.HIGH,
                        tool_name="k8s_patch_resource",
                        tool_params_template={
                            "resource_type": "deployment",
                            "resource_name": "{resource_name}",
                            "namespace": "{namespace}",
                            "patch": '{"spec":{"template":{"spec":{"containers":[{"name":"{resource_name}","resources":{"limits":{"memory":"1Gi"}}}]}}}}',
                        },
                    ),
                ],
            )
        )

        self.register(
            Playbook(
                id="deployment_rollback",
                name="Deployment Rollback",
                description="Roll back a failing deployment to the previous stable revision",
                steps=[
                    PlaybookStep(
                        name="Get Rollout History",
                        description="Show deployment revisions available for rollback",
                        risk_level=RiskLevel.LOW,
                        tool_name="k8s_get_rollout_history",
                        tool_params_template={
                            "deployment_name": "{resource_name}",
                            "namespace": "{namespace}",
                        },
                    ),
                    PlaybookStep(
                        name="Rollback Deployment",
                        description="Undo to previous stable revision",
                        risk_level=RiskLevel.HIGH,
                        tool_name="k8s_rollback_deployment",
                        tool_params_template={
                            "deployment_name": "{resource_name}",
                            "namespace": "{namespace}",
                        },
                    ),
                    PlaybookStep(
                        name="Check Rollout Status",
                        description="Verify rollback completed successfully",
                        risk_level=RiskLevel.LOW,
                        tool_name="k8s_rollout_status",
                        tool_params_template={
                            "deployment_name": "{resource_name}",
                            "namespace": "{namespace}",
                        },
                    ),
                ],
            )
        )

        self.register(
            Playbook(
                id="node_not_ready_remediation",
                name="Node NotReady Remediation",
                description="Drain and cordon a NotReady node",
                steps=[
                    PlaybookStep(
                        name="Describe Node",
                        description="Gather node conditions and events",
                        risk_level=RiskLevel.LOW,
                        tool_name="k8s_describe_resource",
                        tool_params_template={
                            "resource_type": "node",
                            "resource_name": "{resource_name}",
                            "namespace": "",
                        },
                    ),
                    PlaybookStep(
                        name="Cordon Node",
                        description="Prevent new pods from scheduling on this node",
                        risk_level=RiskLevel.MEDIUM,
                        tool_name="k8s_cordon_node",
                        tool_params_template={"node_name": "{resource_name}"},
                    ),
                    PlaybookStep(
                        name="Drain Node",
                        description="Evict all pods from the node",
                        risk_level=RiskLevel.HIGH,
                        tool_name="k8s_drain_node",
                        tool_params_template={"node_name": "{resource_name}"},
                    ),
                ],
            )
        )

        self.register(
            Playbook(
                id="scale_up_on_load",
                name="Scale Up Under Load",
                description="Increase replica count when HPA has hit maxReplicas",
                steps=[
                    PlaybookStep(
                        name="Scale Deployment",
                        description="Add replicas to handle increased load",
                        risk_level=RiskLevel.MEDIUM,
                        tool_name="k8s_scale_deployment",
                        tool_params_template={
                            "deployment": "{resource_name}",
                            "namespace": "{namespace}",
                            "replicas": "{target_replicas}",
                        },
                    ),
                ],
            )
        )

        # ── API Backend Remediation Playbooks ─────────────────────────────────

        self.register(
            Playbook(
                id="api_backend_down_remediation",
                name="API Backend Down Investigation",
                description="Diagnose and remediate an unreachable API backend",
                steps=[
                    PlaybookStep(
                        name="DNS Lookup",
                        description="Verify DNS resolution for the API endpoint",
                        risk_level=RiskLevel.LOW,
                        tool_name="api_dns_lookup",
                        tool_params_template={
                            "hostname": "{url}",
                        },
                    ),
                    PlaybookStep(
                        name="Curl Test",
                        description="Attempt direct HTTP request to API endpoint",
                        risk_level=RiskLevel.LOW,
                        tool_name="api_curl_test",
                        tool_params_template={
                            "url": "{url}",
                            "timeout": 10,
                        },
                    ),
                    PlaybookStep(
                        name="Check SSL Certificate",
                        description="Verify SSL certificate is valid and not expired",
                        risk_level=RiskLevel.LOW,
                        tool_name="api_ssl_check",
                        tool_params_template={
                            "url": "{url}",
                        },
                    ),
                ],
            )
        )

        self.register(
            Playbook(
                id="api_high_latency_investigation",
                name="API High Latency Investigation",
                description="Investigate causes of elevated API response times",
                steps=[
                    PlaybookStep(
                        name="Measure Current Latency",
                        description="Run multiple test requests to measure current latency",
                        risk_level=RiskLevel.LOW,
                        tool_name="api_curl_test",
                        tool_params_template={
                            "url": "{url}",
                            "timeout": 30,
                            "repeat": 5,
                        },
                    ),
                    PlaybookStep(
                        name="Check Network Path",
                        description="Trace route to API endpoint to identify network bottlenecks",
                        risk_level=RiskLevel.LOW,
                        tool_name="api_traceroute",
                        tool_params_template={
                            "hostname": "{url}",
                        },
                    ),
                ],
            )
        )

        self.register(
            Playbook(
                id="api_error_rate_investigation",
                name="API Error Rate Investigation",
                description="Investigate elevated error rates from API backend",
                steps=[
                    PlaybookStep(
                        name="Fetch Recent Errors",
                        description="Retrieve sample error responses from API",
                        risk_level=RiskLevel.LOW,
                        tool_name="api_curl_test",
                        tool_params_template={
                            "url": "{url}",
                            "timeout": 10,
                            "verbose": True,
                        },
                    ),
                    PlaybookStep(
                        name="Check API Status Page",
                        description="Query API provider status page for incidents",
                        risk_level=RiskLevel.LOW,
                        tool_name="api_curl_test",
                        tool_params_template={
                            "url": "{status_page_url}",
                            "timeout": 10,
                        },
                        success_pattern=r"(operational|all systems)",
                    ),
                ],
            )
        )

        # ── VM Platform Remediation Playbook (SPEC-003) ───────────────────────

        self.register(
            Playbook(
                id="platform_vm_remediation",
                name="Platform VM Remediation",
                description="Diagnose and remediate a degraded/unreachable Nutanix/VMware/OpenShift platform",
                steps=[
                    PlaybookStep(
                        name="List Platform VMs",
                        description="Enumerate VMs on the affected platform for triage",
                        risk_level=RiskLevel.LOW,
                        tool_name="platform_list_vms",
                        tool_params_template={
                            "platform_name": "{platform_name}",
                        },
                    ),
                    PlaybookStep(
                        name="Restart Affected VM",
                        description="Restart the VM identified as the source of the incident",
                        risk_level=RiskLevel.MEDIUM,
                        tool_name="platform_restart_vm",
                        tool_params_template={
                            "platform_name": "{platform_name}",
                            "vm_id": "{vm_id}",
                        },
                    ),
                ],
            )
        )

        # ── Azure Resource Remediation Playbook (SPEC-003) ────────────────────

        self.register(
            Playbook(
                id="azure_resource_remediation",
                name="Azure Resource Remediation",
                description="Diagnose and remediate an unhealthy or deallocated Azure resource",
                steps=[
                    PlaybookStep(
                        name="Check Resource Health",
                        description="Query Azure Resource Health for current availability status",
                        risk_level=RiskLevel.LOW,
                        tool_name="azure_resource_health",
                        tool_params_template={
                            "resource_id": "{resource_id}",
                            "subscription_id": "{subscription_id}",
                        },
                    ),
                    PlaybookStep(
                        name="Check Activity Log",
                        description="Review recent Activity Log entries for the resource group",
                        risk_level=RiskLevel.LOW,
                        tool_name="azure_activity_log",
                        tool_params_template={
                            "resource_group": "{resource_group}",
                            "subscription_id": "{subscription_id}",
                        },
                    ),
                    PlaybookStep(
                        name="Restart VM",
                        description="Restart the affected Azure VM",
                        risk_level=RiskLevel.MEDIUM,
                        tool_name="azure_restart_vm",
                        tool_params_template={
                            "resource_group": "{resource_group}",
                            "vm_name": "{resource_name}",
                            "subscription_id": "{subscription_id}",
                        },
                    ),
                ],
            )
        )

    def register(self, playbook: Playbook) -> None:
        self._playbooks[playbook.id] = playbook
        logger.debug("playbook_registered", playbook_id=playbook.id, name=playbook.name)

    def get(self, playbook_id: str) -> Playbook | None:
        return self._playbooks.get(playbook_id)

    def list_playbooks(self) -> list[dict[str, Any]]:
        return [
            {
                "id": pb.id,
                "name": pb.name,
                "description": pb.description,
                "steps": len(pb.steps),
                "requires_approval": any(
                    s.risk_level in (RiskLevel.MEDIUM, RiskLevel.HIGH) for s in pb.steps
                ),
            }
            for pb in self._playbooks.values()
        ]


# ── PlaybookExecutor ──────────────────────────────────────────────────────────


class PlaybookExecutor:
    """
    Orchestrates execution of a playbook against a live incident context.

    Execution rules by risk level:
      LOW    → call MCP tool immediately, append output, continue
      MEDIUM → request human approval via ApprovalManager; pause execution
      HIGH   → request human approval with risk warning; pause execution

    Approval/rejection is handled asynchronously by ApprovalManager.
    The executor does not block waiting for approval responses.

    Usage:
        executor = PlaybookExecutor(registry, mcp_manager, approval_manager)
        run = await executor.execute(
            playbook_id="crash_loop_remediation",
            incident_context={"resource_name": "nginx-abc", "namespace": "prod"},
            channel_type="telegram",
            channel_target="123456789",
        )
    """

    def __init__(
        self,
        registry: PlaybookRegistry,
        mcp_manager: Any | None = None,
        approval_manager: Any | None = None,
        notify_callback: Callable[[str, str], Coroutine] | None = None,
    ) -> None:
        self._registry = registry
        self._mcp = mcp_manager
        self._approval = approval_manager
        # notify_callback(channel_target, message) → sends a message to the user
        self._notify = notify_callback

    async def execute(
        self,
        playbook_id: str,
        incident_context: dict[str, Any],
        channel_type: str = "",
        channel_target: str = "",
        requested_by: str = "auto",
    ) -> PlaybookRun:
        """
        Execute a playbook, returning a PlaybookRun that reflects final state.

        LOW-risk steps are executed immediately.  MEDIUM/HIGH-risk steps pause
        execution and request human approval — the run ends at that step with
        status 'awaiting_approval'.
        """
        playbook = self._registry.get(playbook_id)
        if not playbook:
            raise ValueError(f"Playbook {playbook_id!r} not found in registry")

        run = PlaybookRun(
            playbook_id=playbook_id,
            incident_context=incident_context,
        )

        with _tracer.start_as_current_span(
            "playbook.execute",
            attributes={
                "playbook.id": playbook_id,
                "playbook.run_id": run.run_id,
                "playbook.requested_by": requested_by,
            },
        ) as span:
            run.status = "running"
            logger.info(
                "playbook_started",
                run_id=run.run_id,
                playbook=playbook_id,
                resource=incident_context.get("resource_name"),
                namespace=incident_context.get("namespace"),
            )

            for i, step in enumerate(playbook.steps):
                run.current_step = i

                if run.status == "failed":
                    break

                params = step.resolve_params(incident_context)

                if step.risk_level == RiskLevel.LOW:
                    output = await self._run_step(run, step, params)
                    run.step_outputs.append(output)
                    if run.status == "failed":
                        break
                    # Notify progress if callback available
                    if self._notify and channel_target:
                        await self._safe_notify(
                            channel_target, f"▶️ **{step.name}**: {output[:300]}"
                        )

                else:
                    # MEDIUM / HIGH — request approval and pause
                    run.status = "awaiting_approval"
                    if self._approval:
                        try:
                            await self._approval.request_approval(
                                tool_name=step.tool_name,
                                tool_params=params,
                                risk_level=step.risk_level,
                                description=step.description,
                                requested_by=requested_by,
                                channel_type=channel_type,
                                channel_target=channel_target,
                                send_message_callback=self._notify,
                                playbook_run_id=run.run_id,
                            )
                            logger.info(
                                "playbook_awaiting_approval",
                                run_id=run.run_id,
                                step=step.name,
                                risk=step.risk_level.value,
                            )
                        except Exception as e:
                            logger.error(
                                "playbook_approval_request_failed",
                                run_id=run.run_id,
                                step=step.name,
                                error=str(e),
                            )
                            run.step_outputs.append(
                                f"⚠️ Step '{step.name}' approval request failed: {e}"
                            )
                            run.status = "failed"
                            run.error = str(e)
                    else:
                        # No approval manager — skip with warning
                        run.step_outputs.append(
                            f"⚠️ Step '{step.name}' ({step.risk_level.value} risk) skipped — "
                            f"no ApprovalManager configured."
                        )
                    # Stop processing further steps until approval is granted
                    break

            if run.status == "running":
                run.status = "completed"
                logger.info(
                    "playbook_completed",
                    run_id=run.run_id,
                    playbook=playbook_id,
                    steps_executed=len(run.step_outputs),
                )

            span.set_attribute("playbook.final_status", run.status)
            span.set_attribute("playbook.steps_executed", len(run.step_outputs))

            if playbook.on_complete:
                try:
                    success = run.status == "completed"
                    await playbook.on_complete(run.run_id, success, run.error or "")
                except Exception as e:
                    logger.warning("playbook_on_complete_failed", error=str(e))

            return run

    async def _run_step(
        self,
        run: PlaybookRun,
        step: PlaybookStep,
        params: dict[str, Any],
    ) -> str:
        """Execute a single LOW-risk step via MCP and return a summary string."""
        logger.info(
            "playbook_step_running",
            run_id=run.run_id,
            step=step.name,
            tool=step.tool_name,
            params=params,
        )
        if not self._mcp:
            return f"⚠️ '{step.name}' skipped — MCP manager not available"

        settings = get_settings()
        timeout = getattr(settings, "mcp_tool_timeout_seconds", 60)
        try:
            result = await asyncio.wait_for(
                self._mcp.call_tool(step.tool_name, params),
                timeout=float(timeout),
            )
            output = str(result)[:600]
            # Validate output against success_pattern if defined
            if step.success_pattern:
                import re as _re

                if not _re.search(step.success_pattern, output):
                    logger.error(
                        "playbook_step_pattern_mismatch",
                        run_id=run.run_id,
                        step=step.name,
                        pattern=step.success_pattern,
                    )
                    run.status = "failed"
                    run.error = f"Step '{step.name}' output did not match success_pattern '{step.success_pattern}'"
                    return f"❌ {step.name}: output did not match success pattern"
            logger.info("playbook_step_completed", run_id=run.run_id, step=step.name)
            return f"✅ {step.name}: {output}"
        except TimeoutError:
            logger.error(
                "playbook_step_timeout", run_id=run.run_id, step=step.name, timeout_seconds=timeout
            )
            run.status = "failed"
            run.error = f"Step '{step.name}' timed out after {timeout}s"
            return f"❌ {step.name}: timed out after {timeout}s"
        except Exception as e:
            logger.error("playbook_step_failed", run_id=run.run_id, step=step.name, error=str(e))
            run.status = "failed"
            run.error = str(e)
            return f"❌ {step.name}: {e}"

    async def _safe_notify(self, target: str, message: str) -> None:
        if self._notify:
            try:
                await self._notify(target, message)
            except Exception as e:
                logger.warning("playbook_notify_failed", error=str(e))
