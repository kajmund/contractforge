"""Tests for prompt assembly, JSON parsing and the document generation engine.

Deterministic tests only: the fake provider and the explicitly selected
``MockProvider`` are used, so no network call is made. The live smoke test is
skipped unless ``CEREBRAS_API_KEY`` is exported and is meant to be run by the
Lead after integration.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from contractforge.config import Settings
from contractforge.errors import GenerationError, ProviderError
from contractforge.generation.engine import DocumentGenerator, GenerationOutcome
from contractforge.generation.parsing import extract_json_object
from contractforge.generation.prompts import (
    PROMPT_VERSION,
    build_contract_request,
    prompt_sha256,
)
from contractforge.models import (
    Anomaly,
    AnomalyKind,
    ClauseMode,
    DocKind,
    DocumentSpec,
    Language,
    LLMRequest,
    LLMResponse,
    Scenario,
    Usage,
)
from contractforge.providers import MockProvider, create_provider

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:  # pragma: no cover - conftest already does this
    sys.path.insert(0, str(TESTS_DIR))

from factories import make_consulting_scenario  # noqa: E402


def _settings(**overrides) -> Settings:
    values = {
        "provider": "mock",
        "model": "mock-1",
        "temperature": 0.0,
        "seed": 42,
    }
    values.update(overrides)
    return Settings(**values)


def _payload_for(scenario: Scenario, document: DocumentSpec) -> dict:
    return {
        "document_id": document.document_id,
        "clauses": [
            {
                "clause_id": clause.clause_id,
                "heading": clause.heading(scenario.language),
                "text": f"Klausul {clause.order}: parterna ska iaktta avtalet.",
            }
            for clause in document.clauses
        ],
    }


class FakeProvider:
    """Records requests and returns queued JSON payloads or raw text."""

    name = "fake"
    model = "fake-1"

    def __init__(self, responses: list[dict | str]) -> None:
        self._responses = list(responses)
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("FakeProvider has no queued response")
        response = self._responses.pop(0)
        text = response if isinstance(response, str) else json.dumps(response)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            usage=Usage(input_tokens=11, output_tokens=22, total_tokens=33),
            latency_ms=12.5,
            finish_reason="stop",
            raw={"raw_id": "raw-1"},
        )


def _appendix() -> DocumentSpec:
    from contractforge.models import ClauseSpec

    return DocumentSpec(
        document_id="appendix_a",
        kind=DocKind.APPENDIX,
        title_sv="Bilaga A",
        title_en="Appendix A",
        filename_stem="appendix_a",
        references_documents=["main_agreement"],
        clauses=[
            ClauseSpec(
                clause_id="clause_06",
                document_id="appendix_a",
                order=6,
                heading_sv="Bilaga",
                heading_en="Appendix",
                mode=ClauseMode.EXPLICIT,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def test_prompt_contains_language_difficulty_facts_and_clause_plan():
    scenario = make_consulting_scenario()
    document = scenario.documents[0]
    request = build_contract_request(scenario, document, _settings())

    assert request.provider == "mock"
    assert request.model == "mock-1"
    assert request.prompt_version == PROMPT_VERSION == "v1"
    assert request.response_format == "json"
    assert request.temperature == 0.0

    user = request.user
    assert "Language: svenska (sv)" in user
    assert "Jurisdiction: SE" in user
    assert "Difficulty: 1 of 6" in user
    assert "Complexity:" in user
    assert "Style: formal" in user
    assert "Konsultavtal" in user
    assert document.document_id in user
    assert "clause_id=clause_01" in user
    assert "clause_id=clause_05" in user
    assert "950 SEK" in user
    assert "uppsägningstid" in user
    assert "Nordvik Teknik AB" in user
    assert "## Facts (the only material truth)" in user
    assert "## Anomalies that must be reproduced" in user
    assert "Return exactly one JSON object" in user

    system = request.system
    assert "synthetic" in system
    assert "Never mention" in system
    assert "No clause ids, document ids, fact ids" in system
    assert "[[PLACEHOLDER" not in user
    assert "[[" not in user
    assert "[[" not in system


def test_prompt_is_rendered_in_english_for_english_scenarios():
    scenario = make_consulting_scenario().model_copy(update={"language": Language.EN})
    request = build_contract_request(scenario, scenario.documents[0], _settings())

    assert "Language: engelska (en)" in request.user
    assert "Consulting Agreement" in request.user
    assert "Language: svenska (sv)" not in request.user


def test_prompt_metadata_carries_scenario_and_document_json():
    scenario = make_consulting_scenario()
    document = scenario.documents[0]
    request = build_contract_request(
        scenario,
        document,
        _settings(),
        attempt=2,
        feedback=["Saknar klausul 3", "Felaktigt belopp"],
    )

    assert set(request.metadata) == {"scenario", "document", "attempt", "feedback"}
    assert json.loads(json.dumps(request.metadata))["attempt"] == 2
    restored_scenario = Scenario.model_validate(request.metadata["scenario"])
    assert restored_scenario.contract_id == scenario.contract_id
    restored_document = DocumentSpec.model_validate(request.metadata["document"])
    assert restored_document.document_id == document.document_id
    assert request.metadata["attempt"] == 2
    assert request.metadata["feedback"] == ["Saknar klausul 3", "Felaktigt belopp"]

    assert "Attempt: 2" in request.user
    assert "- Saknar klausul 3" in request.user
    assert "- Felaktigt belopp" in request.user


def test_prompt_uses_settings_seed_but_falls_back_to_scenario_seed():
    scenario = make_consulting_scenario()
    document = scenario.documents[0]

    assert build_contract_request(scenario, document, _settings(seed=7)).seed == 7
    assert build_contract_request(scenario, document, _settings(seed=None)).seed == scenario.seed


def test_prompt_hash_is_deterministic_sha256():
    first = prompt_sha256()
    second = prompt_sha256()
    assert first == second
    assert len(first) == 64
    assert all(char in "0123456789abcdef" for char in first)


def test_prompt_never_contains_api_key():
    scenario = make_consulting_scenario()
    document = scenario.documents[0]
    settings = _settings(api_key="super-secret-cerebras-key")
    request = build_contract_request(scenario, document, settings)

    combined = "\n".join(
        [
            request.system,
            request.user,
            json.dumps(request.metadata, ensure_ascii=False),
        ]
    )
    assert "super-secret-cerebras-key" not in combined


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def test_extract_json_object_plain():
    assert extract_json_object('{"document_id": "main_agreement"}') == {
        "document_id": "main_agreement"
    }


def test_extract_json_object_from_markdown_fence():
    text = '```json\n{"document_id": "main_agreement", "clauses": []}\n```'
    assert extract_json_object(text)["document_id"] == "main_agreement"


def test_extract_json_object_with_surrounding_prose():
    text = 'Here is the result:\n{"document_id": "main_agreement"}\nHope it helps.'
    assert extract_json_object(text) == {"document_id": "main_agreement"}


def test_extract_json_object_keeps_braces_inside_strings():
    text = '{"text": "ett { inre } objekt", "ok": true}'
    assert extract_json_object(text) == {"text": "ett { inre } objekt", "ok": True}


def test_extract_json_object_skips_invalid_candidates():
    text = 'anteckning {inte json} sedan {"ok": true}'
    assert extract_json_object(text) == {"ok": True}


def test_extract_json_object_nested_object():
    text = '{"outer": {"inner": [1, 2, 3]}}'
    assert extract_json_object(text) == {"outer": {"inner": [1, 2, 3]}}


@pytest.mark.parametrize("text", ["", "   ", "ingen json här", "[1, 2, 3]", '"bara en sträng"'])
def test_extract_json_object_rejects_non_object_or_empty(text):
    with pytest.raises(GenerationError):
        extract_json_object(text)


# ---------------------------------------------------------------------------
# Engine: successful generation
# ---------------------------------------------------------------------------


def test_engine_generates_package_and_sanitized_call_records():
    scenario = make_consulting_scenario()
    document = scenario.documents[0]
    payload = _payload_for(scenario, document)
    payload["clauses"] = list(reversed(payload["clauses"]))  # order must not matter
    provider = FakeProvider([payload])

    outcome = DocumentGenerator(provider, _settings()).generate(scenario)

    assert isinstance(outcome, GenerationOutcome)
    assert outcome.package.contract_id == scenario.contract_id
    assert len(outcome.package.documents) == 1
    generated = outcome.package.documents[0]
    assert generated.document_id == document.document_id
    assert generated.kind == document.kind
    assert generated.title == document.title_sv
    assert [clause.clause_id for clause in generated.clauses] == [
        clause.clause_id for clause in document.clauses
    ]
    for clause, spec in zip(generated.clauses, document.clauses, strict=True):
        assert clause.heading == spec.heading_sv
        assert clause.text.strip()
        assert clause.document_id == document.document_id

    assert len(outcome.calls) == 1
    call = outcome.calls[0]
    assert call.purpose == "contract_document"
    assert call.contract_id == scenario.contract_id
    assert call.document_id == document.document_id
    assert call.attempt == 1
    assert call.provider == "fake"
    assert call.model == "fake-1"
    assert call.usage.input_tokens == 11
    assert call.usage.output_tokens == 22
    assert call.usage.total_tokens == 33
    assert call.latency_ms == 12.5
    assert call.finish_reason == "stop"
    assert call.raw == {"raw_id": "raw-1"}
    assert call.response_text == json.dumps(payload)

    assert call.request.metadata == {
        "contract_id": scenario.contract_id,
        "document_id": document.document_id,
        "purpose": "contract_document",
        "difficulty": scenario.difficulty,
    }
    assert "scenario" in provider.requests[0].metadata
    assert "document" in provider.requests[0].metadata


def test_engine_makes_one_call_per_document_in_spec_order():
    scenario = make_consulting_scenario()
    appendix = _appendix()
    scenario = scenario.model_copy(update={"documents": [scenario.documents[0], appendix]})
    payloads = [_payload_for(scenario, document) for document in scenario.documents]
    provider = FakeProvider(payloads)

    outcome = DocumentGenerator(provider, _settings()).generate(scenario)

    assert len(provider.requests) == 2
    assert len(outcome.calls) == 2
    assert [call.document_id for call in outcome.calls] == [
        "main_agreement",
        "appendix_a",
    ]
    assert [document.document_id for document in outcome.package.documents] == [
        "main_agreement",
        "appendix_a",
    ]
    assert outcome.package.documents[1].references == ["main_agreement"]


def test_engine_passes_feedback_and_attempt_to_provider_and_call():
    scenario = make_consulting_scenario()
    payload = _payload_for(scenario, scenario.documents[0])
    provider = FakeProvider([payload])

    outcome = DocumentGenerator(provider, _settings()).generate(
        scenario, feedback=["Rätta beloppet"], attempt=3
    )

    request = provider.requests[0]
    assert request.metadata["attempt"] == 3
    assert request.metadata["feedback"] == ["Rätta beloppet"]
    assert "Rätta beloppet" in request.user
    assert "Attempt: 3" in request.user
    assert outcome.calls[0].attempt == 3


# ---------------------------------------------------------------------------
# Engine: invalid structured output
# ---------------------------------------------------------------------------


def _run_with_payload(payload: dict | str) -> None:
    scenario = make_consulting_scenario()
    provider = FakeProvider([payload])
    DocumentGenerator(provider, _settings()).generate(scenario)


def test_engine_rejects_missing_clause():
    scenario = make_consulting_scenario()
    payload = _payload_for(scenario, scenario.documents[0])
    payload["clauses"] = payload["clauses"][:-1]
    with pytest.raises(GenerationError, match="clause ids do not match"):
        _run_with_payload(payload)


def test_engine_rejects_unexpected_clause():
    scenario = make_consulting_scenario()
    payload = _payload_for(scenario, scenario.documents[0])
    payload["clauses"].append({"clause_id": "clause_99", "heading": "Påhittad", "text": "Text."})
    with pytest.raises(GenerationError, match="clause ids do not match"):
        _run_with_payload(payload)


def test_engine_rejects_duplicate_clause():
    scenario = make_consulting_scenario()
    payload = _payload_for(scenario, scenario.documents[0])
    payload["clauses"].append(dict(payload["clauses"][0]))
    with pytest.raises(GenerationError, match="duplicate clause_id"):
        _run_with_payload(payload)


def test_engine_rejects_wrong_document_id():
    scenario = make_consulting_scenario()
    payload = _payload_for(scenario, scenario.documents[0])
    payload["document_id"] = "appendix_b"
    with pytest.raises(GenerationError, match="document_id"):
        _run_with_payload(payload)


def test_engine_rejects_empty_heading():
    scenario = make_consulting_scenario()
    payload = _payload_for(scenario, scenario.documents[0])
    payload["clauses"][0]["heading"] = "   "
    with pytest.raises(GenerationError, match="empty heading"):
        _run_with_payload(payload)


def test_engine_rejects_empty_text():
    scenario = make_consulting_scenario()
    payload = _payload_for(scenario, scenario.documents[0])
    payload["clauses"][0]["text"] = ""
    with pytest.raises(GenerationError, match="empty text"):
        _run_with_payload(payload)


def test_engine_rejects_non_list_clauses():
    with pytest.raises(GenerationError):
        _run_with_payload({"document_id": "main_agreement", "clauses": "inte en lista"})


def test_engine_rejects_invalid_json_text():
    with pytest.raises(GenerationError):
        _run_with_payload("Det här är inte JSON alls.")


def test_engine_rejects_json_array_text():
    with pytest.raises(GenerationError):
        _run_with_payload('[{"document_id": "main_agreement"}]')


def _two_document_scenario() -> Scenario:
    scenario = make_consulting_scenario()
    return scenario.model_copy(update={"documents": [scenario.documents[0], _appendix()]})


class UsageProvider(FakeProvider):
    """Fake provider whose token usage differs per call, like a real answer stream."""

    def __init__(self, responses: list[dict | str], totals: list[int]) -> None:
        super().__init__(responses)
        self._totals = list(totals)

    def complete(self, request: LLMRequest) -> LLMResponse:
        response = super().complete(request)
        total = self._totals.pop(0)
        return response.model_copy(
            update={"usage": Usage(input_tokens=total - 10, output_tokens=10, total_tokens=total)}
        )


def test_generation_error_carries_every_call_booked_before_the_failure():
    """Document 1 valid (30 tokens), document 2 invalid JSON (90 tokens): both booked."""

    scenario = _two_document_scenario()
    first = _payload_for(scenario, scenario.documents[0])
    provider = UsageProvider([first, "detta är inte json"], totals=[30, 90])

    with pytest.raises(GenerationError) as excinfo:
        DocumentGenerator(provider, _settings()).generate(scenario)

    calls = excinfo.value.calls
    assert len(calls) == 2
    assert [call.document_id for call in calls] == [
        document.document_id for document in scenario.documents
    ]
    assert calls[0].usage.total_tokens == 30
    assert calls[1].usage.total_tokens == 90
    # The failing call is booked too: its tokens were spent.
    assert calls[1].response_text == "detta är inte json"
    assert calls[0].request.metadata["document_id"] == scenario.documents[0].document_id
    assert "scenario" not in calls[0].request.metadata


def test_generation_error_carries_calls_when_the_provider_fails_later():
    """A provider failure on document 2 keeps document 1's booked call."""

    scenario = _two_document_scenario()

    class DyingProvider(FakeProvider):
        def complete(self, request: LLMRequest) -> LLMResponse:
            if self.requests:
                raise GenerationError("mock provider broke")
            return super().complete(request)

    provider = DyingProvider([_payload_for(scenario, scenario.documents[0])])
    with pytest.raises(GenerationError) as excinfo:
        DocumentGenerator(provider, _settings()).generate(scenario)

    assert len(excinfo.value.calls) == 1
    assert excinfo.value.calls[0].document_id == scenario.documents[0].document_id


