"""The provider boundary.

A provider is a thin, synchronous adapter around one LLM API. It receives a
fully assembled :class:`~contractforge.models.LLMRequest` and returns exactly
one :class:`~contractforge.models.LLMResponse`. Everything above the provider
(generation, pipeline, validation) is provider agnostic and must never rely on
a concrete implementation.

Provider selection is explicit and there is no fallback: if the selected
provider cannot be constructed, :func:`contractforge.providers.create_provider`
raises :class:`~contractforge.errors.ConfigError`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import LLMRequest, LLMResponse


@runtime_checkable
class Provider(Protocol):
    """Minimal protocol implemented by every provider adapter."""

    name: str
    model: str

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Execute one completion and record usage, latency and raw response."""
        ...
