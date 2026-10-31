"""Benchmark verifiability tests.

A benchmark report is only useful if it can be checked. These tests generate a
small mock corpus and assert that the report:

* is tied to the exact corpus it describes (dataset manifest hash + per-contract
  manifest hashes that match the files on disk);
* records the runtime that produced it and the scenario inputs required to
  reproduce it;
* reports question rejections instead of silently dropping them;
* uses a weighted latency (all calls), and
* is arithmetically consistent (rates equal their own numerators/denominators).

They are deliberately small (``count=3``) so they run in CI with the
deterministic mock provider and no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from contractforge.benchmark import run_benchmark
from contractforge.config import Settings
from contractforge.models import ContractType, Language, ScenarioRequest, utc_now
from contractforge.utils import read_json, sha256_file


@pytest.fixture(scope="module")
def benchmark_run(tmp_path_factory) -> tuple[Path, dict]:
    """One small mock benchmark, shared by the assertions below."""

    output_dir = tmp_path_factory.mktemp("benchmarks")
    settings = Settings.from_env().with_overrides(provider="mock")
    request = ScenarioRequest(
        contract_type=ContractType.CONSULTING,
        count=3,
        clauses=8,
        difficulty=2,
        language=Language.SV,
        seed=1234,
    )
    benchmark_id = f"bench_test_{utc_now().strftime('%Y%m%dT%H%M%S%f')}"
    report = run_benchmark(
        settings, request, output_dir, benchmark_id=benchmark_id
    )
    report_path = output_dir / f"{report.benchmark_id}.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    return output_dir / report.benchmark_id, payload


def _details(payload: dict) -> dict:
    return payload["details"]


def test_report_records_dataset_manifest_hash_that_matches_disk(benchmark_run) -> None:
    run_dir, payload = benchmark_run
    dataset_root = run_dir / "dataset"
    manifest_path = dataset_root / "dataset_manifest.json"

    assert manifest_path.is_file(), "benchmarken måste skriva ett dataset-manifest"
    assert _details(payload)["dataset_manifest_sha256"] == sha256_file(manifest_path)

    manifest = read_json(manifest_path)
    record_hashes = {
        record["contract_id"]: record["manifest_sha256"] for record in manifest["contracts"]
    }
    assert _details(payload)["dataset_contracts_hashes"] == record_hashes

    # Every recorded hash must be the hash of the committed manifest.json file.
    for contract_id, digest in record_hashes.items():
        contract_dir = dataset_root / contract_id
        assert sha256_file(contract_dir / "manifest.json") == digest
    assert _details(payload)["dataset_contract_count"] == len(record_hashes) == 3


def test_report_records_runtime_identity(benchmark_run) -> None:
    _run_dir, payload = benchmark_run
    runtime = _details(payload)["runtime"]

    assert runtime["python_version"] == f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    assert runtime["contractforge_version"] == payload["contractforge_version"]
    assert runtime["platform"]
    assert "git_commit" in runtime
    assert runtime["environment"]["python"] == runtime["python_version"]


def test_report_records_complete_scenario_inputs(benchmark_run) -> None:
    """Fields required to reproduce the scenario corpus must be in the report."""

    _run_dir, payload = benchmark_run
    config = payload["config"]

    assert config["seed"] == 1234
    assert config["contract_type"] == "consulting"
    assert config["count"] == 3
    assert config["clauses"] == 8
    assert config["difficulty"] == 2
    assert config["language"] == "sv"
    assert config["package"] is True
    assert config["variation_overrides"] == {}
    assert config["prompt_version"]
    assert config["prompt_sha256"]
    for record in config["scenario_definitions"]:
        assert record["contract_type"]
        assert len(record["sha256"]) == 64
    assert config["runtime"]["python_version"]
    assert any(
        record["contract_type"] == "consulting" for record in config["scenario_definitions"]
    )


def test_report_records_question_rejections(benchmark_run) -> None:
    _run_dir, payload = benchmark_run
    details = _details(payload)
    stats = payload["stats"]

    assert "questions_accepted" in details
    assert "questions_rejected" in details
    assert details["questions_accepted"] == stats["questions_total"]
    assert details["questions_rejected"] == stats["questions_rejected"]
    assert details["questions_rejected"] == len(details["rejected_question_ids"])
    # Rejected questions are never exported as validated facit.
    assert stats["questions_validated"] == stats["questions_total"]


def test_report_includes_validation_report(benchmark_run) -> None:
    _run_dir, payload = benchmark_run
    validation = _details(payload)["validation"]

    assert validation is not None, "benchmarkrapporten måste bära datasetvalideringen"
    assert validation["status"] == "PASS"
    assert validation["contracts_checked"] == 3


def test_report_latency_is_weighted_over_all_calls(benchmark_run) -> None:
    _run_dir, payload = benchmark_run
    latency = _details(payload)["latency"]
    stats = payload["stats"]

    assert latency["basis"] == "weighted_per_call"
    assert latency["calls"] == stats["calls_total"]
    if latency["calls"]:
        assert latency["mean_latency_ms"] == pytest.approx(
            latency["total_latency_ms"] / latency["calls"]
        )
    # The stats field uses the same weighted definition.
    if stats["mean_document_latency_ms"] is not None:
        assert stats["mean_document_latency_ms"] == pytest.approx(latency["mean_latency_ms"])


def test_report_statistics_are_arithmetically_consistent(benchmark_run) -> None:
    _run_dir, payload = benchmark_run
    stats = payload["stats"]
    details = _details(payload)

    assert stats["contracts_accepted"] + stats["contracts_rejected"] == stats["contracts_total"]
    assert stats["documents_accepted"] + stats["documents_rejected"] == stats["documents_total"]
    assert stats["calls_total"] == details["latency"]["calls"]
    if details["latency"]["calls"]:
        assert stats["mean_document_latency_ms"] is not None
    assert stats["verification_pass_rate"] == pytest.approx(
        stats["documents_accepted"] / stats["documents_total"]
    )
    assert stats["repair_rate"] == pytest.approx(
        stats["documents_repaired"] / stats["documents_total"]
    )
    assert stats["rejection_rate"] == pytest.approx(
        stats["contracts_rejected"] / stats["contracts_total"]
    )
    if stats["total_seconds"]:
        assert stats["documents_per_minute"] == pytest.approx(
            stats["documents_total"] / (stats["total_seconds"] / 60.0)
        )
    assert payload["provider_is_mock"] is True


def test_report_notes_mock_runs_are_not_model_quality(benchmark_run) -> None:
    _run_dir, payload = benchmark_run

    assert payload["provider_is_mock"] is True
    assert any("mock" in note.lower() for note in payload["notes"])
