"""Tests for deterministic golden-question generation and text-grounded verification."""

from __future__ import annotations

from factories import (
    make_consulting_scenario,
    make_fact,
    make_generated_package,
    make_rendered_documents,
)

from contractforge.gold import build_gold_set
from contractforge.models import (
    Anomaly,
    AnomalyKind,
    Answerability,
    ClauseSpec,
    DocKind,
    DocumentSpec,
    EvidenceSpan,
    FactKind,
    GoldSet,
    Language,
    QuestionCategory,
    Relation,
    RelationKind,
    Scenario,
)
from contractforge.questions import generate_and_verify_questions

PARTY_TEXT = "Parterna Nordvik Teknik AB och Sundberg Konsult AB ingår avtalet."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def clean_scenario() -> Scenario:
    return make_consulting_scenario()


def clean_package(scenario: Scenario):
    package = make_generated_package(scenario)
    document = package.document("main_agreement")
    clauses = list(document.clauses)
    clauses[0] = clauses[0].model_copy(update={"text": PARTY_TEXT})
    document = document.model_copy(update={"clauses": clauses})
    return package.model_copy(update={"documents": [document]})


def generate(scenario: Scenario, *, max_questions: int | None = None):
    package = clean_package(scenario)
    rendered = make_rendered_documents(scenario, package)
    gold = build_gold_set(scenario, package, rendered)
    return (
        generate_and_verify_questions(
            scenario, gold, rendered, max_questions=max_questions
        ),
        gold,
        rendered,
    )


def golden(gold: GoldSet, fact_id: str):
    return gold.fact(fact_id)


def evidence_text(question, rendered) -> str:
    parts = []
    for span in question.evidence:
        document = rendered.get(span.document_id)
        if document is not None:
            parts.append(document.canonical_text[span.start : span.end])
    return "\n".join(parts)


def conflicting_scenario() -> Scenario:
    scenario = make_consulting_scenario(difficulty=5)
    amendment = make_fact(
        "fact_notice_period_amendment",
        6,
        kind=FactKind.DURATION,
        unit="months",
        label_sv="Ändrad uppsägningstid",
        explicit_surfaces=["sex (6) månader"],
        keywords_sv=["uppsägningstid"],
    )
    document = scenario.documents[0]
    clauses = list(document.clauses)
    clauses[2] = clauses[2].model_copy(
        update={"fact_ids": ["fact_notice_period", "fact_notice_period_amendment"]}
    )
    document = document.model_copy(update={"clauses": clauses})
    anomaly = Anomaly(
        anomaly_id="anomaly_conflict",
        kind=AnomalyKind.INTENTIONAL_CONTRADICTION,
        fact_ids=["fact_notice_period", "fact_notice_period_amendment"],
        governing_fact_id="fact_notice_period",
        answerability=Answerability.CONFLICTING_EVIDENCE,
        description_sv="Huvudavtal och tillägg anger olika uppsägningstider.",
    )
    return scenario.model_copy(
        update={
            "facts": [*scenario.facts, amendment],
            "documents": [document],
            "anomalies": [anomaly],
        }
    )


def insufficient_scenario() -> Scenario:
    scenario = make_consulting_scenario(difficulty=6)
    anomaly = Anomaly(
        anomaly_id="anomaly_missing",
        kind=AnomalyKind.MISSING_INFORMATION,
        fact_ids=["fact_extension_period"],
        answerability=Answerability.INSUFFICIENT_EVIDENCE,
        description_sv="Förlängningsperioden regleras i ett saknat sidodokument.",
    )
    return scenario.model_copy(
        update={"anomalies": [anomaly], "missing_documents": ["side_letter_2026"]}
    )


def scenario_with_appendix() -> Scenario:
    scenario = make_consulting_scenario()
    appendix = DocumentSpec(
        document_id="appendix_a",
        kind=DocKind.APPENDIX,
        title_sv="Bilaga A",
        title_en="Appendix A",
        filename_stem="appendix_a",
        clauses=[
            ClauseSpec(
                clause_id="appendix_a_clause_01",
                document_id="appendix_a",
                order=1,
                heading_sv="Detaljer",
            )
        ],
    )
    relation = Relation(
        relation_id="rel_001",
        kind=RelationKind.REFERENCES,
        source="main_agreement:clause_02",
        target="appendix_a:appendix_a_clause_01",
        description_sv="Ersättningsklausulen hänvisar till bilaga A.",
    )
    return scenario.model_copy(
        update={
            "documents": [*scenario.documents, appendix],
            "relations": [relation],
        }
    )


