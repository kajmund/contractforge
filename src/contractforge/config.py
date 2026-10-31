"""Runtime configuration.

One settings object is the source of truth for a run. Secrets come from the
process environment; everything else can be overridden by CLI flags. There is
no hidden fallback: if the selected provider needs credentials and they are
missing, provider construction fails loudly with :class:`ConfigError`.

Environment variables
---------------------
``CEREBRAS_API_KEY``
    Credential for the Cerebras provider (never persisted, never logged).
``CEREBRAS_BASE_URL``
    Overrides the Cerebras OpenAI-compatible base URL.
``CONTRACTFORGE_PROVIDER``
    ``cerebras`` (default) or ``mock`` (deterministic offline test provider).
``CONTRACTFORGE_MODEL``
    Model id passed to the provider. Defaults to ``gpt-oss-120b``.
``CONTRACTFORGE_TIMEOUT_S``, ``CONTRACTFORGE_MAX_RETRIES``,
``CONTRACTFORGE_BACKOFF_S``, ``CONTRACTFORGE_CONCURRENCY``,
``CONTRACTFORGE_TEMPERATURE``, ``CONTRACTFORGE_TOP_P``,
``CONTRACTFORGE_MAX_TOKENS``, ``CONTRACTFORGE_MAX_REPAIRS``,
``CONTRACTFORGE_SEED``, ``CONTRACTFORGE_FORMATS``, ``CONTRACTFORGE_PREFIX``,
``CONTRACTFORGE_PRICING``
    Run-level defaults; CLI flags take precedence.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError
from .models import DocumentFormat, PricingTable

DEFAULT_CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"
DEFAULT_MODEL = "gpt-oss-120b"
DEFAULT_FORMATS: tuple[DocumentFormat, ...] = (
    DocumentFormat.TXT,
    DocumentFormat.MARKDOWN,
    DocumentFormat.DOCX,
    DocumentFormat.PDF,
)
SUPPORTED_PROVIDERS: tuple[str, ...] = ("cerebras", "mock")


def _get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _parse_int(env: Mapping[str, str], name: str, default: int, minimum: int = 0) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _parse_float(
    env: Mapping[str, str], name: str, default: float | None
) -> float | None:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def parse_formats(raw: str) -> tuple[DocumentFormat, ...]:
    """Parse a comma separated format list, accepting the ``md`` alias."""

    aliases = {
        "txt": DocumentFormat.TXT,
        "text": DocumentFormat.TXT,
        "md": DocumentFormat.MARKDOWN,
        "markdown": DocumentFormat.MARKDOWN,
        "docx": DocumentFormat.DOCX,
        "pdf": DocumentFormat.PDF,
    }
    formats: list[DocumentFormat] = []
    for token in raw.split(","):
        token = token.strip().lower()
        if not token:
            continue
        if token not in aliases:
            raise ConfigError(
                f"unknown document format {token!r}; choose from {sorted(aliases)}"
            )
        if aliases[token] not in formats:
            formats.append(aliases[token])
    if not formats:
        raise ConfigError("no document formats selected")
    return tuple(formats)


def load_pricing_table(path: str | Path | None) -> PricingTable | None:
    """Load an explicit price table.

    ContractForge ships no prices. Cost fields stay ``None`` with
    ``cost_basis == "unknown"`` until the user supplies a table.
    """

    if path is None:
        return None
    pricing_path = Path(path)
    if not pricing_path.exists():
        raise ConfigError(f"pricing file not found: {pricing_path}")
    try:
        payload = json.loads(pricing_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"pricing file is not valid JSON: {pricing_path}") from exc
    try:
        return PricingTable.model_validate(payload)
    except ValueError as exc:
        raise ConfigError(f"pricing file does not match PricingTable: {exc}") from exc


@dataclass(frozen=True)
class Settings:
    provider: str = "cerebras"
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_CEREBRAS_BASE_URL
    api_key: str | None = None
    request_timeout_s: float = 180.0
    max_retries: int = 4
    retry_backoff_s: float = 1.5
    concurrency: int = 4
    temperature: float = 0.7
    top_p: float | None = None
    max_tokens: int = 4000
    max_repair_attempts: int = 2
    seed: int | None = None
    formats: tuple[DocumentFormat, ...] = DEFAULT_FORMATS
    contract_id_prefix: str = "contract"
    pricing: PricingTable | None = field(default=None, compare=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        provider = (_get(env, "CONTRACTFORGE_PROVIDER") or "cerebras").lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ConfigError(
                f"unknown provider {provider!r}; choose from {', '.join(SUPPORTED_PROVIDERS)}"
            )

        model = (
            _get(env, "CONTRACTFORGE_MODEL")
            or _get(env, "CEREBRAS_MODEL")
            or DEFAULT_MODEL
        )
        base_url = _get(env, "CEREBRAS_BASE_URL") or DEFAULT_CEREBRAS_BASE_URL
        api_key = _get(env, "CEREBRAS_API_KEY")

        formats_raw = _get(env, "CONTRACTFORGE_FORMATS")
        formats = parse_formats(formats_raw) if formats_raw else DEFAULT_FORMATS

        top_p = _parse_float(env, "CONTRACTFORGE_TOP_P", None)
        temperature = _parse_float(env, "CONTRACTFORGE_TEMPERATURE", 0.7)
        assert temperature is not None  # default is not None
        if not 0.0 <= temperature <= 2.0:
            raise ConfigError("CONTRACTFORGE_TEMPERATURE must be between 0.0 and 2.0")
        if top_p is not None and not 0.0 < top_p <= 1.0:
            raise ConfigError("CONTRACTFORGE_TOP_P must be in (0.0, 1.0]")

        seed = _parse_int(env, "CONTRACTFORGE_SEED", 0)
        seed_value: int | None = seed if _get(env, "CONTRACTFORGE_SEED") is not None else None

        return cls(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            request_timeout_s=float(_parse_float(env, "CONTRACTFORGE_TIMEOUT_S", 180.0) or 180.0),
            max_retries=_parse_int(env, "CONTRACTFORGE_MAX_RETRIES", 4),
            retry_backoff_s=float(_parse_float(env, "CONTRACTFORGE_BACKOFF_S", 1.5) or 1.5),
            concurrency=_parse_int(env, "CONTRACTFORGE_CONCURRENCY", 4, minimum=1),
            temperature=temperature,
            top_p=top_p,
            max_tokens=_parse_int(env, "CONTRACTFORGE_MAX_TOKENS", 4000, minimum=1),
            max_repair_attempts=_parse_int(env, "CONTRACTFORGE_MAX_REPAIRS", 2),
            seed=seed_value,
            formats=formats,
            contract_id_prefix=_get(env, "CONTRACTFORGE_PREFIX") or "contract",
            pricing=load_pricing_table(_get(env, "CONTRACTFORGE_PRICING")),
        )

    def with_overrides(self, **overrides) -> Settings:
        from dataclasses import replace

        clean = {key: value for key, value in overrides.items() if value is not None}
        unknown = set(clean) - set(self.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"unknown settings override(s): {sorted(unknown)}")
        return replace(self, **clean)
