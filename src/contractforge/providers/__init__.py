"""Provider factory.

Provider selection is explicit, never a fallback: an unknown provider or a
missing credential raises :class:`~contractforge.errors.ConfigError` instead of
silently choosing something else.
"""

from __future__ import annotations

from ..config import DEFAULT_MODEL, SUPPORTED_PROVIDERS, Settings
from ..errors import ConfigError
from .base import Provider
from .cerebras import CerebrasProvider
from .mock import MOCK_MODEL, MockProvider

__all__ = [
    "CerebrasProvider",
    "MOCK_MODEL",
    "MockProvider",
    "Provider",
    "create_provider",
]


def create_provider(settings: Settings) -> Provider:
    """Build the provider selected by ``settings`` or fail loudly."""

    provider = (settings.provider or "").strip().lower()
    if provider == "cerebras":
        if not settings.api_key:
            raise ConfigError(
                "CEREBRAS_API_KEY is not set. Export the key for live generation or "
                "select the explicit offline provider (provider='mock')."
            )
        return CerebrasProvider(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            timeout_s=settings.request_timeout_s,
            max_retries=settings.max_retries,
            backoff_s=settings.retry_backoff_s,
            temperature=settings.temperature,
            top_p=settings.top_p,
            max_tokens=settings.max_tokens,
        )
    if provider == "mock":
        # ``Settings.from_env`` keeps the real-provider default model even when
        # the provider is mock; the mock provider advertises its own model id.
        model = settings.model if settings.model and settings.model != DEFAULT_MODEL else MOCK_MODEL
        return MockProvider(model=model)
    raise ConfigError(
        f"unknown provider {settings.provider!r}; choose from {', '.join(SUPPORTED_PROVIDERS)}"
    )