def package_with_appendix(scenario: Scenario):
    package = make_generated_package(scenario)
    document = package.document("main_agreement")
    clauses = []
    for clause in document.clauses:
        text = clause.text
        if clause.clause_id == "clause_01":
            text = PARTY_TEXT
        elif clause.clause_id == "clause_02":
            text = text + " Se Bilaga A för detaljer."
        clauses.append(clause.model_copy(update={"text": text}))
    document = document.model_copy(
        update={"clauses": clauses, "references": ["appendix_a"]}
    )
    return package.model_copy(
        update={
            "documents": [document, package.document("appendix_a")],
        }
    )


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------


def test_clean_scenario_generates_accepted_questions() -> None:
    outcome, _gold, _rendered = generate(clean_scenario())
    assert len(outcome.accepted) >= 8
    categories = {question.category for question in outcome.accepted}
    assert QuestionCategory.FACT_EXTRACTION in categories
    assert QuestionCategory.ENTITY_IDENTIFICATION in categories
    assert QuestionCategory.COMPLETENESS in categories
    assert all(question.validated for question in outcome.accepted)
    assert all(not question.validated for question in outcome.rejected)
    assert all(question.source == "deterministic" for question in outcome.accepted)


def test_question_ids_are_unique_and_generation_is_deterministic() -> None:
    scenario = clean_scenario()
    first, _gold, _rendered = generate(scenario)
    second, _gold2, _rendered2 = generate(scenario)
    first_ids = [question.question_id for question in first.accepted]
    second_ids = [question.question_id for question in second.accepted]
    assert first_ids == second_ids
    assert len(set(first_ids)) == len(first_ids)
    assert [
        (question.question, question.answerability, question.difficulty)
        for question in first.accepted
    ] == [
        (question.question, question.answerability, question.difficulty)
        for question in second.accepted
    ]


def test_all_accepted_evidence_spans_are_valid() -> None:
    outcome, _gold, rendered = generate(clean_scenario())
    for question in outcome.accepted:
        for span in question.evidence:
            document = rendered[span.document_id]
            text = document.canonical_text
            assert 0 <= span.start < span.end <= len(text)
            assert text[span.start : span.end] == span.quote
            if span.clause_id is not None:
                clause_span = document.clause_spans[span.clause_id]
                assert clause_span.start <= span.start
                assert span.end <= clause_span.end


def test_fact_extraction_answers_match_verified_gold() -> None:
    outcome, gold, rendered = generate(clean_scenario())
    fact_questions = [
        question
        for question in outcome.accepted
        if question.category is QuestionCategory.FACT_EXTRACTION
    ]
    assert fact_questions
    for question in fact_questions:
        assert question.answerability is Answerability.ANSWERABLE
        assert len(question.required_facts) == 1
        gold_fact = golden(gold, question.required_facts[0])
        assert question.expected_answer.value == gold_fact.value
        span_keys = {(span.document_id, span.start, span.end) for span in question.evidence}
        gold_keys = {
            (span.document_id, span.start, span.end) for span in gold_fact.evidence
        }
        assert gold_keys <= span_keys
        assert evidence_text(question, rendered)


def test_english_templates_are_used_for_english_scenario() -> None:
    scenario = clean_scenario().model_copy(update={"language": Language.EN})
    outcome, _gold, _rendered = generate(scenario)
    assert outcome.accepted
    assert all(question.language is Language.EN for question in outcome.accepted)
    texts = " ".join(question.question for question in outcome.accepted)
    assert any(marker in texts for marker in ("What", "Which", "List", "Does", "Can"))
    assert any(
        question.category is QuestionCategory.FACT_EXTRACTION
        for question in outcome.accepted
    )


def test_max_questions_limits_accepted_questions() -> None:
    outcome, _gold, _rendered = generate(clean_scenario(), max_questions=3)
    assert len(outcome.accepted) == 3
    assert all(question.validated for question in outcome.accepted)


# ---------------------------------------------------------------------------
# Answerability levels
# ---------------------------------------------------------------------------


