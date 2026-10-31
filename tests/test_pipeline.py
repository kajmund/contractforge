"""End-to-end integration: scenario -> mock generation -> validation -> gold -> export.

These tests exercise the whole vertical chain without network access.
"""

from __future__ import annotations

import json

import pytest

from contractforge.config import Settings
from contractforge.export.loader import load_dataset, load_dataset_manifest
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
    # A run where every contract is rejected is a failed run: generation must
    # not report success, and nothing may be exported as an approved contract.
    from contractforge.errors import ValidationError

    with pytest.raises(ValidationError, match="minst 1 förväntades"):
        generate_dataset(
            _settings(CONTRACTFORGE_MAX_REPAIRS="0"), _request(count=1, clauses=8), dataset
        )

    assert not list(dataset.glob("contract_*"))
    rejected = json.loads((dataset / "rejected" / "contract_0001.json").read_text(encoding="utf-8"))
    assert rejected["verification"]["status"] == "REJECTED"
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


def _run(settings: Settings, request: ScenarioRequest, output, **kwargs):
    from contractforge.pipeline import generate_dataset

    return generate_dataset(settings, request, output, **kwargs)


def test_foreign_output_directory_is_refused_and_nothing_deleted(tmp_path):
    from contractforge.errors import UsageError

    foreign = tmp_path / "out"
    (foreign / "contract_notes").mkdir(parents=True)
    keep = foreign / "contract_notes" / "keep.txt"
    keep.write_text("användarens fil", encoding="utf-8")

    try:
        _run(_settings(), _request(count=1, clauses=6), foreign)
    except UsageError as exc:
        assert exc.exit_code == 2
    else:  # pragma: no cover - the refusal is the point
        raise AssertionError("en främmande katalog måste refuseras")

    assert keep.read_text(encoding="utf-8") == "användarens fil"
    assert not (foreign / "dataset_manifest.json").exists()


def test_force_allows_replacing_a_foreign_directory(tmp_path):
    foreign = tmp_path / "out"
    foreign.mkdir()
    (foreign / "stale.txt").write_text("gammalt", encoding="utf-8")

    outcome = _run(_settings(), _request(count=1, clauses=6), foreign, force=True)

    assert outcome.stats.contracts_accepted == 1
    assert not (foreign / "stale.txt").exists()
    assert (foreign / ".contractforge").is_file()


def test_rerun_in_managed_directory_removes_only_generated_artifacts(tmp_path):
    dataset = tmp_path / "dataset"
    _run(_settings(), _request(count=2, clauses=6, seed=1), dataset)
    user_file = dataset / "mina_anteckningar.txt"
    user_file.write_text("behåll mig", encoding="utf-8")

    outcome = _run(_settings(), _request(count=1, clauses=6, seed=2), dataset)

    assert [contract.contract_id for contract in load_dataset(dataset)] == ["contract_0001"]
    assert not (dataset / "contract_0002").exists()
    assert user_file.is_file()
    assert outcome.validation_report is not None
    assert outcome.validation_report.status.value == "PASS"


def test_changed_prefix_in_managed_directory_is_refused(tmp_path):
    from contractforge.errors import UsageError

    dataset = tmp_path / "dataset"
    _run(_settings(), _request(count=1, clauses=6), dataset)

    with pytest.raises(UsageError, match="skapades med prefixet"):
        _run(_settings(), _request(count=1, clauses=6, contract_id_prefix="case"), dataset)

    # The previous run is intact.
    assert [contract.contract_id for contract in load_dataset(dataset)] == ["contract_0001"]


def test_generate_dataset_reports_validation_and_rejected_questions(tmp_path):
    outcome = _run(
        _settings(), _request(count=1, clauses=10, difficulty=3, seed=7), tmp_path / "ds"
    )
    assert outcome.validation_report is not None
    assert outcome.validation_report.status.value == "PASS"
    assert outcome.validation_report.contracts_checked == outcome.stats.contracts_accepted
    assert outcome.stats.questions_rejected == len(outcome.rejected_questions)


def test_config_snapshot_records_complete_scenario_inputs(tmp_path):
    dataset = tmp_path / "dataset"
    request = _request(count=1, clauses=6, seed=3)
    _run(_settings(), request, dataset)

    manifest = load_dataset_manifest(dataset)
    assert manifest is not None
    config = manifest.config
    assert config.package == request.package
    assert config.variation_overrides == request.variation_overrides
    assert [record.contract_type for record in config.scenario_definitions] == [
        request.contract_type.value
    ]
    assert len(config.scenario_definitions[0].sha256) == 64
    assert config.runtime.contractforge_version
    assert config.runtime.python_version
    assert config.runtime.dependencies


# ---------------------------------------------------------------------------
# F4: call accounting survives a failed attempt, and unknown usage is not a cost
# ---------------------------------------------------------------------------


def _pricing(settings: Settings) -> Settings:
    from dataclasses import replace

    from contractforge.models import ModelPrice, PricingTable

    return replace(
        settings,
        pricing=PricingTable(
            providers={
                "mock": {
                    "mock-1": ModelPrice(
                        input_usd_per_million=1.0, output_usd_per_million=2.0
                    )
                }
            }
        ),
    )


def _one_call(**usage_kwargs):
    from contractforge.models import LLMCallRecord, LLMRequest, Usage

    return LLMCallRecord(
        call_id="c1",
        purpose="contract_document",
        contract_id="contract_0001",
        request=LLMRequest(
            provider="mock",
            model="mock-1",
            prompt_version="v1",
            system="s",
            user="u",
        ),
        response_text="{}",
        usage=Usage(**usage_kwargs),
        latency_ms=4.0,
        provider="mock",
        model="mock-1",
    )