def test_generation_error_to_dict_mentions_generation_and_never_raw():
    scenario = _two_document_scenario()
    provider = UsageProvider(
        [_payload_for(scenario, scenario.documents[0]), "inte json"], totals=[30, 90]
    )

    with pytest.raises(GenerationError) as excinfo:
        DocumentGenerator(provider, _settings()).generate(scenario)

    payload = excinfo.value.to_dict()
    assert payload["generation"]["attempts"] == 1
    assert len(payload["generation"]["calls"]) == 2
    assert "raw" not in payload["generation"]["calls"][0]


def test_engine_propagates_provider_error():
    class FailingProvider:
        name = "failing"
        model = "failing-1"

        def complete(self, request: LLMRequest) -> LLMResponse:
            raise ProviderError("provider exploded")

    scenario = make_consulting_scenario()
    with pytest.raises(ProviderError, match="provider exploded"):
        DocumentGenerator(FailingProvider(), _settings()).generate(scenario)


# ---------------------------------------------------------------------------
# Engine + MockProvider integration
# ---------------------------------------------------------------------------


def test_engine_with_mock_provider_is_deterministic_and_validation_friendly():
    scenario = make_consulting_scenario()
    settings = _settings()
    first = DocumentGenerator(MockProvider(), settings).generate(scenario)
    second = DocumentGenerator(MockProvider(), settings).generate(scenario)

    assert first.package == second.package
    assert [call.response_text for call in first.calls] == [
        call.response_text for call in second.calls
    ]
    assert len(first.calls) == len(scenario.documents)
    for call in first.calls:
        assert set(call.request.metadata) == {
            "contract_id",
            "document_id",
            "purpose",
            "difficulty",
        }
        assert "scenario" not in call.request.metadata

    main = first.package.document("main_agreement")
    party_text = main.clause("clause_01").text
    assert "Nordvik Teknik AB" in party_text
    assert "Sundberg Konsult AB" in party_text
    hourly = scenario.fact("fact_hourly_rate")
    assert any(surface in main.clause("clause_02").text for surface in hourly.explicit_surfaces)
    for clause in main.clauses:
        assert clause.heading.strip()
        assert clause.text.strip()