def test_level5_conflicting_evidence_question() -> None:
    outcome, _gold, _rendered = generate(conflicting_scenario())
    contradiction = [
        question
        for question in outcome.accepted
        if question.category is QuestionCategory.CONTRADICTION
    ]
    assert contradiction
    question = contradiction[0]
    assert question.answerability is Answerability.CONFLICTING_EVIDENCE
    assert question.difficulty == 5
    assert set(question.required_facts) == {
        "fact_notice_period",
        "fact_notice_period_amendment",
    }
    span_keys = {(span.document_id, span.start, span.end) for span in question.evidence}
    assert len(span_keys) >= 2
    assert set(question.expected_answer.values) == {3, 6}


def test_level6_insufficient_evidence_question() -> None:
    outcome, _gold, _rendered = generate(insufficient_scenario())
    insufficient = [
        question
        for question in outcome.accepted
        if question.category is QuestionCategory.INSUFFICIENT_EVIDENCE
    ]
    assert insufficient
    question = insufficient[0]
    assert question.answerability is Answerability.INSUFFICIENT_EVIDENCE
    assert question.difficulty == 6
    assert any(
        condition.startswith("anomaly:anomaly_missing")
        for condition in question.expected_answer.conditions
    )


def test_conditional_question_from_anomaly() -> None:
    scenario = make_consulting_scenario(difficulty=6)
    anomaly = Anomaly(
        anomaly_id="anomaly_condition",
        kind=AnomalyKind.EXCEPTION,
        fact_ids=["fact_notice_period"],
        answerability=Answerability.CONDITIONAL,
        description_sv="Uppsägningstiden gäller under förutsättning att avtalet fullföljs.",
    )
    scenario = scenario.model_copy(update={"anomalies": [anomaly]})
    outcome, _gold, _rendered = generate(scenario)
    conditional = [
        question
        for question in outcome.accepted
        if question.category is QuestionCategory.CONDITIONAL
    ]
    assert conditional
    question = conditional[0]
    assert question.answerability is Answerability.CONDITIONAL
    assert question.expected_answer.text
    assert any(
        condition.startswith("anomaly:anomaly_condition")
        for condition in question.expected_answer.conditions
    )


def test_absence_question_is_grounded_in_missing_documents() -> None:
    scenario = clean_scenario().model_copy(
        update={"missing_documents": ["appendix_b"]}
    )
    outcome, _gold, _rendered = generate(scenario)
    absence = [
        question
        for question in outcome.accepted
        if question.category is QuestionCategory.ABSENCE
    ]
    assert absence
    question = absence[0]
    assert question.answerability is Answerability.INSUFFICIENT_EVIDENCE
    assert "missing_document:appendix_b" in question.expected_answer.conditions
    assert question.validated


# ---------------------------------------------------------------------------
# Relations, completeness
# ---------------------------------------------------------------------------


def test_relation_question_is_supported_by_source_text() -> None:
    scenario = scenario_with_appendix()
    package = package_with_appendix(scenario)
    rendered = make_rendered_documents(scenario, package)
    gold = build_gold_set(scenario, package, rendered)
    outcome = generate_and_verify_questions(scenario, gold, rendered)
    relation = [
        question
        for question in outcome.accepted
        if question.category is QuestionCategory.RELATION
    ]
    assert relation
    question = relation[0]
    assert question.expected_answer.text == "Bilaga A"
    assert question.evidence
    assert question.validated


def test_completeness_all_parties_requirement() -> None:
    scenario = clean_scenario()
    outcome, _gold, _rendered = generate(scenario)
    party_questions = [
        question
        for question in outcome.accepted
        if question.category is QuestionCategory.COMPLETENESS
        and question.completeness is not None
        and question.completeness.kind == "all_parties"
    ]
    assert party_questions
    question = party_questions[0]
    assert question.answerability is Answerability.ANSWERABLE
    assert set(question.completeness.expected_ids) == {
        party.party_id for party in scenario.parties
    }
    assert set(question.expected_answer.values) == {
        party.name for party in scenario.parties
    }


def test_completeness_all_documents_requirement() -> None:
    scenario = scenario_with_appendix()
    package = package_with_appendix(scenario)
    rendered = make_rendered_documents(scenario, package)
    gold = build_gold_set(scenario, package, rendered)
    outcome = generate_and_verify_questions(scenario, gold, rendered)
    document_questions = [
        question
        for question in outcome.accepted
        if question.category is QuestionCategory.COMPLETENESS
        and question.completeness is not None
        and question.completeness.kind == "all_documents"
    ]
    assert document_questions
    question = document_questions[0]
    assert set(question.completeness.expected_ids) == {
        document.document_id for document in scenario.documents
    }
    assert {span.document_id for span in question.evidence} == {
        document.document_id for document in scenario.documents
    }


