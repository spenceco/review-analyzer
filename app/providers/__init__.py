"""Pick a provider implementation from settings."""

from __future__ import annotations

from ..config import Settings
from .base import (
    LLMProvider,
    PermanentProviderError,
    ProviderError,
    TransientProviderError,
)

__all__ = [
    "LLMProvider",
    "PermanentProviderError",
    "ProviderError",
    "TransientProviderError",
    "create_provider",
]


def create_provider(settings: Settings) -> LLMProvider:
    """Build the configured provider. Add a branch here to support another vendor."""
    if settings.ai_provider == "fake":
        from .fake import FakeProvider

        return FakeProvider()
    if settings.ai_provider == "gemini":
        from .gemini import GeminiProvider

        if settings.gemini_api_key is None or not settings.gemini_api_key.get_secret_value():
            raise RuntimeError("GEMINI_API_KEY is not set. Add it to .env or use AI_PROVIDER=fake.")
        return GeminiProvider(
            api_key=settings.gemini_api_key.get_secret_value(),
            model=settings.gemini_model,
            timeout=settings.request_timeout_seconds,
        )
    raise RuntimeError(f"Unknown AI_PROVIDER: {settings.ai_provider}")