def test_mock_provider_paraphrase_mode_uses_paraphrase_surfaces():
    scenario = make_consulting_scenario()
    document = scenario.documents[0]
    clauses = [
        clause.model_copy(update={"mode": ClauseMode.PARAPHRASE})
        if clause.clause_id == "clause_03"
        else clause
        for clause in document.clauses
    ]
    document = document.model_copy(update={"clauses": clauses})
    scenario = scenario.model_copy(update={"documents": [document]})

    outcome = DocumentGenerator(MockProvider(), _settings()).generate(scenario)
    text = outcome.package.document("main_agreement").clause("clause_03").text

    notice_period = scenario.fact("fact_notice_period")
    assert any(surface in text for surface in notice_period.paraphrase_surfaces)
    for explicit_surface in notice_period.explicit_surfaces:
        assert explicit_surface not in text


def test_mock_provider_prefers_paraphrase_surfaces_and_never_mixes_explicit_forms():
    scenario = make_consulting_scenario()
    document = scenario.documents[0]
    salary = scenario.fact("fact_hourly_rate").model_copy(
        update={"paraphrase_surfaces": ["950 kronor per timme"]}
    )
    facts = [fact for fact in scenario.facts if fact.fact_id != salary.fact_id] + [salary]
    clauses = [
        clause.model_copy(update={"mode": ClauseMode.PARAPHRASE})
        if clause.clause_id == "clause_02"
        else clause
        for clause in document.clauses
    ]
    document = document.model_copy(update={"clauses": clauses})
    scenario = scenario.model_copy(update={"documents": [document], "facts": facts})

    outcome = DocumentGenerator(MockProvider(), _settings()).generate(scenario)
    text = outcome.package.document("main_agreement").clause("clause_02").text

    assert "950 kronor per timme" in text
    assert "950 SEK" not in text
    # No Swedish number-word rendering when verified paraphrase forms exist.
    assert "niohundrafemtio" not in text