# ---------------------------------------------------------------------------
# Rejection when the canonical text does not support the answer
# ---------------------------------------------------------------------------


def test_unsupported_answer_is_rejected() -> None:
    scenario = clean_scenario()
    package = clean_package(scenario)
    rendered = make_rendered_documents(scenario, package)
    gold = build_gold_set(scenario, package, rendered)
    broken_facts = [
        fact.model_copy(update={"value": 99})
        if fact.fact_id == "fact_notice_period"
        else fact
        for fact in gold.facts
    ]
    broken_gold = gold.model_copy(update={"facts": broken_facts})
    outcome = generate_and_verify_questions(scenario, broken_gold, rendered)
    rejected = [
        question
        for question in outcome.rejected
        if question.category is QuestionCategory.FACT_EXTRACTION
        and question.required_facts == ["fact_notice_period"]
    ]
    assert rejected
    assert any("stöds inte" in note for note in rejected[0].validation_notes)
    assert all(
        not (
            question.category is QuestionCategory.FACT_EXTRACTION
            and question.required_facts == ["fact_notice_period"]
        )
        for question in outcome.accepted
    )
    assert all(question.validated for question in outcome.accepted)


def test_invalid_evidence_span_is_rejected() -> None:
    scenario = clean_scenario()
    package = clean_package(scenario)
    rendered = make_rendered_documents(scenario, package)
    gold = build_gold_set(scenario, package, rendered)
    valid_span = gold.fact("fact_notice_period").evidence[0]
    broken_span = EvidenceSpan(
        document_id=valid_span.document_id,
        clause_id=valid_span.clause_id,
        start=len(rendered["main_agreement"].canonical_text) + 100,
        end=len(rendered["main_agreement"].canonical_text) + 120,
        quote="utanför texten",
    )
    broken_facts = [
        fact.model_copy(update={"evidence": [broken_span]})
        if fact.fact_id == "fact_notice_period"
        else fact
        for fact in gold.facts
    ]
    broken_gold = gold.model_copy(update={"facts": broken_facts})
    outcome = generate_and_verify_questions(scenario, broken_gold, rendered)
    rejected = [
        question
        for question in outcome.rejected
        if question.category is QuestionCategory.FACT_EXTRACTION
        and question.required_facts == ["fact_notice_period"]
    ]
    assert rejected
    assert any("ligger utanför" in note for note in rejected[0].validation_notes)


def test_conditional_answerability_categories_are_consistent() -> None:
    outcome, _gold, _rendered = generate(clean_scenario())
    for question in outcome.accepted:
        if question.category is QuestionCategory.CONTRADICTION:
            assert question.answerability is Answerability.CONFLICTING_EVIDENCE
        if question.category in (
            QuestionCategory.ABSENCE,
            QuestionCategory.INSUFFICIENT_EVIDENCE,
        ):
            assert question.answerability is Answerability.INSUFFICIENT_EVIDENCE
        assert 1 <= question.difficulty <= 6


def test_answerable_questions_have_supported_answers() -> None:
    outcome, gold, rendered = generate(clean_scenario())
    gold_facts = {fact.fact_id: fact for fact in gold.facts}
    for question in outcome.accepted:
        if question.answerability is not Answerability.ANSWERABLE:
            continue
        assert question.evidence, question.question_id
        text = evidence_text(question, rendered)
        values = []
        if question.expected_answer.value is not None:
            values.append(question.expected_answer.value)
        values.extend(question.expected_answer.values)
        for value in values:
            covered = False
            for fact_id in question.required_facts:
                gold_fact = gold_facts[fact_id]
                if gold_fact.value == value and gold_fact.evidence:
                    covered = all(
                        any(
                            span.document_id == gold_span.document_id
                            and span.start <= gold_span.start
                            and gold_span.end <= span.end
                            for span in question.evidence
                        )
                        for gold_span in gold_fact.evidence
                    )
                    if covered:
                        break
            if not covered:
                assert str(value).lower() in text.lower() or str(value) in text
        if question.expected_answer.text:
            assert question.expected_answer.text.lower() in text.lower() or (
                question.category is QuestionCategory.RELATION
            )
