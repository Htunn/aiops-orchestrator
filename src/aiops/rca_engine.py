"""
AI-powered Root Cause Analysis engine.

Takes an incident context (pod events, logs, metrics snapshot) and
sends it to the AI model with an SRE-specialist prompt to produce
a structured RCA report with confidence score and recommended actions.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog

from src.config import get_settings

logger = structlog.get_logger()
Settings = get_settings


@dataclass
class RCAReport:
    """Structured output from the RCA engine."""

    root_cause: str
    confidence: float  # 0.0 - 1.0
    failure_pattern: str  # e.g., "OOMKill", "Config Error", "Network Timeout"
    recommended_actions: list[str]
    supporting_evidence: list[str]
    incident_context: dict[str, Any] = field(default_factory=dict)

    def to_markdown(self) -> str:
        confidence_pct = int(self.confidence * 100)
        evidence_lines = "\n".join(f"  - {e}" for e in self.supporting_evidence)
        actions_lines = "\n".join(f"  {i + 1}. {a}" for i, a in enumerate(self.recommended_actions))
        return (
            f"**🔍 Root Cause Analysis**\n\n"
            f"**Pattern:** {self.failure_pattern}\n"
            f"**Root Cause:** {self.root_cause}\n"
            f"**Confidence:** {confidence_pct}%\n\n"
            f"**Supporting Evidence:**\n{evidence_lines}\n\n"
            f"**Recommended Actions:**\n{actions_lines}"
        )


_RCA_SYSTEM_PROMPT = """\
You are an expert Site Reliability Engineer (SRE) specialized in Kubernetes, cloud infrastructure
(Azure), and virtualization platforms (Nutanix, VMware, OpenShift).
Your task is to perform root cause analysis (RCA) on the provided incident context, which may come
from any of these domains.

Respond ONLY with a JSON object with this exact structure:
{
  "root_cause": "Clear one-sentence description of the root cause",
  "confidence": 0.85,
  "failure_pattern": "One of: OOMKill | CrashLoop | ConfigError | NetworkTimeout | ImagePullError | ResourceExhaustion | DependencyFailure | NodePressure | StorageFailure | VMUnreachable | VMDeallocated | ResourceHealthDegraded | PlatformDegraded | Unknown",
  "recommended_actions": ["Action 1", "Action 2", "Action 3"],
  "supporting_evidence": ["Evidence item 1", "Evidence item 2"]
}

