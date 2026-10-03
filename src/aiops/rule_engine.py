"""
AIOps Rule Engine - evaluates cluster events against rules and triggers playbooks.

Rules are simple condition + action pairs stored in memory (and eventually DB).
Each incoming ClusterEvent or Alertmanager alert is evaluated against all enabled rules.
When a rule matches, the associated playbook is queued for execution (with approval if needed).
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger()


class RuleCondition(StrEnum):
    CRASH_LOOP = "crash_loop"
    OOM_KILLED = "oom_killed"
    NOT_READY_NODE = "not_ready_node"
    REPLICATION_FAILURE = "replication_failure"
    HIGH_RESTART_COUNT = "high_restart_count"
    ALERTMANAGER_FIRING = "alertmanager_firing"
    PROMETHEUS_THRESHOLD = "prometheus_threshold"
    # API Backend monitoring conditions
    API_BACKEND_DOWN = "api_backend_down"
    API_HIGH_LATENCY = "api_high_latency"
    API_HIGH_ERROR_RATE = "api_high_error_rate"
    API_SSL_EXPIRING = "api_ssl_expiring"
    # VM platform monitoring conditions (SPEC-003) — values match the
    # event_type strings PlatformWatchLoop already emits (src/monitoring/platform_watchloop.py)
    PLATFORM_UNREACHABLE = "unreachable"
    PLATFORM_DEGRADED = "health_degraded"
    PLATFORM_RECOVERED = "health_recovered"
    # Azure resource monitoring conditions (SPEC-003)
    AZURE_RESOURCE_UNHEALTHY = "azure_resource_unhealthy"
    AZURE_VM_DEALLOCATED = "azure_vm_deallocated"


@dataclass
class Rule:
    """A rule mapping a condition to a playbook."""

    id: str
    name: str
    condition: RuleCondition
    playbook_id: str
    endpoint_filter: str | None = None  # regex pattern for API backend endpoint names
    enabled: bool = True
    # Optional filters: only trigger when labels/namespace match
    namespace_filter: str | None = None  # regex pattern
    severity_filter: str | None = None  # critical | warning | info
    platform_filter: str | None = None  # regex pattern for platform_name (VM platforms) or resource_group (Azure)
    # Extra condition params (e.g., restart threshold)
    params: dict[str, Any] = field(default_factory=dict)

    def matches(self, event: dict[str, Any]) -> bool:
        """Test whether an incoming event matches this rule."""
        if not self.enabled:
            return False
        if event.get("event_type") != self.condition.value:
            return False
        if self.namespace_filter and event.get("namespace"):
            import re

            if not re.search(self.namespace_filter, event["namespace"]):
                return False

        if self.severity_filter and event.get("severity") != self.severity_filter:
            return False

        if self.endpoint_filter and event.get("endpoint_name"):
            import re

            if not re.search(self.endpoint_filter, event["endpoint_name"]):
                return False

        if self.platform_filter:
            import re

            scope_value = event.get("platform_name") or event.get("resource_group")
            if scope_value and not re.search(self.platform_filter, scope_value):
                return False

        return True


class RuleEngine:
    """
    Evaluates incoming events/alerts against registered rules.

    Usage:
        engine = RuleEngine()
        engine.add_rule(Rule(...))
        matched = engine.evaluate(event.to_dict())
        # matched is a list of (rule, playbook_id) pairs
    """

    # Built-in default rules reflecting common SRE best practices
    DEFAULT_RULES: list[Rule] = [
        Rule(
            id="rule-001",
            name="CrashLoop Auto-Restart",
            condition=RuleCondition.CRASH_LOOP,
            playbook_id="crash_loop_remediation",
            severity_filter="critical",
        ),
        Rule(
            id="rule-002",
            name="OOMKill Memory Increase",
            condition=RuleCondition.OOM_KILLED,
            playbook_id="oom_kill_remediation",
            severity_filter="critical",
        ),
        Rule(
            id="rule-003",
            name="NotReady Node Evacuation",
            condition=RuleCondition.NOT_READY_NODE,
            playbook_id="node_not_ready_remediation",
            severity_filter="critical",
        ),
        Rule(
            id="rule-004",
            name="Replication Failure Rollback",
            condition=RuleCondition.REPLICATION_FAILURE,
            playbook_id="deployment_rollback",
            severity_filter="critical",
        ),
        # API Backend monitoring rules
        Rule(
            id="rule-005",
            name="API Backend Down Investigation",
            condition=RuleCondition.API_BACKEND_DOWN,
            playbook_id="api_backend_down_remediation",
            severity_filter="critical",
        ),
        Rule(
            id="rule-006",
            name="API High Latency Investigation",
            condition=RuleCondition.API_HIGH_LATENCY,
            playbook_id="api_high_latency_investigation",
            severity_filter="warning",
        ),
        Rule(
            id="rule-007",
            name="API High Error Rate Investigation",
            condition=RuleCondition.API_HIGH_ERROR_RATE,
            playbook_id="api_error_rate_investigation",
            severity_filter="critical",
        ),
        # VM platform monitoring rules (SPEC-003)
        Rule(
            id="rule-008",
            name="Platform VM Unreachable Remediation",
            condition=RuleCondition.PLATFORM_UNREACHABLE,
            playbook_id="platform_vm_remediation",
            severity_filter="critical",
        ),
        Rule(
            id="rule-009",
            name="Platform VM Degraded Remediation",
            condition=RuleCondition.PLATFORM_DEGRADED,
            playbook_id="platform_vm_remediation",
            severity_filter="warning",
        ),
        # Azure resource monitoring rules (SPEC-003)
        Rule(
            id="rule-010",
            name="Azure Resource Unhealthy Remediation",
            condition=RuleCondition.AZURE_RESOURCE_UNHEALTHY,
            playbook_id="azure_resource_remediation",
            severity_filter="critical",
        ),
        Rule(
            id="rule-011",
            name="Azure VM Deallocated Remediation",
            condition=RuleCondition.AZURE_VM_DEALLOCATED,
            playbook_id="azure_resource_remediation",
            severity_filter="critical",
        ),
    ]

    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}
        # Load defaults
        for rule in self.DEFAULT_RULES:
            self._rules[rule.id] = rule

    def add_rule(self, rule: Rule) -> None:
        """Register a new rule."""
        self._rules[rule.id] = rule
        logger.info("rule_registered", rule_id=rule.id, name=rule.name, playbook=rule.playbook_id)

    def remove_rule(self, rule_id: str) -> bool:
        """Remove a rule by ID."""
        if rule_id in self._rules:
            del self._rules[rule_id]
            return True
        return False

    def list_rules(self) -> list[Rule]:
        return list(self._rules.values())

    def evaluate(self, event: dict[str, Any]) -> list[tuple[Rule, str]]:
        """
        Evaluate an event against all enabled rules.
        Returns list of (Rule, playbook_id) for every matching rule.
        """
        matches = []
        for rule in self._rules.values():
            if rule.matches(event):
                logger.info(
                    "rule_matched",
                    rule_id=rule.id,
                    name=rule.name,
                    event_type=event.get("event_type"),
                    resource=event.get("resource_name"),
                )
                matches.append((rule, rule.playbook_id))
        return matches