def test_summarize_cost_is_unknown_when_usage_is_unknown():
    """A configured price table must not turn ``Usage()`` into a 0.0 cost (F4)."""

    from contractforge.pipeline import summarize_cost

    settings = _pricing(_settings())
    summary = summarize_cost([_one_call()], settings)

    assert summary.calls == 1
    assert summary.cost_usd is None
    assert summary.cost_basis == "unknown"
    assert summary.tokens_known is False


def test_summarize_cost_prices_known_usage_from_the_configured_table():
    from contractforge.pipeline import summarize_cost

    summary = summarize_cost(
        [_one_call(input_tokens=1_000_000, output_tokens=500_000, total_tokens=1_500_000)],
        _pricing(_settings()),
    )

    assert summary.tokens_known is True
    assert summary.cost_basis == "configured_price_table"
    assert summary.cost_usd == pytest.approx(2.0)


def test_one_unknown_call_among_known_ones_keeps_the_cost_unknown():
    from contractforge.pipeline import summarize_cost

    known = _one_call(input_tokens=10, output_tokens=5, total_tokens=15)
    unknown = _one_call().model_copy(update={"call_id": "c2"})
    summary = summarize_cost([known, unknown], _pricing(_settings()))

    assert summary.calls == 2
    assert summary.tokens_known is False
    assert summary.cost_usd is None
    assert summary.cost_basis == "unknown"


def test_failed_attempt_calls_are_booked_in_the_rejected_contract(tmp_path, monkeypatch):
    """A failed attempt that already paid for a document still books its call (F4)."""

    from contractforge.errors import GenerationError, ValidationError
    from contractforge.models import LLMCallRecord, Usage
    from contractforge.providers.mock import MockProvider

    def partial_then_invalid(self, request):  # noqa: ANN001
        raise GenerationError(
            "document 2: provider text does not contain a valid JSON object",
            calls=[
                LLMCallRecord(
                    call_id="c-1",
                    purpose="contract_document",
                    contract_id=request.metadata["scenario"]["contract_id"],
                    request=request.model_copy(update={"metadata": {"purpose": "contract_document"}}),
                    response_text="{}",
                    usage=Usage(input_tokens=20, output_tokens=10, total_tokens=30),
                    latency_ms=1.0,
                    provider="mock",
                    model="mock-1",
                )
            ],
        )

    monkeypatch.setattr(MockProvider, "complete", partial_then_invalid)

    from contractforge.pipeline import run_contract
    from contractforge.scenarios.generator import ScenarioGenerator
    from contractforge.scenarios.registry import load_registry

    settings = _settings(CONTRACTFORGE_MAX_REPAIRS="0")
    scenario = ScenarioGenerator(load_registry()).generate(_request(count=1, clauses=8), 0)
    result = run_contract(scenario, MockProvider(), settings)

    assert result.status.value == "REJECTED"
    assert len(result.llm_calls) == 1
    assert result.llm_calls[0].usage.total_tokens == 30
    assert result.cost.calls == 1
    assert result.cost.total_tokens == 30
    assert result.cost.cost_basis == "unknown"

    # A wholly rejected dataset fails the final dataset validation, but the
    # rejected contract must still expose what the attempt cost.
    with pytest.raises(ValidationError):
        generate_dataset(
            _settings(CONTRACTFORGE_MAX_REPAIRS="0"),
            _request(count=1, clauses=8),
            tmp_path / "d",
        )

    payload = json.loads((tmp_path / "d" / "rejected" / "contract_0001.json").read_text())
    assert payload["status"] == "REJECTED"


def test_progress_messages_never_contain_a_provider_secret(tmp_path, monkeypatch):
    """Progress lines are logged/printed: they must carry scrubbed text (F5)."""

    from contractforge.errors import ProviderError, ValidationError
    from contractforge.providers.mock import MockProvider

    secret = "csk-review-secret-1234567890abcdef"
    messages: list[str] = []

    def failing(self, request):  # noqa: ANN001
        raise ProviderError(f"Cerebras returned HTTP 400: Bearer {secret}")

    monkeypatch.setattr(MockProvider, "complete", failing)
    with pytest.raises(ValidationError):
        generate_dataset(
            _settings(CONTRACTFORGE_MAX_REPAIRS="0"),
            _request(count=1, clauses=8),
            tmp_path / "d",
            progress=messages.append,
        )

    blob = "\n".join(messages)
    assert blob, "progress callback must be called"
    assert secret not in blob
    assert "[REDACTED]" in blob


def test_generate_dataset_closes_the_provider(tmp_path, monkeypatch):
    """No sockets may leak: generate_dataset closes the provider it created (F12)."""

    from contractforge.providers.mock import MockProvider

    closed: list[bool] = []
    original_close = MockProvider.close

    def close(self):  # noqa: ANN001
        closed.append(True)
        original_close(self)

    monkeypatch.setattr(MockProvider, "close", close)
    generate_dataset(_settings(), _request(count=1, clauses=8), tmp_path / "d")

    assert closed == [True]


def test_generate_dataset_closes_the_provider_even_when_generation_fails(tmp_path, monkeypatch):
    from contractforge.providers.mock import MockProvider

    closed: list[bool] = []
    original_close = MockProvider.close

    def close(self):  # noqa: ANN001
        closed.append(True)
        original_close(self)

    monkeypatch.setattr(MockProvider, "close", close)

    def broken(self, request):  # noqa: ANN001
        raise RuntimeError("boom")

    monkeypatch.setattr(MockProvider, "complete", broken)
    with pytest.raises(RuntimeError, match="boom"):
        generate_dataset(_settings(), _request(count=1, clauses=8), tmp_path / "d")

    assert closed == [True]
