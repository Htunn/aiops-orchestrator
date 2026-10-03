"""vLLM backend client.

vLLM is a high-performance inference engine for LLMs with OpenAI-compatible API.
Supports models like Llama, Mistral, Qwen, and many others.
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


class VLLMClient(BaseAIClient):
    """Client for vLLM server using OpenAI-compatible API."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        """
        Initialize vLLM client.

        Args:
            base_url: vLLM server URL (e.g., "http://localhost:8000/v1")
            api_key: API key (optional, vLLM doesn't require it by default)
        """
        self.base_url = base_url or settings.vllm_base_url
        self.api_key = api_key or settings.vllm_api_key or "EMPTY"

        if not self.base_url:
            raise ValueError(
                "vLLM base URL not configured. Set VLLM_BASE_URL environment variable."
            )

        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        logger.info("vllm_client_initialized", base_url=self.base_url)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True,
    )
    async def generate_response(
        self,
        messages: list[dict[str, str]],
        model: str = "meta-llama/Llama-2-7b-chat-hf",
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> tuple[str, int]:
        """Generate AI response from vLLM server."""
        logger.debug(
            "generating_vllm_response",
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
            tokens = response.usage.total_tokens if response.usage else 0

            logger.info(
                "vllm_response_generated",
                model=model,
                tokens=tokens,
                content_length=len(content),
            )

            return content, tokens

        except Exception as e:
            logger.error(
                "vllm_generation_failed",
                model=model,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise

    async def stream_response(
        self,
        messages: list[dict[str, str]],
        model: str = "meta-llama/Llama-2-7b-chat-hf",
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Stream AI response from vLLM server."""
        logger.debug("streaming_vllm_response", model=model, message_count=len(messages))

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
                if chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

        except Exception as e:
            logger.error("vllm_streaming_failed", model=model, error=str(e))
            raise

    def is_model_supported(self, model: str) -> bool:
        """Check if model is supported."""
        patterns = [
            "llama",
            "mistral",
            "qwen",
            "phi",
            "vicuna",
            "yi",
            "mixtral",
            "deepseek",
            "codellama",
            "solar",
        ]
        model_lower = model.lower()
        return any(pattern in model_lower for pattern in patterns)

    def list_supported_models(self) -> list[str]:
        """Return list of commonly supported models."""
        return [
            "meta-llama/Llama-2-7b-chat-hf",
            "meta-llama/Llama-2-13b-chat-hf",
            "meta-llama/Llama-2-70b-chat-hf",
            "meta-llama/Meta-Llama-3-8B-Instruct",
            "meta-llama/Meta-Llama-3-70B-Instruct",
            "mistralai/Mistral-7B-Instruct-v0.2",
            "mistralai/Mixtral-8x7B-Instruct-v0.1",
            "Qwen/Qwen-7B-Chat",
            "Qwen/Qwen-14B-Chat",
            "microsoft/phi-2",
            "deepseek-ai/deepseek-coder-6.7b-instruct",
        ]
