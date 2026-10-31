"""Live smoke test against the real Cerebras API.

Skipped unless ``CEREBRAS_API_KEY`` is set. Run with::

    CEREBRAS_API_KEY=... .venv/bin/python -m pytest -m live -q

This test is intentionally small (one contract) so it can be run in CI-less
environments without spending meaningful money.
"""

from __future__ import annotations

import os

import pytest

from contractforge.config import Settings
from contractforge.export.loader import load_dataset
from contractforge.models import ContractType, Language, ScenarioRequest
from contractforge.pipeline import generate_dataset
from contractforge.validation.dataset import validate_dataset

pytestmark = pytest.mark.live


@pytest.fixture()
def cerebras_settings() -> Settings:
    if not os.environ.get("CEREBRAS_API_KEY"):
        pytest.skip("CEREBRAS_API_KEY saknas")
    env = {
        "CONTRACTFORGE_PROVIDER": "cerebras",
        "CONTRACTFORGE_FORMATS": "txt,markdown",
        "CONTRACTFORGE_CONCURRENCY": "1",
        "CONTRACTFORGE_MAX_REPAIRS": "1",
        "CONTRACTFORGE_TIMEOUT_S": "240",
        "CONTRACTFORGE_MAX_TOKENS": "6000",
        "CEREBRAS_API_KEY": os.environ["CEREBRAS_API_KEY"],
    }
    if os.environ.get("CEREBRAS_BASE_URL"):
        env["CEREBRAS_BASE_URL"] = os.environ["CEREBRAS_BASE_URL"]
    return Settings.from_env(env)


def test_live_cerebras_vertical_chain(tmp_path, cerebras_settings):
    request = ScenarioRequest(
        contract_type=ContractType.CONSULTING,
        count=1,
        clauses=6,
        difficulty=1,
        language=Language.SV,
        seed=4242,
    )
    dataset = tmp_path / "dataset"
    outcome = generate_dataset(cerebras_settings, request, dataset)

    assert outcome.stats.calls_total >= 1
    assert outcome.stats.total_tokens > 0
    assert outcome.stats.contracts_total == 1

    if outcome.stats.contracts_rejected:
        report = outcome.rejected[0].verification
        failures = [check.message for check in report.failed_checks]
        pytest.fail("Cerebras-genererat avtal kasserades: " + "; ".join(failures))

    contracts = load_dataset(dataset)
    assert len(contracts) == 1
    contract = contracts[0]
    assert contract.manifest.provider == "cerebras"
    assert contract.manifest.cost.cost_basis == "unknown"
    assert contract.questions

    report = validate_dataset(dataset, expect_min_contracts=1)
    assert report.status.value == "PASS", report.errors
