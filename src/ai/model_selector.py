"""Model selection service for AI interactions."""

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database.repositories import (
    ChannelConfigRepository,
    ConversationRepository,
    UserRepository,
)

logger = structlog.get_logger()
settings = get_settings()


class ModelSelector:
    """Selects the appropriate AI model based on preferences."""

    def __init__(self, db_session: AsyncSession):
        self.db_session = db_session
        self.user_repo = UserRepository(db_session)
        self.conversation_repo = ConversationRepository(db_session)
        self.channel_config_repo = ChannelConfigRepository(db_session)

    async def select_model(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        channel_type: str,
    ) -> str:
        """
        Select model based on priority:
        1. Conversation model override
        2. User preferred model
        3. Channel default model
        4. System default model

        Args:
            user_id: User ID
            conversation_id: Conversation ID
            channel_type: Channel type (telegram, slack)

        Returns:
            Selected model name
        """
        # Check conversation override
        conversation = await self.conversation_repo.get_by_id(conversation_id)
        if conversation and conversation.model_override:
            logger.debug(
                "model_selected_from_conversation",
                model=conversation.model_override,
                conversation_id=str(conversation_id),
            )
            return conversation.model_override

        # Check user preference
        user = await self.user_repo.get_by_id(user_id)
        if user and user.preferred_model:
            logger.debug(
                "model_selected_from_user",
                model=user.preferred_model,
                user_id=str(user_id),
            )
            return user.preferred_model

        # Check channel default
        channel_config = await self.channel_config_repo.get_by_channel_type(channel_type)
        if channel_config:
            logger.debug(
                "model_selected_from_channel",
                model=channel_config.default_model,
                channel_type=channel_type,
            )
            return channel_config.default_model

        # Fall back to system default
        logger.debug(
            "model_selected_default",
            model=settings.default_model,
            channel_type=channel_type,
        )
        return settings.default_model

    async def set_user_model(self, user_id: uuid.UUID, model: str) -> bool:
        """Set user's preferred model."""
        user = await self.user_repo.update_preferred_model(user_id, model)
        return user is not None

    async def set_conversation_model(self, conversation_id: uuid.UUID, model: str) -> bool:
        """Set conversation model override."""
        conversation = await self.conversation_repo.set_model_override(conversation_id, model)
        return conversation is not None

    async def set_channel_default(self, channel_type: str, model: str) -> bool:
        """Set channel default model."""
        config = await self.channel_config_repo.update_default_model(channel_type, model)
        if not config:
            # Create if doesn't exist
            config = await self.channel_config_repo.create(channel_type, model)
        return config is not None

    @staticmethod
    def is_valid_model(model: str) -> bool:
        """Return True if *model* is a known alias for any supported backend.

        Accepts GitHub Models aliases and all gemini-* model names.
        """
        from src.ai.gemini_client import GeminiClient
        from src.ai.github_models import GitHubModelsClient

        github_aliases = set(GitHubModelsClient.SUPPORTED_MODELS.keys())
        gemini_aliases = set(GeminiClient.SUPPORTED_MODELS.keys())
        known = github_aliases | gemini_aliases
        return model in known or model.startswith("gemini-")
