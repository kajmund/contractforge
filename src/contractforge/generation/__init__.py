"""Contract generation layer: versioned prompts, JSON parsing and engine."""

from __future__ import annotations

from .engine import DocumentGenerator, GenerationOutcome
from .parsing import extract_json_object
from .prompts import PROMPT_VERSION, build_contract_request, prompt_sha256

__all__ = [
    "DocumentGenerator",
    "GenerationOutcome",
    "PROMPT_VERSION",
    "build_contract_request",
    "extract_json_object",
    "prompt_sha256",
]
