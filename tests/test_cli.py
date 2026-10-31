from __future__ import annotations

import json

from contractforge.cli.main import main


def _run_json(capsys, argv: list[str]) -> tuple[int, dict]:
    code = main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else {}
    return code, payload


def test_generate_validate_stats_export(tmp_path, capsys):
    dataset = tmp_path / "dataset"
    code, payload = _run_json(
        capsys,
        [
            "generate",
            "--type",
            "consulting",
            "--count",
            "2",
            "--clauses",
            "8",
            "--difficulty",
            "2",
            "--provider",
            "mock",
            "--seed",
            "5",
            "--concurrency",
            "2",
            "--output",
            str(dataset),
            "--json",
        ],
    )
    assert code == 0
    assert payload["status"] == "ok"
    assert payload["stats"]["contracts_total"] == 2
    assert payload["stats"]["contracts_accepted"] >= 1

    code, payload = _run_json(capsys, ["validate", str(dataset), "--json"])
    assert code == 0, payload
    assert payload["report"]["status"] == "PASS"

    code, payload = _run_json(capsys, ["stats", str(dataset), "--json"])
    assert code == 0
    assert payload["stats"]["documents_total"] >= 1

    code, payload = _run_json(
        capsys, ["export", str(dataset), "--format", "zip", "--json"]
    )
    assert code == 0
    assert payload["path"].endswith(".zip")

    code, payload = _run_json(
        capsys, ["export", str(dataset), "--format", "jsonl", "--json"]
    )
    assert code == 0
    jsonl_path = dataset.with_suffix(".jsonl")
    assert jsonl_path.exists()
    assert payload["path"] == str(jsonl_path)


def test_list_types(tmp_path, capsys):
    code, payload = _run_json(capsys, ["list-types", "--json"])
    assert code == 0
    assert len(payload["types"]) == 10
    assert "consulting" in payload["types"]


def test_generate_questions(tmp_path, capsys):
    dataset = tmp_path / "dataset"
    code, _ = _run_json(
        capsys,
        [
            "generate", "--type", "nda", "--count", "1", "--clauses", "6",
            "--difficulty", "1", "--provider", "mock", "--seed", "9",
            "--output", str(dataset), "--json",
        ],
    )
    assert code == 0
    code, payload = _run_json(capsys, ["generate-questions", str(dataset), "--json"])
    assert code == 0
    assert payload["stats"]["questions_total"] >= 1


def test_missing_provider_key_is_machine_readable(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    monkeypatch.setenv("CONTRACTFORGE_PROVIDER", "cerebras")
    code, payload = _run_json(
        capsys,
        [
            "generate", "--type", "consulting", "--count", "1",
            "--output", str(tmp_path / "d"), "--json",
        ],
    )
    assert code == 5
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "config_error"


def test_usage_error_exit_code(capsys):
    code = main(["generate", "--type", "not-a-type", "--output", "/tmp/x", "--json"])
    assert code == 2


def test_invalid_dataset_path(capsys, tmp_path):
    code, payload = _run_json(capsys, ["validate", str(tmp_path / "missing"), "--json"])
    assert code == 2
    assert payload["error"]["type"] == "dataset_not_found"
