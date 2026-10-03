"""A2A Authentication - JWT and API key management for agent-to-agent communication."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import jwt
import structlog

from src.config import get_settings
from src.exceptions import A2AAuthenticationError

logger = structlog.get_logger()
settings = get_settings()


class A2AAuth:
    """
    Authentication service for Agent-to-Agent communication.

    Handles JWT token generation/validation and API key management.
    """

    def __init__(self, jwt_secret: str | None = None, token_expiry_hours: int = 1):
        """
        Initialize A2A authentication.

        Args:
            jwt_secret: Secret key for JWT signing (defaults to settings)
            token_expiry_hours: Token expiration time in hours
        """
        self.jwt_secret = jwt_secret or settings.a2a_jwt_secret
        self.token_expiry_hours = token_expiry_hours
        self.algorithm = "HS256"

    def generate_api_key(self) -> str:
        """
        Generate a new API key for an agent.

        Returns:
            32-character hexadecimal API key
        """
        return secrets.token_hex(32)

    def hash_api_key(self, api_key: str) -> str:
        """
        Hash an API key for secure storage.

        Args:
            api_key: Plaintext API key

        Returns:
            SHA-256 hash of the API key
        """
        return hashlib.sha256(api_key.encode()).hexdigest()

    def verify_api_key(self, api_key: str, api_key_hash: str) -> bool:
        """
        Verify an API key against its stored hash.

        Args:
            api_key: Plaintext API key to verify
            api_key_hash: Stored hash to compare against

        Returns:
            True if API key matches hash, False otherwise
        """
        computed_hash = self.hash_api_key(api_key)
        return secrets.compare_digest(computed_hash, api_key_hash)

    def create_jwt_token(
        self,
        agent_id: str,
        capabilities: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """
        Create a JWT token for an agent.

        Args:
            agent_id: Unique agent identifier
            capabilities: List of capabilities the agent can invoke
            metadata: Additional metadata to include in token

        Returns:
            JWT token string
        """
        now = datetime.now(UTC)
        expiry = now + timedelta(hours=self.token_expiry_hours)

        payload = {
            "iat": now,
            "exp": expiry,
            "sub": agent_id,  # Subject: agent identifier
            "capabilities": capabilities or [],
            "metadata": metadata or {},
        }

        token = jwt.encode(payload, self.jwt_secret, algorithm=self.algorithm)
        return token

    def verify_jwt_token(self, token: str) -> dict[str, Any]:
        """
        Verify and decode a JWT token.

        Args:
            token: JWT token string

        Returns:
            Decoded token payload

        Raises:
            A2AAuthenticationError: If token is invalid or expired
        """
        try:
            payload = jwt.decode(
                token,
                self.jwt_secret,
                algorithms=[self.algorithm],
                options={"verify_exp": True},
            )
            return cast(dict[str, Any], payload)
        except jwt.ExpiredSignatureError as e:
            raise A2AAuthenticationError("Token has expired") from e
        except jwt.InvalidTokenError as e:
            raise A2AAuthenticationError(f"Invalid token: {e}") from e

    def extract_agent_id(self, token: str) -> str:
        """
        Extract agent ID from JWT token without full verification.

        Args:
            token: JWT token string

        Returns:
            Agent ID from token subject claim

        Raises:
            A2AAuthenticationError: If token is malformed
        """
        try:
            # Decode without verification to extract agent ID
            payload = jwt.decode(
                token,
                options={"verify_signature": False, "verify_exp": False},
            )
            return str(payload.get("sub", ""))
        except Exception as e:
            raise A2AAuthenticationError(f"Failed to extract agent ID: {e}") from e

    def validate_request(
        self,
        authorization_header: str | None,
        required_capabilities: list[str] | None = None,
    ) -> str:
        """
        Validate an incoming A2A request.

        Args:
            authorization_header: Authorization header value (Bearer token)
            required_capabilities: Capabilities required to access this endpoint

        Returns:
            Agent ID from validated token

        Raises:
            A2AAuthenticationError: If authentication fails
        """
        if not authorization_header:
            raise A2AAuthenticationError("Missing Authorization header")

        if not authorization_header.startswith("Bearer "):
            raise A2AAuthenticationError("Invalid Authorization header format")

        token = authorization_header[7:]  # Remove "Bearer " prefix

        # Verify token
        payload = self.verify_jwt_token(token)
        agent_id = payload.get("sub")

        if not agent_id:
            raise A2AAuthenticationError("Token missing agent ID")

        # Check required capabilities
        if required_capabilities:
            token_capabilities = set(payload.get("capabilities", []))
            required = set(required_capabilities)

            if not required.issubset(token_capabilities):
                missing = required - token_capabilities
                raise A2AAuthenticationError(
                    f"Token missing required capabilities: {', '.join(missing)}"
                )

        logger.debug("a2a_auth_success", agent_id=agent_id)
        return str(agent_id)


# Global auth instance
_auth: A2AAuth | None = None


def get_a2a_auth() -> A2AAuth:
    """Get the global A2A authentication instance."""
    global _auth
    if _auth is None:
        _auth = A2AAuth()
    return _auth
