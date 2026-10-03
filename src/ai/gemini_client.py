"""Google Gemini backend client.

Uses the google-generativeai SDK to talk to the Gemini API.
Exposes the same interface as GitHubModelsClient (BaseAIClient) so the
AIRouter can dispatch to either backend without any changes to callers.
"""

from collections.abc import AsyncGenerator
from typing import Any

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from src.ai.base_client import BaseAIClient
from src.config import get_settings

logger = structlog.get_logger()
settings = get_settings()


class GeminiClient(BaseAIClient):
    """Client for Google Gemini API using google-generativeai SDK."""

    # Alias → canonical Gemini model ID
    SUPPORTED_MODELS: dict[str, str] = {
        "gemini-2.5-pro": "gemini-2.5-pro",
        "gemini-2.5-flash": "gemini-2.5-flash",
        "gemini-2.0-flash": "gemini-2.0-flash",
        "gemini-1.5-pro": "gemini-1.5-pro",
        "gemini-1.5-flash": "gemini-1.5-flash",
    }

    def __init__(self, api_key: str | None = None) -> None:
        import google.generativeai as genai  # lazy import — optional dependency

        self._api_key = api_key or settings.gemini_api_key
        if not self._api_key:
            raise ValueError(
                "GEMINI_API_KEY is not set. Set it in .env or pass api_key= to GeminiClient()."
            )
        genai.configure(api_key=self._api_key)
        self._genai = genai
        logger.info("gemini_client_initialized")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_model(self, alias: str) -> str:
        """Resolve an alias to a canonical model ID."""
        return self.SUPPORTED_MODELS.get(alias, alias)

    def _split_messages(
        self, messages: list[dict[str, str]]
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert OpenAI-format messages into (system_instruction, gemini_history).

        OpenAI roles:  system / user / assistant
        Gemini roles:  (system_instruction separately) / user / model
        """
        system_instruction: str | None = None
        history: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                # Gemini accepts a single system instruction at model construction
                system_instruction = (
                    (system_instruction + "\n\n" + content) if system_instruction else content
                )
            elif role == "user":
                history.append({"role": "user", "parts": [{"text": content}]})
            elif role == "assistant":
                history.append({"role": "model", "parts": [{"text": content}]})

        return system_instruction, history

    # ------------------------------------------------------------------
    # BaseAIClient interface
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True,
    )
    async def generate_response(
        self,
        messages: list[dict[str, str]],
        model: str = "gemini-2.0-flash",
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> tuple[str, int]:
        """Generate a full response from the Gemini API.

        Returns:
            (response_text, total_token_count)
        """
        from google.generativeai.types import GenerationConfig

        model_id = self._resolve_model(model)
        system_instruction, history = self._split_messages(messages)

        logger.debug(
            "gemini_generating_response",
            model=model_id,
            message_count=len(messages),
            temperature=temperature,
        )

        # The last entry in history must be from the user
        if not history or history[-1]["role"] != "user":
            logger.error("gemini_no_user_message_at_end", history_len=len(history))
            return "I couldn't process your request.", 0

        last_user_text: str = history[-1]["parts"][0]["text"]
        chat_history = history[:-1]  # everything except the pending user turn

        try:
            genai_model = self._genai.GenerativeModel(
                model_name=model_id,
                system_instruction=system_instruction,
                generation_config=GenerationConfig(
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ),
            )

            chat = genai_model.start_chat(history=chat_history)
            response = await chat.send_message_async(last_user_text)

            content: str = response.text or ""

            # usage_metadata is available on most models
            token_count: int = 0
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                token_count = getattr(response.usage_metadata, "total_token_count", 0) or 0

            logger.info(
                "gemini_response_generated",
                model=model_id,
                tokens=token_count,
                content_length=len(content),
            )
            return content, token_count

        except Exception as e:
            logger.error(
                "gemini_generation_failed",
                model=model_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise

    async def stream_response(
        self,
        messages: list[dict[str, str]],
        model: str = "gemini-2.0-flash",
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Stream response chunks from the Gemini API."""
        from google.generativeai.types import GenerationConfig

        model_id = self._resolve_model(model)
        system_instruction, history = self._split_messages(messages)

        if not history or history[-1]["role"] != "user":
            yield "I couldn't process your request."
            return

        last_user_text: str = history[-1]["parts"][0]["text"]
        chat_history = history[:-1]

        try:
            genai_model = self._genai.GenerativeModel(
                model_name=model_id,
                system_instruction=system_instruction,
                generation_config=GenerationConfig(
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ),
            )
            chat = genai_model.start_chat(history=chat_history)
            response = await chat.send_message_async(last_user_text, stream=True)

            async for chunk in response:
                if chunk.text:
                    yield chunk.text

        except Exception as e:
            logger.error(
                "gemini_streaming_failed",
                model=model_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise

    def is_model_supported(self, model: str) -> bool:
        return model in self.SUPPORTED_MODELS or model.startswith("gemini-")

    def list_supported_models(self) -> list[str]:
        return list(self.SUPPORTED_MODELS.keys())
