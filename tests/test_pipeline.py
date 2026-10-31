"""End-to-end integration: scenario -> mock generation -> validation -> gold -> export.

These tests exercise the whole vertical chain without network access.
"""

from __future__ import annotations

import json

from contractforge.config import Settings
from contractforge.export.loader import load_dataset
from contractforge.models import (
    Answerability,
    ContractType,
    DocumentFormat,
    Language,
    ScenarioRequest,
)
from contractforge.pipeline import generate_dataset
from contractforge.utils import sha256_file
from contractforge.validation.dataset import validate_dataset


def _settings(**env: str) -> Settings:
    base = {
        "CONTRACTFORGE_PROVIDER": "mock",
        "CONTRACTFORGE_FORMATS": "txt,markdown,docx,pdf",
        "CONTRACTFORGE_CONCURRENCY": "2",
        "CONTRACTFORGE_MAX_REPAIRS": "1",
    }
    base.update(env)
    return Settings.from_env(base)


def _request(**overrides) -> ScenarioRequest:
    payload = {
        "contract_type": ContractType.CONSULTING,
        "count": 2,
        "clauses": 8,
        "difficulty": 2,
        "language": Language.SV,
        "seed": 11,
        "package": True,
    }
    payload.update(overrides)
    return ScenarioRequest(**payload)


def test_offline_end_to_end(tmp_path):
    dataset = tmp_path / "dataset"
    outcome = generate_dataset(_settings(), _request(), dataset)

    assert outcome.stats.contracts_total == 2
    assert outcome.stats.contracts_accepted >= 1
    assert outcome.stats.contracts_accepted + outcome.stats.contracts_rejected == 2
    assert outcome.stats.documents_total >= outcome.stats.contracts_accepted

    contracts = load_dataset(dataset)
    assert len(contracts) == outcome.stats.contracts_accepted
    for contract in contracts:
        assert contract.scenario.contract_id == contract.contract_id
        assert contract.gold is not None
        assert contract.gold.facts, "accepted contracts must have gold facts"
        assert contract.questions, "accepted contracts must have golden questions"
        assert contract.rendered
        for document in contract.scenario.documents:
            document_id = document.document_id
            assert document_id in contract.texts
            assert contract.texts[document_id] == contract.rendered[document_id].canonical_text
            for fmt in (DocumentFormat.TXT, DocumentFormat.DOCX, DocumentFormat.PDF):
                suffix = {"txt": ".txt", "docx": ".docx", "pdf": ".pdf"}[fmt.value]
                path = contract.root / f"{document.filename_stem}{suffix}"
                assert path.exists(), f"missing {path}"
                assert path.stat().st_size > 0
        manifest = contract.manifest
        assert manifest.synthetic is True
        assert manifest.provider == "mock"
        assert manifest.prompt_version
        assert manifest.cost.cost_basis == "unknown"
        for artifact in manifest.artifacts:
            path = contract.root / artifact.path
            assert path.exists(), f"manifest lists missing artifact {artifact.path}"
            assert sha256_file(path) == artifact.sha256, f"hash mismatch {artifact.path}"

    report = validate_dataset(dataset, expect_min_contracts=1)
    assert report.status.value == "PASS", report.errors
    assert report.contracts_checked == len(contracts)


def test_repair_loop_marks_repaired_not_rejected(tmp_path, monkeypatch):
    """A first failing attempt followed by a good one must be REPAIRED, not REJECTED."""

    from contractforge.providers.mock import MockProvider

    attempts = {"count": 0}
    original = MockProvider.complete

    def flaky_complete(self, request):  # noqa: ANN001
        attempts["count"] += 1
        response = original(self, request)
        if attempts["count"] == 1:
            # Corrupt the first answer so validation fails on missing surfaces.
            response.text = json.dumps(
                {
                    "document_id": request.metadata["document"]["document_id"],
                    "clauses": [
                        {"clause_id": "clause_01", "heading": "Fel", "text": "Inget av värde."}
                    ],
                }
            )
        return response

    monkeypatch.setattr(MockProvider, "complete", flaky_complete)
    dataset = tmp_path / "dataset"
    outcome = generate_dataset(_settings(), _request(count=1, clauses=8), dataset)
    assert outcome.results, "contract should be accepted after repair"
    result = outcome.results[0]
    assert result.status.value == "REPAIRED"
    assert result.attempts == 2
    assert any(
        document.status.value == "REPAIRED" for document in result.verification.documents
    )


def test_rejected_contracts_are_not_exported_as_approved(tmp_path, monkeypatch):
    from contractforge.models import LLMResponse, Usage
    from contractforge.providers.mock import MockProvider

    # Never produce parseable JSON -> GenerationError on every attempt.

    def garbage(self, request):  # noqa: ANN001
        return LLMResponse(
            text="detta är inte json",
            provider="mock",
            model="mock-1",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            latency_ms=1.0,
            finish_reason="stop",
        )

    monkeypatch.setattr(MockProvider, "complete", garbage)
    dataset = tmp_path / "dataset"
    outcome = generate_dataset(
        _settings(CONTRACTFORGE_MAX_REPAIRS="0"), _request(count=1, clauses=8), dataset
    )
    assert outcome.stats.contracts_accepted == 0
    assert outcome.stats.contracts_rejected == 1
    assert not list(dataset.glob("contract_*"))
    assert (dataset / "rejected" / "contract_0001.json").exists()
    report = validate_dataset(dataset)
    assert report.contracts_checked == 0
    assert report.status.value == "PASS"


def test_questions_have_valid_evidence_and_answerability(tmp_path):
    outcome = generate_dataset(
        _settings(), _request(count=1, difficulty=1, clauses=8, seed=3), tmp_path / "d"
    )
    assert outcome.results
    result = outcome.results[0]
    assert result.questions
    for question in result.questions:
        assert question.validated is True
        assert question.answerability in set(Answerability)
        for evidence in question.evidence:
            rendered = result.rendered[evidence.document_id]
            snippet = rendered.canonical_text[evidence.start : evidence.end]
            assert snippet, "evidence span must not be empty"
            if evidence.quote:
                assert evidence.quote.strip() in snippet or snippet.strip() in evidence.quote


def test_rerun_same_output_replaces_previous_dataset(tmp_path):
    """A repeated generate must not mix stale contracts or rejected entries."""

    dataset = tmp_path / "dataset"
    generate_dataset(_settings(), _request(count=3, clauses=6, seed=1), dataset)
    assert len(load_dataset(dataset)) == 3

    generate_dataset(_settings(), _request(count=2, clauses=6, seed=2), dataset)
    contracts = load_dataset(dataset)
    assert [contract.contract_id for contract in contracts] == ["contract_0001", "contract_0002"]
    assert not (dataset / "contract_0003").exists()
    assert not (dataset / "rejected").exists()

    report = validate_dataset(dataset)
    assert report.status.value == "PASS", report.errors
    assert report.contracts_checked == 2
