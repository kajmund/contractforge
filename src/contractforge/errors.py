"""Typed errors with stable CLI exit codes.

The CLI maps every :class:`ContractForgeError` to a machine-readable error
envelope (with ``--json``) and to a stable process exit code, so automation can
react to failure classes without parsing human text.

Errors may also carry *generation accounting*: what the provider produced before
the failure was detected. ``ProviderError.attempts``, ``GenerationError.calls``
and the ``generation`` entry in :meth:`ContractForgeError.to_dict` let the
pipeline book the calls of a failed attempt instead of losing them (and the
tokens they cost).

Messages and ``details`` are scrubbed at this boundary: an exception must never
carry a credential into a log line, a rejected-contract file or a CLI error
envelope, no matter what upstream code passed in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .sanitize import scrub_text, scrub_value

if TYPE_CHECKING:
    from .models import LLMCallRecord


class ContractForgeError(Exception):
    """Base class for all expected ContractForge failures."""

    exit_code: int = 1
    error_type: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        generation: dict[str, Any] | None = None,
    ) -> None:
        message = scrub_text(message)
        details = scrub_value(details) if details else {}
        generation = scrub_value(generation) if generation else None
        super().__init__(message)
        self.message = message
        self.details = details
        self.generation = generation

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.error_type,
            "message": self.message,
            "details": self.details,
            "exit_code": self.exit_code,
        }
        if self.generation is not None:
            payload["generation"] = self.generation
        return payload


class UsageError(ContractForgeError):
    """Invalid CLI arguments or unsupported option combination."""

    exit_code = 2
    error_type = "usage_error"


class ValidationError(ContractForgeError):
    """A dataset, document or gold file failed verification."""

    exit_code = 3
    error_type = "validation_error"


class ProviderError(ContractForgeError):
    """The LLM provider failed after retries, or credentials are missing.

    ``attempts`` is the number of failed provider attempts. A provider failure
    happens *before* a usable response exists, so there is nothing to book:
    ``calls`` is always empty.
    """

    exit_code = 4
    error_type = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message, details=details)
        self.attempts = attempts
        self.calls: list[LLMCallRecord] = []

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["generation"] = {"calls": [], "attempts": self.attempts}
        return payload


class ConfigError(ContractForgeError):
    """Missing or invalid configuration."""

    exit_code = 5
    error_type = "config_error"


class ScenarioError(ContractForgeError):
    """A scenario could not be generated or is internally inconsistent."""

    exit_code = 6
    error_type = "scenario_error"


class GenerationError(ContractForgeError):
    """Provider output could not be turned into a valid structured document.

    ``calls`` holds every :class:`~contractforge.models.LLMCallRecord` the
    attempt produced before the failure, so the tokens spent on a rejected
    attempt are never lost when the pipeline books the outcome.
    """

    exit_code = 7
    error_type = "generation_error"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        calls: list[LLMCallRecord] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.calls: list[LLMCallRecord] = list(calls or [])

    def with_calls(self, calls: list[LLMCallRecord]) -> GenerationError:
        """Prepend earlier calls of the same attempt; returns ``self``."""

        self.calls = [*calls, *self.calls]
        return self

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["generation"] = {
            "attempts": self.calls[-1].attempt if self.calls else 0,
            "calls": [call.model_dump(mode="json") for call in self.calls],
        }
        return payload


class ExportError(ContractForgeError):
    """Writing or packaging a dataset failed."""

    exit_code = 8
    error_type = "export_error"


class DatasetNotFoundError(UsageError):
    """The requested dataset directory or manifest does not exist."""

    error_type = "dataset_not_found"