def test_mock_provider_omits_missing_information():
    scenario = make_consulting_scenario()
    anomaly = Anomaly(
        anomaly_id="anom_missing",
        kind=AnomalyKind.MISSING_INFORMATION,
        fact_ids=["fact_extension_period"],
        document_ids=["main_agreement"],
        description_sv="Förlängningsperioden är avsiktligt oreglerad.",
    )
    scenario = scenario.model_copy(update={"anomalies": [anomaly]})

    outcome = DocumentGenerator(MockProvider(), _settings()).generate(scenario)
    text = outcome.package.document("main_agreement").clause("clause_04").text

    assert "12" not in text
    assert "tolv" not in text
    assert "inte reglerat" in text


def test_mock_provider_reproduces_intentional_contradiction_description():
    scenario = make_consulting_scenario()
    anomaly = Anomaly(
        anomaly_id="anom_conflict",
        kind=AnomalyKind.INTENTIONAL_CONTRADICTION,
        fact_ids=["fact_liability_cap"],
        document_ids=["main_agreement"],
        description_sv="En tidigare version angav 400 000 SEK som ansvarsgräns.",
    )
    scenario = scenario.model_copy(update={"anomalies": [anomaly]})

    outcome = DocumentGenerator(MockProvider(), _settings()).generate(scenario)
    main = outcome.package.document("main_agreement")
    text = main.clause("clause_05").text

    assert "400 000 SEK" in text
    assert "500 000" in text
    # The anomaly must not be sprayed over every clause of the document.
    assert "400 000 SEK" not in main.clause("clause_02").text
    assert "400 000 SEK" not in main.clause("clause_03").text


