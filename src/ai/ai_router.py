"""AI client router — dispatches to the right backend by model prefix.

Routing rules (checked in order):
  1. Model name starts with "gemini-"  → GeminiClient
  2. Model name contains "/" or starts with "vllm:"  → VLLMClient
  3. Model name is Ollama model or starts with "ollama:"  → OllamaClient
  4. Everything else                   → GitHubModelsClient (GitHub Models / Azure AI Inference)

This lets callers use any model alias (e.g. "gpt-4", "gemini-2.0-flash", "llama2", "meta-llama/Llama-2-7b-chat-hf")
without caring which backend handles it.
"""

from collections.abc import AsyncGenerator
from typing import Any

import structlog

from src.ai.base_client import BaseAIClient
from src.ai.gemini_client import GeminiClient
from src.ai.github_models import GitHubModelsClient
from src.ai.ollama_client import OllamaClient
from src.ai.vllm_client import VLLMClient
from src.config import get_settings

logger = structlog.get_logger()
settings = get_settings()


class AIRouter(BaseAIClient):
    """Routes AI calls to the appropriate backend based on model prefix."""

    def __init__(self) -> None:
        self._github = GitHubModelsClient()

        # GeminiClient is optional — only instantiate if the key is configured
        self._gemini: GeminiClient | None = None
        if settings.gemini_api_key:
            try:
                self._gemini = GeminiClient()
                logger.info("ai_router_gemini_enabled")
            except Exception as e:
                logger.warning("ai_router_gemini_init_failed", error=str(e))
        else:
            logger.info("ai_router_gemini_disabled", reason="GEMINI_API_KEY not set")

        # VLLMClient is optional — only instantiate if base URL is configured
        self._vllm: VLLMClient | None = None
        if settings.vllm_base_url:
            try:
                self._vllm = VLLMClient()
                logger.info("ai_router_vllm_enabled", base_url=settings.vllm_base_url)
            except Exception as e:
                logger.warning("ai_router_vllm_init_failed", error=str(e))
        else:
            logger.debug("ai_router_vllm_disabled", reason="VLLM_BASE_URL not set")

        # OllamaClient is optional — only instantiate if base URL is configured
        self._ollama: OllamaClient | None = None
        if settings.ollama_base_url:
            try:
                self._ollama = OllamaClient()
                logger.info("ai_router_ollama_enabled", base_url=settings.ollama_base_url)
            except Exception as e:
                logger.warning("ai_router_ollama_init_failed", error=str(e))
        else:
            logger.debug("ai_router_ollama_disabled", reason="OLLAMA_BASE_URL not set")

        backends = ["github_models"]
        if self._gemini:
            backends.append("gemini")
        if self._vllm:
            backends.append("vllm")
        if self._ollama:
            backends.append("ollama")

        logger.info("ai_router_initialized", backends=backends)

    # ------------------------------------------------------------------
    # Routing helpers
    # ------------------------------------------------------------------

    def _is_gemini_model(self, model: str) -> bool:
        return model.lower().startswith("gemini")

    def _is_vllm_model(self, model: str) -> bool:
        """Check if model should be routed to vLLM.

        vLLM models typically have "/" in the name (e.g., meta-llama/Llama-2-7b-chat-hf)
        or start with "vllm:" prefix.
        """
        if model.startswith("vllm:"):
            return True
        # hf.co/ is Ollama's HuggingFace model ref format, not vLLM
        if model.startswith("hf.co/"):
            return False
        return "/" in model

    def _is_ollama_model(self, model: str) -> bool:
        """Check if model should be routed to Ollama.

        Ollama models are typically simple names like "llama2", "mistral", etc.
        or start with "ollama:" prefix.
        """
        if model.startswith("ollama:"):
            return True

        model_lower = model.lower()
        # HuggingFace-format Ollama model refs (e.g., hf.co/htunn/gemma-4-e2b-aiops-gguf:Q4_K_M)
        if model_lower.startswith("hf.co/"):
            return True
        # Common Ollama model names/patterns
        ollama_patterns = [
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
        return any(model_lower.startswith(p) or model_lower == p for p in ollama_patterns)

    def _strip_provider_prefix(self, model: str) -> str:
        """Strip provider prefix from model name (e.g., 'vllm:model' → 'model')."""
        if ":" in model:
            parts = model.split(":", 1)
            if parts[0] in ["vllm", "ollama"]:
                return parts[1]
        return model

    def _backend_for(self, model: str) -> BaseAIClient:
        """Return the correct backend client for *model*."""
        # Check in order of priority
        if self._is_gemini_model(model):
            if self._gemini is None:
                raise RuntimeError(
                    f"Model '{model}' requires Gemini but GEMINI_API_KEY is not set."
                )
            return self._gemini

        if self._is_vllm_model(model):
            if self._vllm is None:
                raise RuntimeError(f"Model '{model}' requires vLLM but VLLM_BASE_URL is not set.")
            return self._vllm

        if self._is_ollama_model(model):
            if self._ollama is None:
                raise RuntimeError(
                    f"Model '{model}' requires Ollama but OLLAMA_BASE_URL is not set."
                )
            return self._ollama

        # Default to GitHub Models
        return self._github

    # ------------------------------------------------------------------
    # BaseAIClient interface
    # ------------------------------------------------------------------

    async def generate_response(
        self,
        messages: list[dict[str, str]],
        model: str = "gpt-4",
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> tuple[str, int]:
        # Strip provider prefix if present
        actual_model = self._strip_provider_prefix(model)
        backend = self._backend_for(model)
        logger.debug(
            "ai_router_dispatch",
            model=model,
            actual_model=actual_model,
            backend=type(backend).__name__,
        )
        return await backend.generate_response(
            messages=messages,
            model=actual_model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    async def stream_response(
        self,
        messages: list[dict[str, str]],
        model: str = "gpt-4",
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        # Strip provider prefix if present
        actual_model = self._strip_provider_prefix(model)
        backend = self._backend_for(model)
        async for chunk in backend.stream_response(
            messages=messages,
            model=actual_model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        ):
            yield chunk

    def is_model_supported(self, model: str) -> bool:
        actual_model = self._strip_provider_prefix(model)

        if self._is_gemini_model(model):
            return self._gemini is not None and self._gemini.is_model_supported(actual_model)
        if self._is_vllm_model(model):
            return self._vllm is not None and self._vllm.is_model_supported(actual_model)
        if self._is_ollama_model(model):
            return self._ollama is not None and self._ollama.is_model_supported(actual_model)

        return self._github.is_model_supported(actual_model)

    def list_supported_models(self) -> list[str]:
        models = self._github.list_supported_models()
        if self._gemini:
            models = models + self._gemini.list_supported_models()
        if self._vllm:
            models = models + [f"vllm:{m}" for m in self._vllm.list_supported_models()]
        if self._ollama:
            models = models + [f"ollama:{m}" for m in self._ollama.list_supported_models()]
        return models
