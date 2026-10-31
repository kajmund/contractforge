"""Shared credential scrubber for everything that can be written or logged.

Two independent policies live here, because two different risks do:

* :func:`scrub_text` / :func:`scrub_value` redact credential-*looking* values
  (``csk-…`` / ``sk-…`` / ``key_…`` tokens, ``Bearer …``). They are safe on any
  string — an HTTP error body, a provider exception message, a raw response —
  and keep the surrounding text readable.
* :func:`is_secret_key` identifies object *keys* whose value must be dropped
  entirely (``api_key``, ``authorization``, ``password``, …). Use
  :func:`sanitize` when a whole structure must never carry credentials.

The export store and the provider adapters share this module so a credential
cannot leak through a path that was forgotten in one of them.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# A key whose *value* is dropped, not rewritten. Matching is done on the
# lowercased key with ``-`` normalised to ``_``, by substring.
_SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "api_secret",
    "apisecret",
    "authorization",
    "secret",
    "password",
    "access_token",
    "refresh_token",
    "bearer",
    "credential",
    "private_key",
)

# A *value* that looks like a credential, wherever it appears in free text.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(?:csk|sk|key)[-_][A-Za-z0-9._-]{16,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{8,}\b"),
)


def is_secret_key(key: str) -> bool:
    """True when a mapping key names a credential and its value must be dropped."""

    normalized = key.lower().replace("-", "_")
    return any(marker.replace("-", "_") in normalized for marker in _SECRET_KEY_MARKERS)


def scrub_text(value: str) -> str:
    """Replace credential-looking substrings in free text with ``[REDACTED]``."""

    for pattern in _SECRET_VALUE_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def scrub_value(value: Any) -> Any:
    """Recursively redact credential-looking *values*, keeping every key.

    Unlike :func:`sanitize` this keeps the shape of the structure, so a redacted
    HTTP body or provider ``details`` payload stays explainable.
    """

    if isinstance(value, dict):
        return {key: scrub_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_value(item) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def sanitize(value: Any) -> Any:
    """Recursively drop credential keys and redact credential-looking values."""

    if isinstance(value, dict):
        return {
            key: sanitize(item) for key, item in value.items() if not is_secret_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value