# ---------------------------------------------------------------------------
# Live smoke test (skipped unless CEREBRAS_API_KEY is exported)
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("CEREBRAS_API_KEY"),
    reason="CEREBRAS_API_KEY not set; live generation smoke test skipped",
)
def test_live_generation_smoke():
    settings = Settings.from_env()
    provider = create_provider(settings)
    scenario = make_consulting_scenario()

    outcome = DocumentGenerator(provider, settings).generate(scenario)

    assert len(outcome.package.documents) == len(scenario.documents)
    assert len(outcome.calls) == len(scenario.documents)
    for document in outcome.package.documents:
        assert document.clauses
        for clause in document.clauses:
            assert clause.heading.strip()
            assert clause.text.strip()


def test_mock_provider_uses_paraphrase_surfaces_in_generated_level2_scenarios():
    from contractforge.models import ContractType, ScenarioRequest
    from contractforge.scenarios.generator import ScenarioGenerator
    from contractforge.scenarios.registry import load_registry

    generator = ScenarioGenerator(load_registry())
    for contract_type in (
        ContractType.CONSULTING,
        ContractType.EMPLOYMENT,
        ContractType.SUPPLY,
    ):
        request = ScenarioRequest(
            contract_type=contract_type,
            count=1,
            clauses=8,
            difficulty=2,
            language=Language.SV,
            seed=77,
        )
        scenario = generator.generate(request, index=0)
        outcome = DocumentGenerator(MockProvider(), _settings()).generate(scenario)
        for generated_document in outcome.package.documents:
            for generated_clause in generated_document.clauses:
                spec_clause = scenario.clause(generated_clause.clause_id)
                if spec_clause.mode is not ClauseMode.PARAPHRASE:
                    continue
                for fact_id in spec_clause.fact_ids:
                    fact = scenario.fact(fact_id)
                    if not fact.paraphrase_surfaces:
                        continue
                    assert any(
                        surface in generated_clause.text for surface in fact.paraphrase_surfaces
                    ), (
                        f"{contract_type.value}: {generated_clause.clause_id} "
                        f"saknar parafrasyta för {fact_id}"
                    )


