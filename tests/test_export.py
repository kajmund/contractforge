"""Tests for dataset export, loading, bundling and statistics."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

import docx
import pytest
from factories import make_consulting_scenario, make_contract_result

from contractforge.errors import ExportError
from contractforge.export import (
    compute_stats,
    export_contract,
    export_jsonl,
    export_zip,
    find_contract_dirs,
    load_contract_dir,
    load_dataset,
    load_dataset_manifest,
    write_dataset_manifest,
    write_rejected_contract,
)
from contractforge.models import (
    FORMAT_EXTENSIONS,
    Answerability,
    ClauseSpec,
    CostSummary,
    DocKind,
    DocumentFormat,
    DocumentSpec,
    DocumentStatus,
    ExpectedAnswer,
    GoldenQuestion,
    Language,
    LLMCallRecord,
    LLMRequest,
    QuestionCategory,
    Usage,
)
from contractforge.utils import sha256_file, sha256_text

ALL_FORMATS = list(DocumentFormat)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(contract_id: str = "contract_0001", seed: int = 42, status=None):
    scenario = make_consulting_scenario()
    if contract_id != "contract_0001":
        scenario = scenario.model_copy(update={"contract_id": contract_id, "seed": seed})
    return make_contract_result(scenario=scenario, status=status or DocumentStatus.ACCEPTED)


def _dataset_config(result):
    return result.config.model_copy(update={"formats": list(ALL_FORMATS)})


def _export_dataset(tmp_path: Path, results) -> Path:
    root = tmp_path / "dataset"
    for result in results:
        export_contract(result, root, formats=ALL_FORMATS)
    return root


def _appendix_scenario():
    scenario = make_consulting_scenario()
    scenario.documents.append(
        DocumentSpec(
            document_id="appendix_a",
            kind=DocKind.APPENDIX,
            title_sv="Bilaga A",
            title_en="Appendix A",
            filename_stem="appendix_a",
            clauses=[
                ClauseSpec(
                    clause_id="appendix_clause_01",
                    document_id="appendix_a",
                    order=1,
                    heading_sv="Övriga villkor",
                )
            ],
        )
    )
    return scenario


def _manifest(contract_dir: Path) -> dict:
    return json.loads((contract_dir / "manifest.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# export_contract layout + documents
# ---------------------------------------------------------------------------


def test_export_contract_creates_dataset_layout(tmp_path: Path) -> None:
    result = _result()
    contract_dir = export_contract(
        result, tmp_path / "dataset", formats=ALL_FORMATS
    )

    assert contract_dir == tmp_path / "dataset" / "contract_0001"
    expected_documents = {
        f"main_agreement{extension}" for extension in FORMAT_EXTENSIONS.values()
    }
    expected = expected_documents | {
        "scenario.json",
        "gold_facts.json",
        "golden_questions.json",
        "verification.json",
        "rendered.json",
        "llm_calls.json",
        "manifest.json",
    }
    actual = {
        path.relative_to(contract_dir).as_posix()
        for path in contract_dir.rglob("*")
        if path.is_file()
    }
    assert actual == expected
    assert not (contract_dir / "raw").exists()


def test_export_contract_with_appendix_writes_both_documents(tmp_path: Path) -> None:
    scenario = _appendix_scenario()
    result = make_contract_result(scenario=scenario)
    contract_dir = export_contract(result, tmp_path / "dataset", formats=ALL_FORMATS)

    directory_names = {
        path.relative_to(contract_dir).as_posix()
        for path in contract_dir.rglob("*")
        if path.is_file()
    }
    for document_id in ("main_agreement", "appendix_a"):
        for extension in FORMAT_EXTENSIONS.values():
            assert f"{document_id}{extension}" in directory_names

    manifest = _manifest(contract_dir)
    assert [record["document_id"] for record in manifest["documents"]] == [
        "main_agreement",
        "appendix_a",
    ]
    assert all(len(record["files"]) == 4 for record in manifest["documents"])

    loaded = load_contract_dir(contract_dir)
    assert sorted(loaded.texts) == ["appendix_a", "main_agreement"]
    assert loaded.texts["appendix_a"] == result.rendered["appendix_a"].canonical_text


def test_export_contract_always_writes_txt(tmp_path: Path) -> None:
    result = _result()
    contract_dir = export_contract(
        result, tmp_path / "dataset", formats=[DocumentFormat.MARKDOWN]
    )
    assert (contract_dir / "main_agreement.txt").is_file()
    assert (contract_dir / "main_agreement.md").is_file()
    assert not (contract_dir / "main_agreement.pdf").exists()
    assert not (contract_dir / "main_agreement.docx").exists()


def test_unknown_format_raises_export_error(tmp_path: Path) -> None:
    with pytest.raises(ExportError):
        export_contract(_result(), tmp_path / "dataset", formats=["bogus"])


def test_txt_is_byte_identical_to_canonical_text(tmp_path: Path) -> None:
    result = _result()
    contract_dir = export_contract(result, tmp_path / "dataset", formats=ALL_FORMATS)
    canonical = result.rendered["main_agreement"].canonical_text
    assert (contract_dir / "main_agreement.txt").read_bytes() == canonical.encode("utf-8")


def test_all_formats_are_readable_from_disk(tmp_path: Path) -> None:
    contract_dir = export_contract(_result(), tmp_path / "dataset", formats=ALL_FORMATS)

    word = docx.Document(contract_dir / "main_agreement.docx")
    paragraphs = "\n".join(paragraph.text for paragraph in word.paragraphs)
    assert "Konsultavtal" in paragraphs
    assert "Parterna är överens om följande: 950 SEK." in paragraphs

    assert (contract_dir / "main_agreement.pdf").read_bytes().startswith(b"%PDF")
    markdown = (contract_dir / "main_agreement.md").read_text(encoding="utf-8")
    assert markdown.startswith("# Konsultavtal")
    assert "## 2. Ersättning" in markdown


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_hashes_every_artifact_and_is_sorted(tmp_path: Path) -> None:
    result = _result()
    contract_dir = export_contract(result, tmp_path / "dataset", formats=ALL_FORMATS)
    manifest = _manifest(contract_dir)

    files = sorted(
        path.relative_to(contract_dir).as_posix()
        for path in contract_dir.rglob("*")
        if path.is_file()
    )
    artifact_paths = [artifact["path"] for artifact in manifest["artifacts"]]

    assert artifact_paths == sorted(artifact_paths)
    assert set(artifact_paths) == set(files) - {"manifest.json"}
    assert "manifest.json" not in artifact_paths
    for artifact in manifest["artifacts"]:
        path = contract_dir / artifact["path"]
        assert artifact["sha256"] == sha256_file(path)
        assert artifact["size_bytes"] == path.stat().st_size

    canonical = result.rendered["main_agreement"].canonical_text
    record = manifest["documents"][0]
    assert record["document_id"] == "main_agreement"
    assert record["kind"] == "main_agreement"
    assert record["canonical_sha256"] == sha256_text(canonical)
    assert record["canonical_sha256"] == sha256_file(contract_dir / "main_agreement.txt")
    assert {file["path"] for file in record["files"]} == {
        f"main_agreement{extension}" for extension in FORMAT_EXTENSIONS.values()
    }
    assert all(file["kind"] == "document" for file in record["files"])


def test_manifest_metadata(tmp_path: Path) -> None:
    result = _result()
    contract_dir = export_contract(result, tmp_path / "dataset", formats=ALL_FORMATS)
    manifest = _manifest(contract_dir)

    assert manifest["synthetic"] is True
    assert manifest["contract_id"] == "contract_0001"
    assert manifest["contract_type"] == "consulting"
    assert manifest["difficulty"] == 1
    assert manifest["language"] == "sv"
    assert manifest["jurisdiction"] == "SE"
    assert manifest["provider"] == "mock"
    assert manifest["model"] == "mock-1"
    assert manifest["prompt_version"] == "v1"
    assert manifest["prompt_sha256"] == "0" * 64
    assert manifest["seed"] == 42
    assert manifest["generation_config"]["provider"] == "mock"
    assert manifest["generation_config"]["formats"] == [fmt.value for fmt in ALL_FORMATS]
    assert manifest["verification"]["status"] == "ACCEPTED"
    assert manifest["verification"]["documents_total"] == 1
    assert manifest["verification"]["documents_accepted"] == 1
    assert manifest["cost"]["total_tokens"] == 300
    assert manifest["contractforge_version"]
    generated_at = datetime.fromisoformat(manifest["generated_at"].replace("Z", "+00:00"))
    assert generated_at == result.created_at


def test_export_contract_is_deterministic(tmp_path: Path) -> None:
    result = _result()
    left = export_contract(result, tmp_path / "left", formats=ALL_FORMATS)
    right = export_contract(result, tmp_path / "right", formats=ALL_FORMATS)

    left_hashes = {item["path"]: item["sha256"] for item in _manifest(left)["artifacts"]}
    right_hashes = {item["path"]: item["sha256"] for item in _manifest(right)["artifacts"]}
    assert left_hashes == right_hashes

    for name in ("main_agreement.docx", "main_agreement.pdf", "main_agreement.txt"):
        assert (left / name).read_bytes() == (right / name).read_bytes()


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def test_export_contract_refuses_rejected_contract(tmp_path: Path) -> None:
    result = _result(status=DocumentStatus.REJECTED)
    with pytest.raises(ExportError):
        export_contract(result, tmp_path / "dataset", formats=ALL_FORMATS)
    assert not (tmp_path / "dataset" / result.contract_id).exists()


def test_write_rejected_contract_never_writes_an_accepted_contract(tmp_path: Path) -> None:
    root = tmp_path / "dataset"
    rejected = _result(status=DocumentStatus.REJECTED)

    path = write_rejected_contract(rejected, root)

    assert path == root / "rejected" / f"{rejected.contract_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "REJECTED"
    assert payload["source_status"] == "REJECTED"
    assert payload["scenario"]["contract_id"] == rejected.contract_id
    assert payload["verification"]["status"] == "REJECTED"
    assert not (root / rejected.contract_id).exists()
    assert find_contract_dirs(root) == []

    # Even a stale ACCEPTED status cannot make a rejection look accepted.
    accepted = _result()
    forced = write_rejected_contract(accepted, tmp_path / "forced")
    forced_payload = json.loads(forced.read_text(encoding="utf-8"))
    assert forced_payload["status"] == "REJECTED"
    assert forced_payload["source_status"] == "ACCEPTED"
    assert find_contract_dirs(tmp_path / "forced") == []


# ---------------------------------------------------------------------------
# LLM call sanitization
# ---------------------------------------------------------------------------


def _result_with_call() -> tuple:
    result = _result()
    request = LLMRequest(
        provider="mock",
        model="mock-1",
        prompt_version="v1",
        system="system",
        user="user",
        metadata={"api_key": "super-secret-key", "scenario_id": "contract_0001"},
    )
    call = LLMCallRecord(
        call_id="call_0001",
        purpose="generate",
        contract_id=result.contract_id,
        request=request,
        response_text="{}",
        usage=Usage(input_tokens=1, output_tokens=2, total_tokens=3),
        latency_ms=5.0,
        provider="mock",
        model="mock-1",
        raw={
            "api_key": "super-secret-key",
            "authorization": "Bearer sk-abcdefghijklmnopqrstuvwxyz",
            "note": "Bearer sk-abcdefghijklmnopqrstuvwxyz",
            "model": "mock-1",
        },
    )
    return result.model_copy(update={"llm_calls": [call]}), call


def test_llm_calls_are_sanitized_and_raw_is_separate(tmp_path: Path) -> None:
    result, call = _result_with_call()
    contract_dir = export_contract(result, tmp_path / "dataset", formats=ALL_FORMATS)

    calls = json.loads((contract_dir / "llm_calls.json").read_text(encoding="utf-8"))
    assert len(calls) == 1
    assert calls[0]["call_id"] == "call_0001"
    assert "raw" not in calls[0]
    calls_blob = json.dumps(calls, ensure_ascii=False)
    assert "super-secret-key" not in calls_blob
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in calls_blob
    assert "api_key" not in json.dumps(calls[0]["request"]["metadata"])

    raw_path = contract_dir / "raw" / f"{call.call_id}.json"
    assert raw_path.is_file()
    raw_text = raw_path.read_text(encoding="utf-8")
    assert "super-secret-key" not in raw_text
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in raw_text
    assert "sk-" not in raw_text
    assert "[REDACTED]" in raw_text

    manifest = _manifest(contract_dir)
    artifact_kinds = {item["path"]: item["kind"] for item in manifest["artifacts"]}
    assert artifact_kinds["raw/call_0001.json"] == "raw_response"
    assert artifact_kinds["llm_calls.json"] == "llm_calls"


def test_no_raw_directory_without_raw_responses(tmp_path: Path) -> None:
    contract_dir = export_contract(_result(), tmp_path / "dataset", formats=ALL_FORMATS)
    assert not (contract_dir / "raw").exists()
    calls = json.loads((contract_dir / "llm_calls.json").read_text(encoding="utf-8"))
    assert calls == []


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def test_loader_round_trip(tmp_path: Path) -> None:
    result = _result()
    root = _export_dataset(tmp_path, [result])

    loaded = load_dataset(root)

    assert len(loaded) == 1
    contract = loaded[0]
    assert contract.contract_id == "contract_0001"
    assert contract.root == root / "contract_0001"
    assert contract.load_errors == []
    assert contract.scenario == result.scenario
    assert contract.manifest.generation_config == _dataset_config(result)
    assert contract.gold is not None
    assert contract.gold.contract_id == result.contract_id
    assert [question.question_id for question in contract.questions] == ["q_001"]
    assert contract.verification is not None
    assert contract.verification.status == DocumentStatus.ACCEPTED
    assert contract.rendered["main_agreement"].canonical_text == (
        result.rendered["main_agreement"].canonical_text
    )
    assert contract.rendered["main_agreement"].clause_spans == (
        result.rendered["main_agreement"].clause_spans
    )
    assert contract.texts["main_agreement"] == result.rendered["main_agreement"].canonical_text


def test_loader_records_missing_optional_files(tmp_path: Path) -> None:
    root = _export_dataset(tmp_path, [_result()])
    contract_dir = root / "contract_0001"
    (contract_dir / "gold_facts.json").unlink()
    (contract_dir / "rendered.json").unlink()

    contract = load_contract_dir(contract_dir)

    assert contract.gold is None
    assert contract.rendered == {}
    assert any("gold_facts.json" in error for error in contract.load_errors)
    assert any("rendered.json" in error for error in contract.load_errors)
    # The canonical text is still readable through the manifest artifact list.
    assert contract.texts["main_agreement"]
    assert contract.scenario.contract_id == "contract_0001"


def test_loader_corrupt_optional_files_are_load_errors(tmp_path: Path) -> None:
    root = _export_dataset(tmp_path, [_result()])
    contract_dir = root / "contract_0001"
    (contract_dir / "gold_facts.json").write_text("{not json", encoding="utf-8")
    (contract_dir / "golden_questions.json").write_text('{"not": "a list"}', encoding="utf-8")

    contract = load_contract_dir(contract_dir)

    assert contract.gold is None
    assert contract.questions == []
    assert any("gold_facts.json" in error for error in contract.load_errors)
    assert any("golden_questions.json" in error for error in contract.load_errors)


def test_loader_raises_on_missing_or_corrupt_mandatory_files(tmp_path: Path) -> None:
    root = _export_dataset(tmp_path, [_result()])
    contract_dir = root / "contract_0001"
    scenario_payload = (contract_dir / "scenario.json").read_text(encoding="utf-8")

    (contract_dir / "scenario.json").unlink()
    with pytest.raises(ExportError):
        load_contract_dir(contract_dir)
    (contract_dir / "scenario.json").write_text(scenario_payload, encoding="utf-8")

    (contract_dir / "scenario.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ExportError):
        load_contract_dir(contract_dir)
    (contract_dir / "scenario.json").write_text(scenario_payload, encoding="utf-8")

    (contract_dir / "manifest.json").unlink()
    with pytest.raises(ExportError):
        load_contract_dir(contract_dir)


def test_load_dataset_and_contract_dir_raise_for_missing_paths(tmp_path: Path) -> None:
    with pytest.raises(ExportError):
        load_dataset(tmp_path / "missing")
    with pytest.raises(ExportError):
        load_contract_dir(tmp_path / "missing")


def test_find_contract_dirs_excludes_rejected(tmp_path: Path) -> None:
    root = _export_dataset(tmp_path, [_result()])
    write_rejected_contract(_result(status=DocumentStatus.REJECTED), root)

    directories = find_contract_dirs(root)

    assert [directory.name for directory in directories] == ["contract_0001"]
    assert "rejected" not in [directory.name for directory in directories]


# ---------------------------------------------------------------------------
# Dataset manifest
# ---------------------------------------------------------------------------


def test_dataset_manifest_round_trip(tmp_path: Path) -> None:
    results = [_result(), _result(contract_id="contract_0002", seed=43)]
    root = _export_dataset(tmp_path, results)

    assert load_dataset_manifest(root) is None

    loaded = load_dataset(root)
    stats = compute_stats(loaded, total_seconds=120)
    path = write_dataset_manifest(root, _dataset_config(results[0]), loaded, stats)

    assert path == root / "dataset_manifest.json"
    dataset_manifest = load_dataset_manifest(root)
    assert dataset_manifest is not None
    assert [record.contract_id for record in dataset_manifest.contracts] == [
        "contract_0001",
        "contract_0002",
    ]
    for record in dataset_manifest.contracts:
        assert record.status == DocumentStatus.ACCEPTED
        assert record.manifest_sha256 == sha256_file(root / record.path / "manifest.json")
    assert dataset_manifest.config == _dataset_config(results[0])
    assert dataset_manifest.totals == stats
    assert dataset_manifest.generated_at == stats.generated_at
    assert dataset_manifest.synthetic is True

    stats_payload = json.loads((root / "stats.json").read_text(encoding="utf-8"))
    assert stats_payload["contracts_total"] == 2
    assert stats_payload["documents_total"] == 2


# ---------------------------------------------------------------------------
# ZIP + JSONL bundles
# ---------------------------------------------------------------------------


def _prepared_dataset(tmp_path: Path, results) -> Path:
    root = _export_dataset(tmp_path, results)
    loaded = load_dataset(root)
    write_dataset_manifest(root, _dataset_config(results[0]), loaded, compute_stats(loaded))
    return root


def test_export_zip_contains_expected_files_and_is_deterministic(tmp_path: Path) -> None:
    root = _prepared_dataset(
        tmp_path, [_result(), _result(contract_id="contract_0002", seed=43)]
    )

    first = export_zip(root, tmp_path / "first.zip")
    second = export_zip(root, tmp_path / "second.zip")

    assert first.is_file()
    assert first.read_bytes() == second.read_bytes()
    names = zipfile.ZipFile(first).namelist()
    assert names == sorted(names)
    assert "dataset_manifest.json" in names
    assert "stats.json" in names
    assert "contract_0001/main_agreement.txt" in names
    assert "contract_0001/manifest.json" in names
    assert "contract_0002/main_agreement.pdf" in names
    assert all(not name.startswith("/") for name in names)


def test_export_zip_requires_dataset_manifest(tmp_path: Path) -> None:
    root = _export_dataset(tmp_path, [_result()])
    with pytest.raises(ExportError):
        export_zip(root, tmp_path / "out.zip")


def test_export_zip_output_inside_dataset_is_excluded(tmp_path: Path) -> None:
    root = _prepared_dataset(tmp_path, [_result()])
    inner = export_zip(root, root / "bundle.zip")
    names = zipfile.ZipFile(inner).namelist()
    assert "bundle.zip" not in names
    assert "dataset_manifest.json" in names


def test_export_jsonl_one_line_per_contract(tmp_path: Path) -> None:
    results = [_result(), _result(contract_id="contract_0002", seed=43)]
    root = _prepared_dataset(tmp_path, results)

    path = export_jsonl(root, tmp_path / "dataset.jsonl")
    assert export_jsonl(root, tmp_path / "dataset-again.jsonl").read_bytes() == path.read_bytes()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert [record["contract_id"] for record in records] == [
        "contract_0001",
        "contract_0002",
    ]
    for record, result in zip(records, results, strict=True):
        assert record["status"] == "ACCEPTED"
        assert record["dataset_status"] == "ACCEPTED"
        assert record["scenario"]["contract_id"] == result.contract_id
        assert record["gold"]["contract_id"] == result.contract_id
        assert len(record["questions"]) == 1
        assert record["manifest"]["contract_id"] == result.contract_id
        assert record["manifest_sha256"] == sha256_file(
            root / record["path"] / "manifest.json"
        )


def test_export_jsonl_without_dataset_manifest(tmp_path: Path) -> None:
    root = _export_dataset(tmp_path, [_result()])
    lines = export_jsonl(root, tmp_path / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["dataset_status"] is None


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def test_stats_empty_dataset() -> None:
    stats = compute_stats([])

    assert stats.contracts_total == 0
    assert stats.contracts_accepted == 0
    assert stats.contracts_rejected == 0
    assert stats.documents_total == 0
    assert stats.clauses_total == 0
    assert stats.facts_total == 0
    assert stats.questions_total == 0
    assert stats.calls_total == 0
    assert stats.cost_usd is None
    assert stats.cost_basis == "unknown"
    assert stats.total_seconds is None
    assert stats.documents_per_minute is None
    assert stats.clauses_per_minute is None
    assert stats.mean_document_latency_ms is None
    assert stats.cost_per_accepted_document_usd is None
    assert stats.cost_per_validated_question_usd is None
    assert stats.verification_pass_rate == 0.0
    assert stats.repair_rate == 0.0
    assert stats.rejection_rate == 0.0

    with_seconds = compute_stats([], total_seconds=30)
    assert with_seconds.total_seconds == 30
    assert with_seconds.documents_per_minute is None
    assert with_seconds.clauses_per_minute is None


def test_stats_counts_and_throughput(tmp_path: Path) -> None:
    results = [_result(), _result(contract_id="contract_0002", seed=43)]
    loaded = load_dataset(_export_dataset(tmp_path, results))

    stats = compute_stats(loaded, total_seconds=120)

    assert stats.contracts_total == 2
    assert stats.contracts_accepted == 2
    assert stats.documents_total == 2
    assert stats.documents_accepted == 2
    assert stats.clauses_total == 10
    assert stats.facts_total == 0
    assert stats.questions_total == 2
    assert stats.questions_validated == 2
    assert stats.questions_answerable == 2
    assert stats.calls_total == 2
    assert (stats.input_tokens, stats.output_tokens, stats.total_tokens) == (200, 400, 600)
    assert stats.tokens_known is True
    assert stats.cost_usd is None
    assert stats.cost_basis == "unknown"
    assert stats.cost_per_accepted_document_usd is None
    assert stats.cost_per_validated_question_usd is None
    assert stats.documents_per_minute == pytest.approx(1.0)
    assert stats.clauses_per_minute == pytest.approx(5.0)
    assert stats.verification_pass_rate == 1.0
    assert stats.repair_rate == 0.0
    assert stats.rejection_rate == 0.0


def test_stats_without_total_seconds_has_no_throughput(tmp_path: Path) -> None:
    loaded = load_dataset(_export_dataset(tmp_path, [_result()]))
    stats = compute_stats(loaded)
    assert stats.total_seconds is None
    assert stats.documents_per_minute is None
    assert stats.clauses_per_minute is None


def test_stats_unknown_cost_is_none(tmp_path: Path) -> None:
    loaded = load_dataset(_export_dataset(tmp_path, [_result()]))
    stats = compute_stats(loaded, total_seconds=60)
    assert stats.cost_usd is None
    assert stats.cost_basis == "unknown"


def test_stats_known_cost_and_derived_costs(tmp_path: Path) -> None:
    result = _result()
    result = result.model_copy(
        update={
            "cost": CostSummary(
                calls=3,
                input_tokens=10,
                output_tokens=20,
                total_tokens=30,
                cost_usd=0.5,
                cost_basis="configured_price_table",
                mean_latency_ms=125.0,
            )
        }
    )
    loaded = load_dataset(_export_dataset(tmp_path, [result]))

    stats = compute_stats(loaded, total_seconds=60)

    assert stats.calls_total == 3
    assert (stats.input_tokens, stats.output_tokens, stats.total_tokens) == (10, 20, 30)
    assert stats.cost_usd == pytest.approx(0.5)
    assert stats.cost_basis == "configured_price_table"
    assert stats.mean_document_latency_ms == pytest.approx(125.0)
    assert stats.cost_per_accepted_document_usd == pytest.approx(0.5)
    assert stats.cost_per_validated_question_usd == pytest.approx(0.5)
    assert stats.documents_per_minute == pytest.approx(1.0)


def test_stats_latency_is_weighted_by_call_count(tmp_path: Path) -> None:
    """mean_document_latency_ms is sum(mean * calls) / sum(calls), not a mean of means."""

    few = _result().model_copy(
        update={
            "cost": CostSummary(calls=2, mean_latency_ms=1000.0, tokens_known=False),
        }
    )
    many = _result(contract_id="contract_0002", seed=43).model_copy(
        update={
            "cost": CostSummary(calls=10, mean_latency_ms=100.0, tokens_known=False),
        }
    )
    loaded = load_dataset(_export_dataset(tmp_path, [few, many]))

    stats = compute_stats(loaded)

    # Naive mean of means would be 550.0; the weighted mean is
    # (2*1000 + 10*100) / 12 = 250.0.
    assert stats.calls_total == 12
    assert stats.mean_document_latency_ms == pytest.approx(250.0)


def test_stats_rejected_questions_are_counted(tmp_path: Path) -> None:
    loaded = load_dataset(_export_dataset(tmp_path, [_result()]))

    stats = compute_stats(loaded)

    assert stats.questions_rejected == 0
    assert stats.questions_total == stats.questions_validated


def test_stats_partial_unknown_cost_is_none(tmp_path: Path) -> None:
    known = _result().model_copy(
        update={
            "cost": CostSummary(
                calls=1,
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                cost_usd=0.5,
                cost_basis="configured_price_table",
            )
        }
    )
    unknown = _result(contract_id="contract_0002", seed=43)
    loaded = load_dataset(_export_dataset(tmp_path, [known, unknown]))

    stats = compute_stats(loaded)

    assert stats.cost_usd is None
    assert stats.cost_basis == "unknown"
    assert stats.cost_per_accepted_document_usd is None
    assert stats.cost_per_validated_question_usd is None


def test_stats_question_answerability_breakdown(tmp_path: Path) -> None:
    result = _result()
    base = result.questions[0]
    questions = [
        base,
        GoldenQuestion(
            question_id="q_002",
            question="Finns uppgiften?",
            language=Language.SV,
            category=QuestionCategory.ABSENCE,
            difficulty=1,
            expected_answer=ExpectedAnswer(
                text="Informationen saknas i avtalet."
            ),
            required_facts=["fact_hourly_rate"],
            answerability=Answerability.INSUFFICIENT_EVIDENCE,
        ),
        GoldenQuestion(
            question_id="q_003",
            question="Villkorligt svar?",
            language=Language.SV,
            category=QuestionCategory.CONDITIONAL,
            difficulty=1,
            expected_answer=ExpectedAnswer(text="Om villkoret uppfylls."),
            required_facts=["fact_extension_period"],
            answerability=Answerability.CONDITIONAL,
        ),
        GoldenQuestion(
            question_id="q_004",
            question="Motstridiga uppgifter?",
            language=Language.SV,
            category=QuestionCategory.CONTRADICTION,
            difficulty=1,
            expected_answer=ExpectedAnswer(text="Uppgifterna motsäger varandra."),
            required_facts=["fact_liability_cap"],
            answerability=Answerability.CONFLICTING_EVIDENCE,
        ),
    ]
    result = result.model_copy(update={"questions": questions})
    loaded = load_dataset(_export_dataset(tmp_path, [result]))

    stats = compute_stats(loaded)

    assert stats.questions_total == 4
    assert stats.questions_validated == 1
    assert stats.questions_answerable == 1
    assert stats.questions_insufficient_evidence == 1
    assert stats.questions_conditional == 1
    assert stats.questions_conflicting_evidence == 1


def test_stats_counts_documents_and_clauses_for_multi_document_package(
    tmp_path: Path,
) -> None:
    result = make_contract_result(scenario=_appendix_scenario())
    loaded = load_dataset(_export_dataset(tmp_path, [result]))

    stats = compute_stats(loaded, total_seconds=60)

    assert stats.documents_total == 2
    assert stats.documents_accepted == 2
    assert stats.clauses_total == 6
    assert stats.documents_per_minute == pytest.approx(2.0)
    assert stats.clauses_per_minute == pytest.approx(6.0)


def test_stats_repaired_and_rejected_rates(tmp_path: Path) -> None:
    repaired = _result(status=DocumentStatus.REPAIRED)
    loaded = load_dataset(_export_dataset(tmp_path, [repaired]))

    stats = compute_stats(loaded)
    # A repaired contract was exported and accepted; only its documents were repaired.
    assert stats.contracts_total == 1
    assert stats.contracts_accepted == 1
    assert stats.contracts_rejected == 0
    assert stats.documents_accepted == 0
    assert stats.documents_repaired == 1
    assert stats.verification_pass_rate == 0.0
    assert stats.repair_rate == 1.0
    assert stats.rejection_rate == 0.0

    # A rejection recorded in a dataset manifest is reflected in the rate.
    loaded[0].manifest.verification.status = DocumentStatus.REJECTED
    rejected_stats = compute_stats(loaded)
    assert rejected_stats.contracts_total == 1
    assert rejected_stats.contracts_accepted == 0
    assert rejected_stats.contracts_rejected == 1
    assert rejected_stats.rejection_rate == 1.0


def test_loader_missing_canonical_text_is_a_load_error(tmp_path: Path) -> None:
    root = _export_dataset(tmp_path, [_result()])
    (root / "contract_0001" / "main_agreement.txt").unlink()

    contract = load_contract_dir(root / "contract_0001")

    assert "main_agreement" not in contract.texts
    assert any("main_agreement.txt" in error for error in contract.load_errors)
    # The rest of the contract is still readable and honestly reported.
    assert contract.rendered["main_agreement"].canonical_text
    assert contract.scenario.contract_id == "contract_0001"


def test_zip_carries_versioned_dataset_manifest(tmp_path: Path) -> None:
    root = _prepared_dataset(tmp_path, [_result()])
    archive = zipfile.ZipFile(export_zip(root, tmp_path / "out.zip"))
    payload = json.loads(archive.read("dataset_manifest.json"))

    assert payload["schema_version"] == "1.0"
    assert payload["contractforge_version"]
    assert payload["synthetic"] is True
    assert [record["contract_id"] for record in payload["contracts"]] == ["contract_0001"]
    assert payload["totals"]["contracts_total"] == 1


def test_write_dataset_manifest_requires_contract_manifest(tmp_path: Path) -> None:
    root = _export_dataset(tmp_path, [_result()])
    loaded = load_dataset(root)
    (root / "contract_0001" / "manifest.json").unlink()

    with pytest.raises(ExportError):
        write_dataset_manifest(
            root,
            loaded[0].manifest.generation_config,
            loaded,
            compute_stats(loaded),
        )


# ---------------------------------------------------------------------------
# F8: only facts the scenario declares *missing* may lack gold evidence
# ---------------------------------------------------------------------------


def _difficulty6_dataset(tmp_path: Path) -> Path:
    """A real level-6 dataset (mock provider): ambiguity + missing information."""

    from contractforge.config import Settings
    from contractforge.models import ContractType, Language, ScenarioRequest
    from contractforge.pipeline import generate_dataset

    settings = Settings.from_env().with_overrides(provider="mock", formats=[DocumentFormat.TXT])
    root = tmp_path / "level6"
    generate_dataset(
        settings,
        ScenarioRequest(
            contract_type=ContractType.CONSULTING,
            count=1,
            clauses=10,
            difficulty=6,
            language=Language.SV,
            seed=77,
        ),
        root,
        progress=lambda _message: None,
    )
    return root


def test_level6_conditional_fact_has_evidence_and_is_verifiable(tmp_path: Path) -> None:
    """A CONDITIONAL (ambiguity) fact is expressed in the text, so gold must cite it."""

    from contractforge.export.loader import load_dataset

    root = _difficulty6_dataset(tmp_path)
    (loaded,) = load_dataset(root)
    conditional_ids = {
        fact_id
        for anomaly in loaded.scenario.anomalies
        if anomaly.kind.value == "ambiguity"
        for fact_id in anomaly.fact_ids
    }
    assert conditional_ids, "nivå 6 måste ha en ambiguity-anomali"

    gold_facts = {fact.fact_id: fact for fact in loaded.gold.facts}
    for fact_id in conditional_ids:
        gold_fact = gold_facts[fact_id]
        assert gold_fact.evidence, f"{fact_id} är uttryckt i texten och måste ha evidens"
        scenario_fact = loaded.scenario.fact(fact_id)
        for span in gold_fact.evidence:
            document = loaded.rendered[span.document_id]
            assert document.canonical_text[span.start : span.end] == span.quote

    # And the value really is expressed in the conditional clause text.
    scenario_fact = loaded.scenario.fact(next(iter(conditional_ids)))
    expressed = [
        scenario_fact.explicit_surfaces[0],
        *scenario_fact.explicit_surfaces[1:],
        *scenario_fact.paraphrase_surfaces,
    ]
    canonical = "\n".join(document.canonical_text for document in loaded.rendered.values())
    assert any(surface.lower() in canonical.lower() for surface in expressed)


def test_gold_fact_without_evidence_fails_unless_declared_missing(tmp_path: Path) -> None:
    """The dataset validator refuses unregulated facts; only missing_information is exempt."""

    from contractforge.export.loader import find_contract_dirs
    from contractforge.utils import atomic_write_json, read_json, sha256_file
    from contractforge.validation.dataset import validate_dataset

    root = _difficulty6_dataset(tmp_path)
    (contract_dir,) = find_contract_dirs(root)
    gold_path = contract_dir / "gold_facts.json"
    gold = read_json(gold_path)

    exempt = {
        fact_id
        for anomaly in read_json(contract_dir / "scenario.json")["anomalies"]
        if anomaly["kind"] == "missing_information"
        for fact_id in anomaly["fact_ids"]
    }
    candidate = next(
        fact for fact in gold["facts"] if fact["evidence"] and fact["fact_id"] not in exempt
    )
    fact_id = candidate["fact_id"]
    candidate["evidence"] = []
    atomic_write_json(gold_path, gold)

    manifest_path = contract_dir / "manifest.json"
    manifest = read_json(manifest_path)
    for artifact in manifest["artifacts"]:
        path = contract_dir / artifact["path"]
        artifact["sha256"] = sha256_file(path)
        artifact["size_bytes"] = path.stat().st_size
    atomic_write_json(manifest_path, manifest)
    dataset_manifest_path = root / "dataset_manifest.json"
    dataset_manifest = read_json(dataset_manifest_path)
    for record in dataset_manifest["contracts"]:
        record["manifest_sha256"] = sha256_file(manifest_path)
    atomic_write_json(dataset_manifest_path, dataset_manifest)

    report = validate_dataset(root)
    failures = [check for check in report.checks if check.status.value == "FAIL"]
    assert report.status.value == "FAIL"
    assert any(
        check.check_id.endswith("gold.evidence") and fact_id in check.message
        for check in failures
    ), [(check.check_id, check.message) for check in failures]


def test_rejected_contract_keeps_sanitized_calls_cost_and_raw(tmp_path: Path) -> None:
    """A rejected attempt must stay auditable without leaking credentials."""

    from contractforge.export.store import write_rejected_contract
    from contractforge.models import DocumentStatus, LLMCallRecord, LLMRequest, Usage

    secret = "csk-review-secret-1234567890abcdef"
    result = _result(status=DocumentStatus.REJECTED)
    result = result.model_copy(
        update={
            "llm_calls": [
                LLMCallRecord(
                    call_id="contract_0001_main_agreement_a1",
                    purpose="generate_document",
                    contract_id="contract_0001",
                    document_id="main_agreement",
                    request=LLMRequest(
                        provider="cerebras",
                        model="gpt-oss-120b",
                        prompt_version="v1",
                        system="system",
                        user="user",
                    ),
                    response_text='{"broken": true}',
                    usage=Usage(input_tokens=30, output_tokens=60, total_tokens=90),
                    latency_ms=12.0,
                    provider="cerebras",
                    model="gpt-oss-120b",
                    raw={"echo": f"Authorization: Bearer {secret}"},
                )
            ],
        }
    )

    path = write_rejected_contract(result, tmp_path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "REJECTED"
    assert payload["llm_calls"][0]["usage"]["total_tokens"] == 90
    assert "cost" in payload and "config" in payload
    assert "raw" not in payload["llm_calls"][0]
    raw_files = sorted((tmp_path / "rejected" / "raw").glob("*.json"))
    assert raw_files, "raw provider responses must be kept for debugging"
    blob = path.read_text(encoding="utf-8") + "".join(
        raw.read_text(encoding="utf-8") for raw in raw_files
    )
    assert secret not in blob
    assert "[REDACTED]" in blob