Analyze the incident context carefully — it will contain one of:
- Kubernetes: pod events, log content, restart count, node conditions
- Azure: resource group/subscription, resource health state, activity log entries
- VM platform (Nutanix/VMware/OpenShift): platform name/type, health status, response time
"""


class RCAEngine:
    """
    AI-powered root cause analysis.

    Usage:
        rca = RCAEngine(ai_client)
        report = await rca.analyze(incident_context)
    """

    def __init__(self, ai_client: Any = None) -> None:
        self._ai_client = ai_client

    async def analyze(self, incident_context: dict[str, Any]) -> RCAReport:
        """
        Run RCA on an incident context dict.

        Context should contain:
          - resource_name, namespace, resource_kind
          - events: list of K8s events
          - logs: log lines as string
          - restarts: int
          - metrics: dict (optional)
        """
        if not self._ai_client:
            return self._fallback_rca(incident_context)

        settings = get_settings()
        timeout = getattr(settings, "rca_timeout_seconds", 30)
        user_message = self._build_context_message(incident_context)
        try:
            import json

            response, _tokens = await asyncio.wait_for(
                self._ai_client.generate_response(
                    messages=[
                        {"role": "system", "content": _RCA_SYSTEM_PROMPT},
                        {"role": "user", "content": user_message},
                    ],
                    model="gpt-4o",
                    max_tokens=800,
                ),
                timeout=float(timeout),
            )
            # Extract JSON from response (handle code-fenced output)
            content = response.strip()
            if "```" in content:
                parts = content.split("```")
                content = parts[1] if len(parts) > 1 else content
                if content.startswith("json"):
                    content = content[4:]
            try:
                data = json.loads(content)
            except (json.JSONDecodeError, ValueError) as parse_err:
                logger.warning("rca_json_parse_failed", error=str(parse_err))
                return self._fallback_rca(incident_context)

            raw_confidence = float(data.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, raw_confidence))  # clamp to [0.0, 1.0]
            return RCAReport(
                root_cause=data.get("root_cause") or "Unknown",
                confidence=confidence,
                failure_pattern=data.get("failure_pattern") or "Unknown",
                recommended_actions=data.get("recommended_actions") or [],
                supporting_evidence=data.get("supporting_evidence") or [],
                incident_context=incident_context,
            )
        except TimeoutError:
            logger.warning("rca_ai_timeout", timeout_seconds=timeout)
            return self._fallback_rca(incident_context)
        except Exception as e:
            logger.warning("rca_ai_analysis_failed", error=str(e))
            return self._fallback_rca(incident_context)

    @staticmethod
    def _resource_label(ctx: dict[str, Any]) -> str:
        """Human-readable resource descriptor across all four domains (SPEC-006)."""
        if ctx.get("platform_name"):
            return f"{ctx['platform_name']} ({ctx.get('platform_type', 'platform')})"
        if ctx.get("resource_group"):
            kind = ctx.get("resource_type", "resource")
            return f"{kind}/{ctx.get('resource_name', 'unknown')} in {ctx['resource_group']}"
        ns = f" (ns: {ctx['namespace']})" if ctx.get("namespace") else ""
        return f"{ctx.get('resource_kind', 'Pod')}/{ctx.get('resource_name', 'unknown')}{ns}"

    @classmethod
    def _build_context_message(cls, ctx: dict[str, Any]) -> str:
        """
        Build the AI prompt's context section. Branches on whichever domain
        scoping field is present (`platform_name` / `resource_group` / `namespace`)
        per the shared event contract established in SPEC-003.
        """
        lines = [
            "## Incident Context",
            f"Event Type: {ctx.get('event_type', 'unknown')}",
            f"Severity: {ctx.get('severity', 'unknown')}",
        ]

        if ctx.get("platform_name"):
            lines += [
                f"Platform: {ctx['platform_name']} ({ctx.get('platform_type', 'unknown')})",
                f"Status: {ctx.get('status', 'unknown')}",
            ]
        elif ctx.get("resource_group"):
            lines += [
                f"Azure Resource: {ctx.get('resource_type', 'resource')}/{ctx.get('resource_name', 'unknown')}",
                f"Resource Group: {ctx['resource_group']}",
            ]
            if ctx.get("subscription_id"):
                lines.append(f"Subscription: {ctx['subscription_id']}")
            if ctx.get("availability_state"):
                lines.append(f"Availability State: {ctx['availability_state']}")
        else:
            lines += [
                f"Resource: {ctx.get('resource_kind', 'Pod')}/{ctx.get('resource_name', 'unknown')}",
                f"Namespace: {ctx.get('namespace', 'default')}",
                f"Restart Count: {ctx.get('restarts', 0)}",
            ]

        lines += ["", "## Summary", ctx.get("message") or "(no message provided)"]

        if ctx.get("events"):
            lines += ["", "## Recent Events"]
            for ev in (ctx.get("events") or [])[:10]:
                lines.append(
                    f"- [{ev.get('type', '')}] {ev.get('reason', '')}: {ev.get('message', '')}"
                )

        if ctx.get("activity_log"):
            lines += ["", "## Activity Log"]
            for entry in (ctx.get("activity_log") or [])[:10]:
                lines.append(f"- {entry}")

        logs = ctx.get("logs", "")
        if logs:
            lines += ["", "## Recent Logs"]
            lines.extend(logs.strip().split("\n")[-50:])

        if ctx.get("metrics"):
            lines += ["", "## Metrics"]
            for k, v in ctx["metrics"].items():
                lines.append(f"- {k}: {v}")

        return "\n".join(lines)

    @staticmethod
    def _fallback_rca(ctx: dict[str, Any]) -> RCAReport:
        """Simple heuristic-based fallback when AI is unavailable."""
        logs = ctx.get("logs", "").lower()
        restarts = ctx.get("restarts", 0)

        if "oomkill" in logs or "out of memory" in logs:
            return RCAReport(
                root_cause="Container exceeded memory limits and was killed by the OOM reaper",
                confidence=0.85,
                failure_pattern="OOMKill",
                recommended_actions=[
                    "Increase memory limits",
                    "Profile application memory usage",
                    "Add memory limit alerts",
                ],
                supporting_evidence=["OOM kill detected in logs"],
                incident_context=ctx,
            )
        if restarts > 5:
            return RCAReport(
                root_cause="Application is crashing repeatedly due to an unhandled error on startup or runtime",
                confidence=0.70,
                failure_pattern="CrashLoop",
                recommended_actions=[
                    "Check application logs for exceptions",
                    "Verify configuration/secrets",
                    "Check liveness probe settings",
                ],
                supporting_evidence=[f"Pod has {restarts} restarts"],
                incident_context=ctx,
            )
        resource_label = RCAEngine._resource_label(ctx)
        return RCAReport(
            root_cause=f"Unknown — insufficient data for automated analysis of {resource_label}",
            confidence=0.30,
            failure_pattern="Unknown",
            recommended_actions=[
                f"Inspect {resource_label}'s recent events/logs manually",
                "Review recent changes or deployments",
                "Check platform/cluster-level status dashboards",
            ],
            supporting_evidence=[],
            incident_context=ctx,
        )