def test_mock_provider_uses_paraphrase_fallback_to_explicit_when_no_paraphrase():
    scenario = make_consulting_scenario()
    document = scenario.documents[0]
    hourly_rate = scenario.fact("fact_hourly_rate").model_copy(update={"paraphrase_surfaces": []})
    facts = [fact for fact in scenario.facts if fact.fact_id != hourly_rate.fact_id] + [hourly_rate]
    clauses = [
        clause.model_copy(update={"mode": ClauseMode.PARAPHRASE})
        if clause.clause_id == "clause_02"
        else clause
        for clause in document.clauses
    ]
    document = document.model_copy(update={"clauses": clauses})
    scenario = scenario.model_copy(update={"documents": [document], "facts": facts})

    outcome = DocumentGenerator(MockProvider(), _settings()).generate(scenario)
    text = outcome.package.document("main_agreement").clause("clause_02").text

    assert any(surface in text for surface in hourly_rate.explicit_surfaces)


def test_mock_provider_explicit_clauses_always_contain_explicit_surfaces():
    from contractforge.models import AnomalyKind, ContractType, ScenarioRequest
    from contractforge.scenarios.generator import ScenarioGenerator
    from contractforge.scenarios.registry import load_registry

    generator = ScenarioGenerator(load_registry())
    for contract_type in ContractType:
        for difficulty in (1, 2, 3, 4):
            request = ScenarioRequest(
                contract_type=contract_type,
                count=1,
                clauses=8,
                difficulty=difficulty,
                language=Language.SV,
                seed=500 + difficulty,
            )
            scenario = generator.generate(request, index=0)
            missing_fact_ids = {
                fact_id
                for anomaly in scenario.anomalies
                if anomaly.kind is AnomalyKind.MISSING_INFORMATION
                for fact_id in anomaly.fact_ids
            }
            outcome = DocumentGenerator(MockProvider(), _settings()).generate(scenario)
            for generated_document in outcome.package.documents:
                for generated_clause in generated_document.clauses:
                    spec_clause = scenario.clause(generated_clause.clause_id)
                    if spec_clause.mode is not ClauseMode.EXPLICIT:
                        continue
                    for fact_id in spec_clause.fact_ids:
                        if fact_id in missing_fact_ids:
                            continue
                        fact = scenario.fact(fact_id)
                        assert any(
                            surface in generated_clause.text for surface in fact.explicit_surfaces
                        ), (
                            f"{contract_type.value} nivå {difficulty}: "
                            f"{generated_clause.clause_id} saknar explicit yta för {fact_id}"
                        )


