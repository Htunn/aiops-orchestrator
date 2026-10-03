"""Unit tests for capability matcher."""

import pytest

from src.models.agent import AgentCapability
from src.services.capability_matcher import (
    filter_capable_agents,
    match_capability,
    rank_agents_by_capability,
)


class TestCapabilityMatcher:
    """Test capability matching and scoring."""

    @pytest.fixture
    def sample_capabilities(self):
        """Sample agent capabilities for testing."""
        return [
            AgentCapability(
                name="kubernetes.scale",
                description="Scale Kubernetes deployments",
                parameters={
                    "type": "object",
                    "required": ["namespace", "deployment", "replicas"],
                    "properties": {
                        "namespace": {"type": "string"},
                        "deployment": {"type": "string"},
                        "replicas": {"type": "integer"},
                    },
                },
                tags=["kubernetes", "scaling"],
            ),
            AgentCapability(
                name="kubernetes.restart",
                description="Restart Kubernetes pods",
                parameters={
                    "type": "object",
                    "required": ["namespace", "deployment"],
                    "properties": {
                        "namespace": {"type": "string"},
                        "deployment": {"type": "string"},
                    },
                },
                tags=["kubernetes", "pods"],
            ),
            AgentCapability(
                name="database.query",
                description="Query database",
                parameters={
                    "type": "object",
                    "required": ["database", "query"],
                    "properties": {
                        "database": {"type": "string"},
                        "query": {"type": "string"},
                    },
                },
                tags=["database", "sql"],
            ),
        ]

    def test_exact_match_valid_params(self, sample_capabilities):
        """Test exact capability match with valid parameters."""
        score = match_capability(
            agent_capabilities=sample_capabilities,
            required_capability="kubernetes.scale",
            parameters={
                "namespace": "production",
                "deployment": "api-server",
                "replicas": 5,
            },
        )

        assert score == 1.0  # Perfect match

    def test_exact_match_invalid_params(self, sample_capabilities):
        """Test exact capability match with invalid parameters."""
        score = match_capability(
            agent_capabilities=sample_capabilities,
            required_capability="kubernetes.scale",
            parameters={
                "namespace": "production",
                # Missing required "deployment" and "replicas"
            },
        )

        assert score == 0.8  # Match but invalid params

    def test_exact_match_no_schema(self):
        """Test exact capability match without parameter schema."""
        capabilities = [
            AgentCapability(
                name="test.capability",
                description="Test capability without schema",
            )
        ]

        score = match_capability(
            agent_capabilities=capabilities,
            required_capability="test.capability",
            parameters={},
        )

        assert score == 0.8  # Exact match, no schema

    def test_partial_match(self, sample_capabilities):
        """Test partial capability name match."""
        score = match_capability(
            agent_capabilities=sample_capabilities,
            required_capability="kubernetes.scale.deployment",
            parameters={},
        )

        assert score == 0.6  # Partial match

    def test_tag_match(self):
        """Test capability match by tags."""
        capabilities = [
            AgentCapability(
                name="k8s-operations",
                description="Kubernetes operations",
                tags=["kubernetes", "cluster", "scaling"],
            )
        ]

        score = match_capability(
            agent_capabilities=capabilities,
            required_capability="scaling-service",
            parameters={},
        )

        assert score == 0.4  # Tag match

    def test_no_match(self, sample_capabilities):
        """Test no capability match."""
        score = match_capability(
            agent_capabilities=sample_capabilities,
            required_capability="notification.send",
            parameters={},
        )

        assert score == 0.0  # No match

    def test_type_validation_correct(self, sample_capabilities):
        """Test parameter type validation with correct types."""
        score = match_capability(
            agent_capabilities=sample_capabilities,
            required_capability="kubernetes.scale",
            parameters={
                "namespace": "production",
                "deployment": "api-server",
                "replicas": 5,  # Correct type: integer
            },
        )

        assert score == 1.0

    def test_type_validation_incorrect(self, sample_capabilities):
        """Test parameter type validation with incorrect types."""
        score = match_capability(
            agent_capabilities=sample_capabilities,
            required_capability="kubernetes.scale",
            parameters={
                "namespace": "production",
                "deployment": "api-server",
                "replicas": "five",  # Wrong type: should be integer
            },
        )

        assert score == 0.8  # Match but params invalid

    def test_rank_agents_by_capability(self):
        """Test ranking multiple agents by capability match."""
        agents = [
            {
                "agent_id": "agent-1",
                "capabilities": [
                    {
                        "name": "kubernetes.scale",
                        "description": "Scale deployments",
                        "parameters": {
                            "type": "object",
                            "required": ["namespace", "deployment", "replicas"],
                            "properties": {
                                "namespace": {"type": "string"},
                                "deployment": {"type": "string"},
                                "replicas": {"type": "integer"},
                            },
                        },
                    }
                ],
            },
            {
                "agent_id": "agent-2",
                "capabilities": [
                    {"name": "kubernetes.restart", "description": "Restart pods"}
                ],
            },
            {
                "agent_id": "agent-3",
                "capabilities": [
                    {"name": "database.query", "description": "Query database"}
                ],
            },
        ]

        ranked = rank_agents_by_capability(
            agents=agents,
            required_capability="kubernetes.scale",
            parameters={
                "namespace": "production",
                "deployment": "api-server",
                "replicas": 5,
            },
        )

        # Should be sorted by score descending
        assert len(ranked) == 3
        assert ranked[0][0] == 1.0  # agent-1 has perfect match
        assert ranked[0][1]["agent_id"] == "agent-1"
        assert ranked[1][0] == 0.0  # agent-2 has no match
        assert ranked[2][0] == 0.0  # agent-3 has no match

    def test_filter_capable_agents(self):
        """Test filtering agents by minimum capability score."""
        agents = [
            {
                "agent_id": "agent-1",
                "capabilities": [
                    {"name": "kubernetes.scale", "description": "Scale deployments"}
                ],
            },
            {
                "agent_id": "agent-2",
                "capabilities": [
                    {"name": "database.query", "description": "Query database"}
                ],
            },
        ]

        filtered = filter_capable_agents(
            agents=agents,
            required_capability="kubernetes.scale",
            min_score=0.5,
        )

        assert len(filtered) == 1
        assert filtered[0]["agent_id"] == "agent-1"

    def test_empty_capabilities(self):
        """Test matching when agent has no capabilities."""
        score = match_capability(
            agent_capabilities=[],
            required_capability="any.capability",
            parameters={},
        )

        assert score == 0.0
