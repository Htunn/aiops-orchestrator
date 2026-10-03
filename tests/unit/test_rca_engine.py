"""Unit tests for the generalized RCAEngine (SPEC-006).

Covers domain-agnostic context building (K8s / Azure / VM-platform shapes)
and the fixed AI-client call contract (generate_response, not the
nonexistent .complete()).
"""

import pytest

from src.aiops.rca_engine import RCAEngine, RCAReport


class _FakeAIClient:
    """Mimics BaseAIClient.generate_response()."""

    def __init__(self, response_json: str | None = None, raise_exc: Exception | None = None):
        self._response_json = response_json
        self._raise_exc = raise_exc
        self.last_messages = None
        self.last_model = None

    async def generate_response(self, messages, model, **kwargs):
        self.last_messages = messages
        self.last_model = model
        if self._raise_exc:
            raise self._raise_exc
        return self._response_json, 42


K8S_CONTEXT = {
    "event_type": "crash_loop",
    "severity": "critical",
    "namespace": "payments",
    "resource_kind": "Pod",
    "resource_name": "web-7f9c",
    "message": "Pod payments/web-7f9c is crash looping",
    "restarts": 7,
}

AZURE_CONTEXT = {
    "event_type": "azure_resource_unhealthy",
    "severity": "critical",
    "resource_group": "prod-rg",
    "resource_name": "web-01",
    "resource_type": "vm",
    "subscription_id": "sub-123",
    "availability_state": "Unavailable",
    "message": "Azure VM web-01 is unavailable",
}

PLATFORM_CONTEXT = {
    "event_type": "unreachable",
    "severity": "critical",
    "platform_name": "nutanix-prod",
    "platform_type": "nutanix",
    "status": "unreachable",
    "message": "Platform nutanix-prod is unreachable",
}


class TestFallbackAcrossDomains:
    """No AI client configured — exercises the heuristic fallback path."""

    @pytest.mark.asyncio
    async def test_k8s_fallback_oomkill(self):
        engine = RCAEngine(ai_client=None)
        ctx = {**K8S_CONTEXT, "logs": "out of memory: Kill process 123"}
        result = await engine.analyze(ctx)
        assert result.failure_pattern == "OOMKill"
        assert result.root_cause

    @pytest.mark.asyncio
    async def test_k8s_fallback_crashloop(self):
        engine = RCAEngine(ai_client=None)
        result = await engine.analyze(K8S_CONTEXT)
        assert result.failure_pattern == "CrashLoop"

    @pytest.mark.asyncio
    async def test_azure_fallback_references_resource(self):
        engine = RCAEngine(ai_client=None)
        result = await engine.analyze(AZURE_CONTEXT)
        assert result.root_cause
        assert "web-01" in result.root_cause or "prod-rg" in result.root_cause
        assert 0.0 <= result.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_platform_fallback_references_resource(self):
        engine = RCAEngine(ai_client=None)
        result = await engine.analyze(PLATFORM_CONTEXT)
        assert result.root_cause
        assert "nutanix-prod" in result.root_cause
        assert isinstance(result.recommended_actions, list)


class TestContextBuilderNoExceptions:
    """_build_context_message must never KeyError/AttributeError on any domain shape."""

    def test_k8s_shape(self):
        msg = RCAEngine._build_context_message(K8S_CONTEXT)
        assert "payments" in msg
        assert "Pod payments/web-7f9c is crash looping" in msg

    def test_azure_shape(self):
        msg = RCAEngine._build_context_message(AZURE_CONTEXT)
        assert "prod-rg" in msg
        assert "sub-123" in msg
        assert "Unavailable" in msg

    def test_platform_shape(self):
        msg = RCAEngine._build_context_message(PLATFORM_CONTEXT)
        assert "nutanix-prod" in msg
        assert "unreachable" in msg

    def test_empty_context_does_not_raise(self):
        msg = RCAEngine._build_context_message({})
        assert isinstance(msg, str)


class TestAIClientCallContract:
    @pytest.mark.asyncio
    async def test_calls_generate_response_not_complete(self):
        ai_client = _FakeAIClient(
            response_json='{"root_cause": "disk pressure", "confidence": 0.9, '
            '"failure_pattern": "ResourceExhaustion", "recommended_actions": ["a"], '
            '"supporting_evidence": ["b"]}'
        )
        engine = RCAEngine(ai_client=ai_client)
        result = await engine.analyze(K8S_CONTEXT)
        assert ai_client.last_model == "gpt-4o"
        assert ai_client.last_messages[0]["role"] == "system"
        assert ai_client.last_messages[1]["role"] == "user"
        assert result.root_cause == "disk pressure"
        assert result.failure_pattern == "ResourceExhaustion"

    @pytest.mark.asyncio
    async def test_ai_failure_falls_back_gracefully(self):
        ai_client = _FakeAIClient(raise_exc=RuntimeError("boom"))
        engine = RCAEngine(ai_client=ai_client)
        result = await engine.analyze(K8S_CONTEXT)
        assert isinstance(result, RCAReport)
        assert result.root_cause
