from __future__ import annotations

import pytest
from pydantic import ValidationError

from contractforge.models import (
    Answerability,
    ExpectedAnswer,
    Fact,
    FactKind,
    GeneratedClause,
    GeneratedDocument,
    GoldenQuestion,
    Language,
    QuestionCategory,
    RenderedDocument,
    Scenario,
    TextSpan,
)


def _scenario_payload():
    return {
        "contract_id": "contract_0001",
        "contract_type": "consulting",
        "difficulty": 1,
        "seed": 1,
        "parties": [{"party_id": "party_a", "name": "A AB", "role": "client"}],
        "facts": [
            {
                "fact_id": "fact_notice_period",
                "kind": "duration",
                "value": 3,
                "unit": "months",
                "label_sv": "Uppsägningstid",
                "explicit_surfaces": ["tre (3) månader"],
            }
        ],
        "documents": [
            {
                "document_id": "main_agreement",
                "kind": "main_agreement",
                "title_sv": "Konsultavtal",
                "filename_stem": "main_agreement",
                "clauses": [
                    {
                        "clause_id": "clause_01",
                        "document_id": "main_agreement",
                        "order": 1,
                        "heading_sv": "Uppsägningstid",
                        "fact_ids": ["fact_notice_period"],
                    }
                ],
            }
        ],
        "variation": {"language": "sv"},
    }


def test_scenario_accepts_valid_payload():
    scenario = Scenario.model_validate(_scenario_payload())
    assert scenario.fact("fact_notice_period").value == 3
    assert scenario.clause("clause_01").document_id == "main_agreement"


def test_scenario_rejects_duplicate_fact_ids():
    payload = _scenario_payload()
    payload["facts"].append(dict(payload["facts"][0]))
    with pytest.raises(ValidationError, match="duplicate fact_id"):
        Scenario.model_validate(payload)


def test_scenario_rejects_dangling_fact_reference():
    payload = _scenario_payload()
    payload["documents"][0]["clauses"][0]["fact_ids"] = ["fact_missing"]
    with pytest.raises(ValidationError, match="missing fact"):
        Scenario.model_validate(payload)


def test_scenario_rejects_superseded_fact_without_target():
    payload = _scenario_payload()
    payload["facts"][0]["status"] = "superseded"
    with pytest.raises(ValidationError, match="superseded"):
        Scenario.model_validate(payload)


def test_rendered_document_rejects_out_of_bounds_span():
    with pytest.raises(ValidationError, match="beyond text length"):
        RenderedDocument(
            document_id="main_agreement",
            kind="main_agreement",
            title="Konsultavtal",
            language="sv",
            canonical_text="kort text",
            clause_spans={
                "clause_01": TextSpan(
                    document_id="main_agreement", clause_id="clause_01", start=0, end=500
                )
            },
        )


def test_generated_document_rejects_duplicate_clause_ids():
    with pytest.raises(ValidationError, match="duplicate generated clause"):
        GeneratedDocument(
            document_id="main_agreement",
            kind="main_agreement",
            title="T",
            clauses=[
                GeneratedClause(
                    clause_id="clause_01", document_id="main_agreement", heading="H", text="x"
                ),
                GeneratedClause(
                    clause_id="clause_01", document_id="main_agreement", heading="H", text="y"
                ),
            ],
        )


def test_answerable_question_requires_expected_answer():
    with pytest.raises(ValidationError, match="no expected answer"):
        GoldenQuestion(
            question_id="q_001",
            question="Vilken uppsägningstid gäller?",
            language=Language.SV,
            category=QuestionCategory.FACT_EXTRACTION,
            difficulty=1,
            expected_answer=ExpectedAnswer(),
            required_facts=["fact_notice_period"],
            answerability=Answerability.ANSWERABLE,
        )


def test_insufficient_evidence_question_may_have_no_answer():
    question = GoldenQuestion(
        question_id="q_002",
        question="Vilket pris gäller för bilaga C?",
        language=Language.SV,
        category=QuestionCategory.INSUFFICIENT_EVIDENCE,
        difficulty=6,
        expected_answer=ExpectedAnswer(),
        required_facts=[],
        answerability=Answerability.INSUFFICIENT_EVIDENCE,
    )
    assert question.validated is False


def test_fact_kind_parsed_from_string():
    fact = Fact(
        fact_id="fact_1",
        kind="money",
        value=100,
        currency="SEK",
        label_sv="Belopp",
        explicit_surfaces=["100 SEK"],
    )
    assert fact.kind == FactKind.MONEY
