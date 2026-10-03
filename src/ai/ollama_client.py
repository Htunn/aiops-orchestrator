"""Ollama backend client.

Ollama is a local LLM runner with OpenAI-compatible API.
Supports models like Llama, Mistral, CodeLlama, and many others.
"""

from collections.abc import AsyncGenerator
from typing import Any, cast

import structlog
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from src.ai.base_client import BaseAIClient
from src.config import get_settings

logger = structlog.get_logger()
settings = get_settings()


class OllamaClient(BaseAIClient):
    """Client for Ollama server using OpenAI-compatible API."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        """
        Initialize Ollama client.

        Args:
            base_url: Ollama server URL (e.g., "http://localhost:11434/v1")
            api_key: API key (optional, Ollama doesn't require it)
        """
        self.base_url = base_url or settings.ollama_base_url
        self.api_key = api_key or "ollama"

        if not self.base_url:
            raise ValueError(
                "Ollama base URL not configured. Set OLLAMA_BASE_URL environment variable."
            )

        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        logger.info("ollama_client_initialized", base_url=self.base_url)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True,
    )
    async def generate_response(
        self,
        messages: list[dict[str, str]],
        model: str = "llama2",
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> tuple[str, int]:
        """Generate AI response from Ollama server."""
        logger.debug(
            "generating_ollama_response",
            model=model,
            message_count=len(messages),
            temperature=temperature,
        )

        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=cast(list[Any], messages),
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )

            content = response.choices[0].message.content or ""
            tokens = response.usage.total_tokens if response.usage else len(content.split())

            logger.info(
                "ollama_response_generated",
                model=model,
                tokens=tokens,
                content_length=len(content),
            )

            return content, tokens

        except Exception as e:
            logger.error(
                "ollama_generation_failed",
                model=model,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise

    async def stream_response(
        self,
        messages: list[dict[str, str]],
        model: str = "llama2",
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Stream AI response from Ollama server."""
        logger.debug("streaming_ollama_response", model=model, message_count=len(messages))

        try:
            stream = await self.client.chat.completions.create(
                model=model,
                messages=cast(list[Any], messages),
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                **kwargs,
            )
            async for chunk in cast(Any, stream):
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                # Thinking models (e.g. Gemma 4) stream text via `reasoning`, not `content`
                text = delta.content or getattr(delta, "reasoning", "") or ""
                if text:
                    yield text

        except Exception as e:
            logger.error("ollama_streaming_failed", model=model, error=str(e))
            raise

    def is_model_supported(self, model: str) -> bool:
        """Check if model is supported."""
        model_lower = model.lower()
        # HuggingFace-format Ollama refs are always supported
        if model_lower.startswith("hf.co/"):
            return True
        common_models = [
            "llama2",
            "llama3",
            "mistral",
            "mixtral",
            "codellama",
            "phi",
            "neural-chat",
            "vicuna",
            "qwen",
            "deepseek-coder",
            "orca-mini",
            "solar",
            "yi",
            "gemma",
        ]
        return any(model_lower.startswith(m) or model_lower == m for m in common_models)

    def list_supported_models(self) -> list[str]:
        """Return list of commonly used Ollama models."""
        return [
            "llama2",
            "llama2:7b",
            "llama2:13b",
            "llama2:70b",
            "llama3:8b",
            "llama3:70b",
            "mistral",
            "mistral:7b",
            "mixtral:8x7b",
            "codellama",
            "codellama:7b",
            "codellama:13b",
            "phi",
            "phi:2.7b",
            "neural-chat",
            "vicuna",
            "orca-mini",
            "qwen:7b",
            "qwen:14b",
            "deepseek-coder:6.7b",
            "solar:10.7b",
            "yi:6b",
            "yi:34b",
            "gemma4:e2b",
            "hf.co/htunn/gemma-4-e2b-aiops-gguf:Q4_K_M",
        ]