def test_mock_provider_prefers_semantically_recognizable_paraphrase_surface():
    scenario = make_consulting_scenario()
    document = scenario.documents[0]
    notice_period = scenario.fact("fact_notice_period").model_copy(
        update={"paraphrase_surfaces": ["tre dygn", "ett kvartal"]}
    )
    facts = [fact for fact in scenario.facts if fact.fact_id != notice_period.fact_id] + [
        notice_period
    ]
    clauses = [
        clause.model_copy(update={"mode": ClauseMode.PARAPHRASE})
        if clause.clause_id == "clause_03"
        else clause
        for clause in document.clauses
    ]
    document = document.model_copy(update={"clauses": clauses})
    scenario = scenario.model_copy(update={"documents": [document], "facts": facts})

    outcome = DocumentGenerator(MockProvider(), _settings()).generate(scenario)
    text = outcome.package.document("main_agreement").clause("clause_03").text

    # "tre dygn" is a paraphrase surface but not a unit a deterministic
    # normalizer can map; "ett kvartal" is.
    assert "ett kvartal" in text
    assert "tre dygn" not in text


# ---------------------------------------------------------------------------
# Document references must be named in the source clause text
# ---------------------------------------------------------------------------


def _generated_package(request, index: int = 0):
    from contractforge.gold import build_gold_set
    from contractforge.rendering.canonical import render_package
    from contractforge.scenarios.generator import ScenarioGenerator
    from contractforge.scenarios.registry import load_registry

    generator = ScenarioGenerator(load_registry())
    scenario = generator.generate(request, index=index)
    outcome = DocumentGenerator(MockProvider(), _settings()).generate(scenario)
    rendered = render_package(scenario, outcome.package)
    gold = build_gold_set(scenario, outcome.package, rendered)
    return scenario, outcome.package, rendered, gold


def _needles_for(scenario, reference) -> list[str]:
    needles: list[str] = []
    if reference.target_clause_id is not None:
        clause = scenario.clause(reference.target_clause_id)
        needles.extend([clause.heading_sv, clause.heading_en])
    document = scenario.document(reference.target_document_id)
    needles.extend([document.title_sv, document.title_en])
    return [needle for needle in needles if needle and needle.strip()]


def test_mock_provider_names_the_target_document_in_the_referring_clause():
    from contractforge.models import ContractType, ScenarioRequest

    scenario, _package, _rendered, _gold = _generated_package(
        ScenarioRequest(
            contract_type=ContractType.CONSULTING,
            count=1,
            clauses=10,
            difficulty=3,
            language=Language.SV,
            seed=42,
        )
    )
    main = scenario.document("main_agreement")
    referring = [clause for clause in main.clauses if "appendix_a" in clause.reference_to]
    assert referring, "main agreement must anchor the appendix in a clause"

    outcome = DocumentGenerator(MockProvider(), _settings()).generate(scenario)
    for clause in referring:
        text = outcome.package.document("main_agreement").clause(clause.clause_id).text
        appendix = scenario.document("appendix_a")
        assert appendix.title(Language.SV) in text
        assert "Närmare bestämmelser framgår av" in text


def test_mock_provider_english_reference_sentence_names_the_target_document():
    from contractforge.models import ContractType, ScenarioRequest

    scenario, _package, _rendered, _gold = _generated_package(
        ScenarioRequest(
            contract_type=ContractType.CONSULTING,
            count=1,
            clauses=10,
            difficulty=3,
            language=Language.EN,
            seed=42,
        )
    )
    outcome = DocumentGenerator(MockProvider(), _settings()).generate(scenario)
    main = scenario.document("main_agreement")
    referring = [clause for clause in main.clauses if "appendix_a" in clause.reference_to]
    assert referring
    for clause in referring:
        text = outcome.package.document("main_agreement").clause(clause.clause_id).text
        assert scenario.document("appendix_a").title(Language.EN) in text
        assert "Further provisions are set out in" in text


