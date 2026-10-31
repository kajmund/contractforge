"""Typed errors with stable CLI exit codes.

The CLI maps every :class:`ContractForgeError` to a machine-readable error
envelope (with ``--json``) and to a stable process exit code, so automation can
react to failure classes without parsing human text.
"""

from __future__ import annotations

from typing import Any


class ContractForgeError(Exception):
    """Base class for all expected ContractForge failures."""

    exit_code: int = 1
    error_type: str = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.error_type,
            "message": self.message,
            "details": self.details,
            "exit_code": self.exit_code,
        }


class UsageError(ContractForgeError):
    """Invalid CLI arguments or unsupported option combination."""

    exit_code = 2
    error_type = "usage_error"


class ValidationError(ContractForgeError):
    """A dataset, document or gold file failed verification."""

    exit_code = 3
    error_type = "validation_error"


class ProviderError(ContractForgeError):
    """The LLM provider failed after retries, or credentials are missing."""

    exit_code = 4
    error_type = "provider_error"


class ConfigError(ContractForgeError):
    """Missing or invalid configuration."""

    exit_code = 5
    error_type = "config_error"


class ScenarioError(ContractForgeError):
    """A scenario could not be generated or is internally inconsistent."""

    exit_code = 6
    error_type = "scenario_error"


class GenerationError(ContractForgeError):
    """Provider output could not be turned into a valid structured document."""

    exit_code = 7
    error_type = "generation_error"


class ExportError(ContractForgeError):
    """Writing or packaging a dataset failed."""

    exit_code = 8
    error_type = "export_error"


class DatasetNotFoundError(UsageError):
    """The requested dataset directory or manifest does not exist."""

    error_type = "dataset_not_found"
