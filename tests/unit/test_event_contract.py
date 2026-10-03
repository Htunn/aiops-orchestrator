"""
Shared event contract tests (SPEC-003).

Asserts all four watch-loop event dataclasses (`ClusterEvent`, `ApiBackendEvent`,
`PlatformEvent`, `AzureEvent`) continue to be independent `@dataclass`es but
guarantee the shared `to_dict()` keys documented in SPEC-003: `event_type`,
`severity`, `message`, `detected_at`, plus exactly one domain-scoping field
(`namespace` for K8s, `platform_name` for VM platforms, `resource_group` for Azure;
API backend events reuse `namespace` as a compatibility field — see
``ApiBackendEvent.to_dict()``).
"""

from datetime import datetime

from src.monitoring.api_watchloop import ApiBackendEvent
from src.monitoring.azure_watchloop import AzureEvent
from src.monitoring.platform_watchloop import PlatformEvent
from src.monitoring.watchloop import ClusterEvent

_SHARED_KEYS = ("event_type", "severity", "message", "detected_at")

# Domain-scoping discriminator fields: namespace (K8s), endpoint_name (API
# backends), platform_name (VM platforms), resource_group (Azure).
_DOMAIN_SCOPE_FIELDS = ("namespace", "endpoint_name", "platform_name", "resource_group")


def _cluster_event() -> ClusterEvent:
    return ClusterEvent(
        event_type="crash_loop",
        severity="critical",
        namespace="default",
        resource_kind="Pod",
        resource_name="web-pod",
        message="CrashLoopBackOff",
    )


def _api_backend_event() -> ApiBackendEvent:
    return ApiBackendEvent(
        event_type="api_backend_down",
        severity="critical",
        endpoint_name="payments-api",
        url="https://payments.example.com",
        message="Endpoint unreachable",
    )


def _platform_event() -> PlatformEvent:
    return PlatformEvent(
        platform_name="nutanix-prod",
        platform_type="nutanix",
        event_type="unreachable",
        severity="critical",
        status="unreachable",
        message="Platform nutanix-prod is unreachable",
    )


def _azure_event() -> AzureEvent:
    return AzureEvent(
        event_type="azure_resource_unhealthy",
        severity="critical",
        resource_group="prod-rg",
        resource_name="web-01",
        resource_type="vm",
        message="Azure vm web-01 is Unavailable",
    )


class TestSharedEventContract:
    def test_all_four_events_expose_shared_keys(self):
        for event in (_cluster_event(), _api_backend_event(), _platform_event(), _azure_event()):
            d = event.to_dict()
            for key in _SHARED_KEYS:
                assert key in d, f"{type(event).__name__}.to_dict() missing shared key {key!r}"

    def test_all_four_events_expose_exactly_one_domain_scope_field(self):
        for event in (_cluster_event(), _api_backend_event(), _platform_event(), _azure_event()):
            d = event.to_dict()
            present = [f for f in _DOMAIN_SCOPE_FIELDS if d.get(f)]
            assert len(present) == 1, (
                f"{type(event).__name__}.to_dict() should expose exactly one domain-scoping "
                f"field from {_DOMAIN_SCOPE_FIELDS}, found {present}"
            )

    def test_detected_at_is_iso_parseable_for_all_events(self):
        for event in (_cluster_event(), _api_backend_event(), _platform_event(), _azure_event()):
            d = event.to_dict()
            assert isinstance(d["detected_at"], str)
            datetime.fromisoformat(d["detected_at"])

    def test_severity_is_a_known_value_for_all_events(self):
        for event in (_cluster_event(), _api_backend_event(), _platform_event(), _azure_event()):
            d = event.to_dict()
            assert d["severity"] in ("critical", "warning", "info")