def test_reference_sentence_is_not_duplicated_for_reference_mode_clauses():
    """A reference-mode clause already names its target through _reference_text."""

    from contractforge.models import ContractType, ScenarioRequest

    scenario, _package, _rendered, _gold = _generated_package(
        ScenarioRequest(
            contract_type=ContractType.CONSULTING,
            count=1,
            clauses=10,
            difficulty=4,
            language=Language.SV,
            seed=42,
        )
    )
    outcome = DocumentGenerator(MockProvider(), _settings()).generate(scenario)
    for clause in scenario.document("main_agreement").clauses:
        if clause.mode is not ClauseMode.REFERENCE or not clause.fact_ids:
            continue
        text = outcome.package.document("main_agreement").clause(clause.clause_id).text
        # Distribution targets are clause ids, already worded by the template.
        assert "Närmare bestämmelser framgår av" not in text


def test_gold_reference_evidence_is_narrow_and_names_the_target():
    from contractforge.models import ContractType, ScenarioRequest

    scenario, _package, rendered, gold = _generated_package(
        ScenarioRequest(
            contract_type=ContractType.CONSULTING,
            count=1,
            clauses=10,
            difficulty=4,
            language=Language.SV,
            seed=42,
        )
    )
    checked = 0
    for reference in gold.references:
        if not reference.evidence:
            continue
        needles = _needles_for(scenario, reference)
        for evidence in reference.evidence:
            text = rendered[evidence.document_id].canonical_text
            slice_text = text[evidence.start : evidence.end]
            assert evidence.quote == slice_text
            assert any(needle in slice_text for needle in needles), slice_text
            # Never the whole clause: the quote must be narrower than it.
            if reference.source_clause_id is not None:
                span = rendered[reference.source_document_id].clause_spans[
                    reference.source_clause_id
                ]
                assert (evidence.start, evidence.end) != (span.start, span.end)
            checked += 1
    assert checked, "expected at least one in-package reference with evidence"


def test_gold_reference_without_text_support_has_no_evidence():
    """A declared-but-missing document is never named, so it gets no span."""

    from contractforge.models import ContractType, ScenarioRequest

    scenario, _package, _rendered, gold = _generated_package(
        ScenarioRequest(
            contract_type=ContractType.CONSULTING,
            count=1,
            clauses=10,
            difficulty=6,
            language=Language.SV,
            seed=42,
        )
    )
    assert scenario.missing_documents
    unresolved = [
        reference
        for reference in gold.references
        if reference.target_document_id in scenario.missing_documents
    ]
    assert unresolved, "level 6 must reference declared-missing documents"
    for reference in unresolved:
        assert reference.evidence == []


def test_gold_reference_evidence_never_spans_an_unrelated_clause():
    """No gold reference may fall back to the first clause of the document."""

    from contractforge.models import ContractType, ScenarioRequest

    for contract_type in (ContractType.CONSULTING, ContractType.SUPPLY):
        for difficulty in (3, 4, 5, 6):
            scenario, _package, rendered, gold = _generated_package(
                ScenarioRequest(
                    contract_type=contract_type,
                    count=1,
                    clauses=10,
                    difficulty=difficulty,
                    language=Language.SV,
                    seed=42,
                )
            )
            for reference in gold.references:
                if not reference.evidence:
                    continue
                needles = _needles_for(scenario, reference)
                for evidence in reference.evidence:
                    text = rendered[evidence.document_id].canonical_text
                    slice_text = text[evidence.start : evidence.end]
                    # The old fallback returned the first clause of the source
                    # document; now every span must actually name the target.
                    assert any(needle in slice_text for needle in needles), slice_text


@pytest.mark.parametrize(
    "reference",
    [
        "appendix_a_clause_01",  # bare clause id (ClauseSpec.reference_to form)
        "appendix_a",  # whole document
        "appendix_a:appendix_a_clause_01",  # explicit document:clause form
    ],
)
def test_reference_sentence_names_any_reference_form(reference):
    """Cross-document references name the target in all supported token forms.

    ``ClauseSpec.reference_to`` normally stores bare clause ids, which is the
    form the scenario generator uses for semantic (anomaly) cross references.
    """

    from contractforge.models import ContractType, ScenarioRequest
    from contractforge.providers.mock import _reference_clause_sentence

    scenario, _package, _rendered, _gold = _generated_package(
        ScenarioRequest(
            contract_type=ContractType.CONSULTING,
            count=1,
            clauses=10,
            difficulty=5,
            language=Language.SV,
            seed=42,
        )
    )
    clause = scenario.clause("clause_03").model_copy(
        update={"reference_to": [reference], "depends_on": [], "fact_ids": []}
    )
    sentence = _reference_clause_sentence(scenario, clause, Language.SV)
    assert sentence
    assert scenario.document("appendix_a").title(Language.SV) in sentence
