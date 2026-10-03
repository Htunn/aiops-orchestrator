"""Capability Matcher - Match tasks to agents based on capabilities."""

from typing import Any

import structlog

from src.models.agent import AgentCapability

logger = structlog.get_logger()


def match_capability(
    agent_capabilities: list[AgentCapability],
    required_capability: str,
    parameters: dict[str, Any],
) -> float:
    """
    Calculate a score for how well an agent's capabilities match a task.

    Args:
        agent_capabilities: List of capabilities the agent provides
        required_capability: The capability name being requested
        parameters: Task parameters to validate against capability schema

    Returns:
        Match score (0.0 to 1.0), where higher is better

    Scoring:
    - 1.0: Exact capability match with valid parameters
    - 0.8: Exact capability match (parameters not validated)
    - 0.6: Partial capability name match
    - 0.0: No match
    """
    best_score = 0.0

    for capability in agent_capabilities:
        # Exact name match
        if capability.name == required_capability:
            # Validate parameters if schema provided
            if capability.parameters:
                if _validate_parameters(parameters, capability.parameters):
                    return 1.0  # Perfect match with valid params
                else:
                    best_score = max(best_score, 0.8)  # Match but params invalid
            else:
                best_score = max(best_score, 0.8)  # Exact match, no schema

        # Partial name match (e.g., "kubernetes.scale" matches "kubernetes")
        elif required_capability.startswith(f"{capability.name}."):
            best_score = max(best_score, 0.6)

        # Check tags for semantic match
        elif capability.tags and any(
            tag.lower() in required_capability.lower() for tag in capability.tags
        ):
            best_score = max(best_score, 0.4)

    return best_score


def _validate_parameters(
    parameters: dict[str, Any],
    schema: dict[str, Any],
) -> bool:
    """
    Validate parameters against a JSON Schema.

    Args:
        parameters: Task parameters
        schema: JSON Schema to validate against

    Returns:
        True if parameters are valid, False otherwise

    Note: This is a simplified validation. For production, use jsonschema library.
    """
    if not schema:
        return True

    # Get required fields from schema
    required_fields = schema.get("required", [])

    # Check all required fields are present
    for field in required_fields:
        if field not in parameters:
            logger.debug(
                "capability_match_missing_field",
                field=field,
                required=required_fields,
            )
            return False

    # Basic type checking for properties
    properties = schema.get("properties", {})
    for field, value in parameters.items():
        if field in properties:
            field_schema = properties[field]
            expected_type = field_schema.get("type")

            if expected_type:
                if not _check_type(value, expected_type):
                    logger.debug(
                        "capability_match_type_mismatch",
                        field=field,
                        expected_type=expected_type,
                        actual_type=type(value).__name__,
                    )
                    return False

    return True


def _check_type(value: Any, expected_type: str) -> bool:
    """
    Check if a value matches the expected JSON Schema type.

    Args:
        value: Value to check
        expected_type: JSON Schema type (string, number, integer, boolean, object, array)

    Returns:
        True if value matches type, False otherwise
    """
    type_map: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "object": dict,
        "array": list,
    }

    expected_python_type = type_map.get(expected_type)
    if expected_python_type is None:
        return True  # Unknown type, assume valid

    return isinstance(value, expected_python_type)


def rank_agents_by_capability(
    agents: list[dict[str, Any]],
    required_capability: str,
    parameters: dict[str, Any],
) -> list[tuple[float, dict[str, Any]]]:
    """
    Rank agents by how well they match a required capability.

    Args:
        agents: List of agent dictionaries with 'capabilities' field
        required_capability: The capability name being requested
        parameters: Task parameters

    Returns:
        List of (score, agent) tuples, sorted by score descending
    """
    from src.models.agent import AgentCapability

    scored_agents = []

    for agent in agents:
        # Convert capabilities to AgentCapability objects if needed
        capabilities = []
        for cap in agent.get("capabilities", []):
            if isinstance(cap, dict):
                capabilities.append(AgentCapability(**cap))
            elif isinstance(cap, AgentCapability):
                capabilities.append(cap)

        score = match_capability(capabilities, required_capability, parameters)
        scored_agents.append((score, agent))

    # Sort by score descending
    scored_agents.sort(key=lambda x: x[0], reverse=True)
    return scored_agents


def filter_capable_agents(
    agents: list[dict[str, Any]],
    required_capability: str,
    min_score: float = 0.5,
) -> list[dict[str, Any]]:
    """
    Filter agents to only those with sufficient capability match.

    Args:
        agents: List of agent dictionaries
        required_capability: The capability name being requested
        min_score: Minimum match score (0.0 to 1.0)

    Returns:
        List of agents with score >= min_score
    """
    ranked = rank_agents_by_capability(agents, required_capability, {})
    return [agent for score, agent in ranked if score >= min_score]
