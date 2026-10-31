"""Robust extraction of the first JSON object from model text.

Models sometimes wrap JSON in markdown fences or add a sentence before/after
the object. The extractor scans every ``{`` in the text, finds its balanced
closing brace (tracking strings and escapes so braces inside strings do not
confuse it) and returns the first candidate that parses as a JSON object.

All failures are :class:`~contractforge.errors.GenerationError`: provider text
that cannot be structured is a generation failure, not a provider failure.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from ..errors import GenerationError


def extract_json_object(text: str) -> dict[str, Any]:
    """Return the first JSON object embedded in ``text``."""

    if not isinstance(text, str) or not text.strip():
        raise GenerationError("provider returned empty text; expected a JSON object")

    for start, end in _candidate_objects(text):
        try:
            payload = json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    raise GenerationError("provider text does not contain a valid JSON object")


def _candidate_objects(text: str) -> Iterator[tuple[int, int]]:
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return
        end = _balanced_end(text, start)
        if end is not None:
            yield start, end
        index = start + 1


def _balanced_end(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(text)):
        char = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return position + 1
    return None
