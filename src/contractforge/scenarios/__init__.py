"""Scenario subsystem: data-driven contract definitions and deterministic generation.

Everything contract-type specific lives in ``definitions/*.json``; the public
API is the registry and the generator described in ``docs/architecture.md``.
"""

from __future__ import annotations

from .generator import ScenarioGenerator
from .registry import (
    ContractTypeDefinition,
    ContractTypeRegistry,
    load_registry,
)

__all__ = [
    "ContractTypeDefinition",
    "ContractTypeRegistry",
    "ScenarioGenerator",
    "load_registry",
]
