"""Abstract base class for all AI/LLM backend clients."""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any


class BaseAIClient(ABC):
    """Common interface every LLM backend must implement.

    Both GitHubModelsClient and GeminiClient conform to this interface so that
    the rest of the application can call generate_response() / stream_response()
    without knowing which backend is active.
    """

    @abstractmethod
    async def generate_response(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> tuple[str, int]:
        """Generate a full response.

        Args:
            messages: OpenAI-format message list
                      [{"role": "system|user|assistant", "content": "..."}]
            model: Model alias understood by this backend
            temperature: Sampling temperature (0.0 – 1.0)
            max_tokens: Maximum tokens to generate

        Returns:
            (response_text, total_token_count)
        """

    @abstractmethod
    async def stream_response(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Stream response chunks.

        Yields:
            Successive text fragments of the response.
        """
        raise NotImplementedError
        yield  # pragma: no cover — unreachable; marks this as an async generator for type checkers

    @abstractmethod
    def is_model_supported(self, model: str) -> bool:
        """Return True if this backend can handle *model*."""

    @abstractmethod
    def list_supported_models(self) -> list[str]:
        """Return list of model aliases supported by this backend."""
