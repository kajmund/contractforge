from __future__ import annotations

import json

import pytest

from contractforge.config import Settings, load_pricing_table, parse_formats
from contractforge.errors import ConfigError
from contractforge.models import DocumentFormat, PricingTable


def test_defaults():
    settings = Settings.from_env({})
    assert settings.provider == "cerebras"
    assert settings.model == "gpt-oss-120b"
    assert settings.concurrency == 4
    assert DocumentFormat.PDF in settings.formats
    assert settings.pricing is None


def test_env_overrides():
    settings = Settings.from_env(
        {
            "CONTRACTFORGE_PROVIDER": "mock",
            "CONTRACTFORGE_MODEL": "mock-1",
            "CONTRACTFORGE_CONCURRENCY": "2",
            "CONTRACTFORGE_SEED": "123",
            "CONTRACTFORGE_FORMATS": "txt,md",
            "CEREBRAS_API_KEY": "csk-test",
        }
    )
    assert settings.provider == "mock"
    assert settings.model == "mock-1"
    assert settings.concurrency == 2
    assert settings.seed == 123
    assert settings.formats == (DocumentFormat.TXT, DocumentFormat.MARKDOWN)


def test_unknown_provider_fails_loudly():
    with pytest.raises(ConfigError, match="provider"):
        Settings.from_env({"CONTRACTFORGE_PROVIDER": "acme"})


def test_invalid_numbers_fail():
    with pytest.raises(ConfigError):
        Settings.from_env({"CONTRACTFORGE_CONCURRENCY": "many"})
    with pytest.raises(ConfigError):
        Settings.from_env({"CONTRACTFORGE_TEMPERATURE": "3.0"})


def test_parse_formats_aliases():
    assert parse_formats("txt,md") == (DocumentFormat.TXT, DocumentFormat.MARKDOWN)
    assert parse_formats("markdown,docx,pdf") == (
        DocumentFormat.MARKDOWN,
        DocumentFormat.DOCX,
        DocumentFormat.PDF,
    )
    with pytest.raises(ConfigError):
        parse_formats("txt,papyrus")


def test_pricing_table_only_from_explicit_file(tmp_path):
    pricing_path = tmp_path / "pricing.json"
    pricing_path.write_text(
        json.dumps(
            {
                "providers": {
                    "cerebras": {
                        "gpt-oss-120b": {
                            "input_usd_per_million": 0.35,
                            "output_usd_per_million": 0.75,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    table = load_pricing_table(pricing_path)
    assert isinstance(table, PricingTable)
    price = table.price_for("cerebras", "gpt-oss-120b")
    assert price is not None
    assert price.input_usd_per_million == 0.35
    assert table.price_for("cerebras", "unknown-model") is None

    settings = Settings.from_env({"CONTRACTFORGE_PRICING": str(pricing_path)})
    assert settings.pricing is not None


def test_missing_pricing_file_fails(tmp_path):
    with pytest.raises(ConfigError):
        load_pricing_table(tmp_path / "nope.json")


def test_with_overrides_ignores_none_and_validates_keys():
    settings = Settings.from_env({})
    changed = settings.with_overrides(concurrency=2, model=None)
    assert changed.concurrency == 2
    assert changed.model == settings.model
    with pytest.raises(ConfigError):
        settings.with_overrides(unknown_field=1)
