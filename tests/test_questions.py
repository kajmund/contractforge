"""Tests for deterministic golden-question generation and text-grounded verification."""

from __future__ import annotations

import json

import pytest
from factories import (
    make_consulting_scenario,
    make_fact,
    make_generated_package,
    make_rendered_documents,
)

from contractforge.config import Settings
from contractforge.generation.engine import DocumentGenerator
from contractforge.gold import build_gold_set
from contractforge.models import (
    Anomaly,
    AnomalyKind,
    Answerability,
    ClauseSpec,
    ContractType,
    DocKind,
    DocumentFormat,
    DocumentSpec,
    EvidenceSpan,
    FactKind,
    GoldSet,
    Language,
    QuestionCategory,
    Relation,
    RelationKind,
    Scenario,
    ScenarioRequest,
)
from contractforge.providers.mock import MockProvider
from contractforge.questions import (
    DOCUMENT_PREFIX,
    MISSING_PREFIX,
    generate_and_verify_questions,
)
from contractforge.rendering.canonical import render_package
from contractforge.scenarios.generator import ScenarioGenerator
from contractforge.scenarios.registry import load_registry
from contractforge.utils import find_span

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


# ---------------------------------------------------------------------------
# One validator: accepted by the generator == accepted by validate_dataset
# ---------------------------------------------------------------------------
#
# Regression gate for the original review finding: level 5-6 datasets were
# exported as approved but rejected by the project's own dataset validator,
# because question generation and dataset validation used two different rule
# sets. ``contractforge.validation.dataset.question_support_problems`` is now
# the only rule set; these tests prove the two code paths agree on real,
# exported datasets.

CONTRACT_TYPES = [contract.value for contract in ContractType]
HARD_DIFFICULTIES = [5, 6]


def _mock_settings():
    from contractforge.config import Settings

    return Settings.from_env().with_overrides(provider="mock", formats=[DocumentFormat.TXT])


def _request(contract_type: str, difficulty: int, seed: int) -> ScenarioRequest:
    return ScenarioRequest(
        contract_type=ContractType(contract_type),
        count=1,
        clauses=10,
        difficulty=difficulty,
        language=Language.SV,
        seed=seed,
    )


@pytest.mark.parametrize("contract_type", CONTRACT_TYPES)
@pytest.mark.parametrize("difficulty", HARD_DIFFICULTIES)
def test_accepted_questions_always_pass_dataset_validation(
    contract_type: str, difficulty: int, tmp_path
) -> None:
    """A generated level 5-6 dataset must be accepted by ``validate_dataset``."""

    from contractforge.export.loader import find_contract_dirs, load_contract_dir
    from contractforge.pipeline import generate_dataset
    from contractforge.validation.dataset import question_support_problems

    root = tmp_path / "dataset"
    outcome = generate_dataset(
        _mock_settings(),
        _request(contract_type, difficulty, seed=7),
        root,
        progress=lambda _message: None,
    )

    assert outcome.validation_report is not None
    failures = [
        (check.check_id, check.message)
        for check in outcome.validation_report.checks
        if check.status.value == "FAIL"
    ]
    assert outcome.validation_report.status.value == "PASS", failures

    # What was exported is exactly what the validator re-verified.
    assert outcome.results, "datasetet innehåller inget godkänt avtal"
    exported = sum(len(result.questions) for result in outcome.results)
    assert exported > 0
    assert outcome.stats.questions_total == exported
    assert outcome.stats.questions_validated == exported

    # Re-applying the shared rule set to every exported question is a no-op.
    for contract_dir in find_contract_dirs(root):
        loaded = load_contract_dir(contract_dir)
        gold_facts = (
            {fact.fact_id: fact for fact in loaded.gold.facts} if loaded.gold else {}
        )
        for question in loaded.questions:
            assert question.validated
            assert question_support_problems(question, loaded.rendered, gold_facts) == []


@pytest.mark.parametrize("contract_type", CONTRACT_TYPES)
@pytest.mark.parametrize("difficulty", HARD_DIFFICULTIES)
def test_rejected_questions_are_reported_and_never_exported(
    contract_type: str, difficulty: int, tmp_path
) -> None:
    """Question rejections are visible statistics, not silently dropped work."""

    from contractforge.pipeline import generate_dataset

    outcome = generate_dataset(
        _mock_settings(),
        _request(contract_type, difficulty, seed=11),
        tmp_path / "dataset",
        progress=lambda _message: None,
    )
    rejected_ids = {question.question_id for question in outcome.rejected_questions}
    exported_ids = {
        question.question_id for result in outcome.results for question in result.questions
    }
    assert not (rejected_ids & exported_ids), "en kasserad fråga får aldrig exporteras"
    assert outcome.stats.questions_rejected == len(outcome.rejected_questions)
    assert outcome.stats.questions_rejected == len(rejected_ids)
    for question in outcome.rejected_questions:
        assert not question.validated
        assert question.validation_notes


def test_dataset_validator_rejects_the_same_unsupported_claim_as_the_generator(tmp_path) -> None:
    """The inverse direction: a claim the generator rejects also fails validation.

    One question in a real dataset is rewritten to expect a value no document
    contains (hashes refreshed, exactly like ``contractforge generate-questions``
    does). The dataset must FAIL, with the same "stöds inte" wording the
    generator uses for the same rule.
    """

    import contractforge.questions as questions_module
    from contractforge.export.loader import find_contract_dirs, load_contract_dir
    from contractforge.pipeline import generate_dataset
    from contractforge.utils import atomic_write_json, sha256_file
    from contractforge.validation.dataset import question_support_problems, validate_dataset

    root = tmp_path / "dataset"
    generate_dataset(
        _mock_settings(),
        ScenarioRequest(
            contract_type=ContractType.CONSULTING,
            count=1,
            clauses=8,
            difficulty=5,
            language=Language.SV,
            seed=3,
        ),
        root,
        progress=lambda _message: None,
    )
    (contract_dir,) = find_contract_dirs(root)
    questions = load_contract_dir(contract_dir).questions
    target = next(
        question
        for question in questions
        if question.category is QuestionCategory.FACT_EXTRACTION
        and question.expected_answer.value is not None
    )
    tampered = target.model_copy(
        update={
            "expected_answer": target.expected_answer.model_copy(update={"value": 987654321})
        }
    )
    rewritten = [tampered if q.question_id == target.question_id else q for q in questions]
    atomic_write_json(
        contract_dir / "golden_questions.json",
        [question.model_dump(mode="json") for question in rewritten],
    )
    manifest = load_contract_dir(contract_dir)
    manifest_path = contract_dir / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for artifact in payload["artifacts"]:
        path = contract_dir / artifact["path"]
        artifact["sha256"] = sha256_file(path)
        artifact["size_bytes"] = path.stat().st_size
    atomic_write_json(manifest_path, payload)
    dataset_manifest_path = root / "dataset_manifest.json"
    dataset_payload = json.loads(dataset_manifest_path.read_text(encoding="utf-8"))
    for record in dataset_payload["contracts"]:
        record["manifest_sha256"] = sha256_file(manifest_path)
    atomic_write_json(dataset_manifest_path, dataset_payload)

    report = validate_dataset(root)
    failures = [check for check in report.checks if check.status.value == "FAIL"]
    assert report.status.value == "FAIL"
    assert any("stöds inte" in check.message for check in failures), [
        (check.check_id, check.message) for check in failures
    ]

    # The generator's own entry point reports the identical problem for the
    # identical question, because both call question_support_problems.
    gold_facts = (
        {fact.fact_id: fact for fact in manifest.gold.facts} if manifest.gold else {}
    )
    generator_problems = questions_module.verify_question_support(
        tampered, manifest.rendered, manifest.gold
    )
    shared_problems = question_support_problems(tampered, manifest.rendered, gold_facts)
    assert generator_problems == shared_problems
    assert generator_problems
    assert any("stöds inte" in problem for problem in generator_problems)


# ---------------------------------------------------------------------------
# F2: cross references must be observable in the canonical text
# ---------------------------------------------------------------------------

_MOCK_SETTINGS = Settings.from_env(
    {
        "CONTRACTFORGE_PROVIDER": "mock",
        "CONTRACTFORGE_FORMATS": "txt",
        "CONTRACTFORGE_CONCURRENCY": "1",
    }
)
_PACKAGE_CACHE: dict[tuple[str, int, int], tuple] = {}


@pytest.fixture(scope="module")
def package_contract():
    """Build scenario -> mock text -> rendered -> gold -> questions, cached."""

    registry = load_registry()
    generator = ScenarioGenerator(registry)
    document_generator = DocumentGenerator(MockProvider(), _MOCK_SETTINGS)

    def build(
        contract_type: ContractType,
        difficulty: int,
        seed: int = 7,
    ):
        key = (contract_type.value, difficulty, seed)
        if key not in _PACKAGE_CACHE:
            scenario = generator.generate(
                ScenarioRequest(
                    contract_type=contract_type,
                    difficulty=difficulty,
                    seed=seed,
                    clauses=10,
                    package=True,
                    language=Language.SV,
                )
            )
            outcome = document_generator.generate(scenario)
            rendered = render_package(scenario, outcome.package)
            gold = build_gold_set(scenario, outcome.package, rendered)
            questions = generate_and_verify_questions(scenario, gold, rendered)
            _PACKAGE_CACHE[key] = (scenario, gold, rendered, questions)
        return _PACKAGE_CACHE[key]

    return build


def _resolve_relation_token(
    scenario: Scenario, token: str
) -> tuple[str | None, str | None]:
    if token.startswith("fact:"):
        fact_id = token[len("fact:") :]
        for document in scenario.documents:
            for clause in document.clauses:
                if fact_id in clause.fact_ids:
                    return document.document_id, clause.clause_id
        return None, None
    if token in scenario.missing_documents:
        return token, None
    if ":" in token:
        document_id, clause_id = token.split(":", 1)
        scenario.document(document_id)
        scenario.clause(clause_id)
        return document_id, clause_id
    if any(document.document_id == token for document in scenario.documents):
        return token, None
    clause = scenario.clause(token)
    return clause.document_id, clause.clause_id


def relations_without_text_support(scenario: Scenario, gold: GoldSet) -> list[str]:
    """Reproducible inventory: relations whose target is never named in text.

    A relation is text-supported when a gold reference for the same resolved
    source/target pair carries evidence. Declared missing documents are exempt:
    they have no canonical text by definition.
    """

    keys = {
        (
            reference.source_document_id,
            reference.source_clause_id,
            reference.target_document_id,
            reference.target_clause_id,
        )
        for reference in gold.references
        if reference.evidence
    }
    unsupported: list[str] = []
    for relation in scenario.relations:
        if relation.target in scenario.missing_documents:
            continue
        source_document, source_clause = _resolve_relation_token(scenario, relation.source)
        target_document, target_clause = _resolve_relation_token(scenario, relation.target)
        if (source_document, source_clause, target_document, target_clause) in keys:
            continue
        # Document-level gold references may carry clause_id=None.
        if any(
            (document_id, target_document, clause_id) in keys
            for document_id, clause_id in [(source_document, target_clause)]
        ):
            continue
        unsupported.append(relation.relation_id)
    return unsupported


@pytest.mark.parametrize("contract_type", list(ContractType))
@pytest.mark.parametrize("difficulty", [1, 2, 3, 4, 5, 6])
def test_relations_have_text_support(package_contract, contract_type, difficulty):
    """No in-package relation may exist only as metadata (review finding F2)."""

    scenario, gold, _rendered, _questions = package_contract(contract_type, difficulty)
    assert relations_without_text_support(scenario, gold) == []
    for reference in gold.references:
        if not reference.evidence:
            assert reference.target_document_id in scenario.missing_documents


@pytest.mark.parametrize("contract_type", list(ContractType))
@pytest.mark.parametrize("difficulty", [3, 4, 5, 6])
def test_relation_questions_cite_the_target_title(
    package_contract, contract_type, difficulty
):
    scenario, _gold, rendered, outcome = package_contract(contract_type, difficulty)
    relation_questions = [
        question
        for question in outcome.accepted
        if question.category is QuestionCategory.RELATION
    ]
    assert relation_questions
    for question in relation_questions:
        assert question.evidence
        target_ids = [
            condition[len(DOCUMENT_PREFIX) :]
            for condition in question.expected_answer.conditions
            if condition.startswith(DOCUMENT_PREFIX)
        ]
        assert len(target_ids) == 1
        target = rendered.get(target_ids[0])
        assert target is not None
        assert question.expected_answer.text == target.title
        # The cited evidence must contain the target document title (document
        # references) or the target clause heading (clause references), as the
        # gold reference evidence is a narrow span on exactly that mention.
        target_spec = scenario.document(target_ids[0])
        headings = [
            heading
            for clause in target_spec.clauses
            for heading in (clause.heading_sv, clause.heading_en)
            if heading
        ]
        slices = [
            rendered[span.document_id].canonical_text[span.start : span.end]
            for span in question.evidence
            if span.document_id in rendered
        ]
        assert any(
            find_span(slice_text, target.title) is not None
            or any(find_span(slice_text, heading) is not None for heading in headings)
            for slice_text in slices
        )
        # The evidence lives in the *source* document, not in the target.
        assert all(span.document_id != target.document_id for span in question.evidence)


@pytest.mark.parametrize("contract_type", list(ContractType))
@pytest.mark.parametrize("difficulty", [3, 4, 5, 6])
def test_relation_questions_are_deduplicated(package_contract, contract_type, difficulty):
    _scenario, _gold, rendered, outcome = package_contract(contract_type, difficulty)
    relation_questions = [
        question
        for question in outcome.accepted
        if question.category is QuestionCategory.RELATION
    ]
    pairs = set()
    for question in relation_questions:
        target_id = next(
            condition[len(DOCUMENT_PREFIX) :]
            for condition in question.expected_answer.conditions
            if condition.startswith(DOCUMENT_PREFIX)
        )
        source_documents = frozenset(
            span.document_id
            for span in question.evidence
            if span.document_id in rendered
        )
        pair = (source_documents, target_id)
        assert pair not in pairs, question.question_id
        pairs.add(pair)
        assert target_id not in source_documents


@pytest.mark.parametrize("contract_type", list(ContractType))
@pytest.mark.parametrize("difficulty", [3, 4, 5, 6])
def test_cross_document_comparison_questions(package_contract, contract_type, difficulty):
    """Package questions combine two documents (F2/F10).

    The generator keeps one distractor base fact in the main agreement and the
    distractor itself in the appendix, so every package always has at least one
    comparably-typed value on each side. The question must name both documents
    and cite evidence from both, otherwise it would be answerable from one
    document alone.
    """

    _scenario, _gold, rendered, outcome = package_contract(contract_type, difficulty)
    comparison_questions = [
        question
        for question in outcome.accepted
        if question.category is QuestionCategory.COMPARISON
    ]
    cross_document = [
        question
        for question in comparison_questions
        if "Jämför dokumenten" in question.question
    ]
    assert cross_document, [q.question for q in comparison_questions]
    for question in cross_document:
        evidence_documents = {span.document_id for span in question.evidence}
        assert len(evidence_documents) >= 2
        mentioned = [
            document_id
            for document_id in rendered
            if rendered[document_id].title.casefold() in question.question.casefold()
        ]
        assert len(mentioned) >= 2
        assert set(mentioned).issubset(evidence_documents)
        assert any(
            rendered[document_id].title.casefold() in question.question.casefold()
            for document_id in evidence_documents
        )


@pytest.mark.parametrize("contract_type", list(ContractType))
def test_level_five_has_conflicting_evidence_question(package_contract, contract_type):
    _scenario, _gold, _rendered, outcome = package_contract(contract_type, 5)
    assert any(
        question.answerability is Answerability.CONFLICTING_EVIDENCE
        for question in outcome.accepted
    )


@pytest.mark.parametrize("contract_type", list(ContractType))
def test_level_six_has_conditional_or_insufficient_question(package_contract, contract_type):
    _scenario, _gold, _rendered, outcome = package_contract(contract_type, 6)
    answerabilities = {question.answerability for question in outcome.accepted}
    assert (
        Answerability.INSUFFICIENT_EVIDENCE in answerabilities
        or Answerability.CONDITIONAL in answerabilities
    )


# ---------------------------------------------------------------------------
# F2 regression: cross-document evidence must name the target, and references
# to declared missing documents must stay unsupported (level 6).
# ---------------------------------------------------------------------------


def _reference_names_target(
    scenario: Scenario,
    rendered,
    reference,
) -> bool:
    target_document = scenario.document(reference.target_document_id)
    names = [target_document.title_sv, target_document.title_en]
    if reference.target_clause_id:
        target_clause = scenario.clause(reference.target_clause_id)
        names.extend([target_clause.heading_sv, target_clause.heading_en])
    names = [name for name in names if name and name.strip()]
    slices = [
        rendered[span.document_id].canonical_text[span.start : span.end]
        for span in reference.evidence
        if span.document_id in rendered
    ]
    return bool(slices) and all(
        any(find_span(slice_text, name) is not None for name in names)
        for slice_text in slices
    )


@pytest.mark.parametrize("difficulty", [5, 6])
def test_employment_cross_document_reference_evidence_names_target(
    package_contract, difficulty
):
    """The concrete review case: amendment_1 -> main_agreement (employment)."""

    scenario, gold, rendered, _questions = package_contract(
        ContractType.EMPLOYMENT, difficulty
    )
    references = [
        reference
        for reference in gold.references
        if reference.source_document_id == "amendment_1"
        and reference.target_document_id == "main_agreement"
    ]
    assert references
    for reference in references:
        assert reference.evidence
        assert _reference_names_target(scenario, rendered, reference), reference


@pytest.mark.parametrize("contract_type", list(ContractType))
@pytest.mark.parametrize("difficulty", [5, 6])
def test_cross_document_references_name_their_target(
    package_contract, contract_type, difficulty
):
    """Every cross-document in-package reference must name where it points."""

    scenario, gold, rendered, _questions = package_contract(contract_type, difficulty)
    cross_document = [
        reference
        for reference in gold.references
        if reference.source_document_id != reference.target_document_id
        and reference.target_document_id not in scenario.missing_documents
    ]
    assert cross_document
    for reference in cross_document:
        assert reference.evidence
        assert _reference_names_target(scenario, rendered, reference), reference


@pytest.mark.parametrize("contract_type", list(ContractType))
def test_missing_document_references_are_unsupported_by_design(
    package_contract, contract_type
):
    """Level 6 references to declared missing documents have no evidence.

    This is intentional: the document does not exist, so there is no canonical
    text to cite. The relation must not become a relation question; instead the
    missing document yields an ``INSUFFICIENT_EVIDENCE`` question with a
    ``missing_document:`` premise.
    """

    scenario, gold, _rendered, outcome = package_contract(contract_type, 6)
    missing_references = [
        reference
        for reference in gold.references
        if reference.target_document_id in scenario.missing_documents
    ]
    assert missing_references, scenario.missing_documents
    for reference in missing_references:
        assert reference.evidence == []
        assert reference.target_document_id in scenario.missing_documents

    relation_answers = {
        question.expected_answer.text
        for question in outcome.accepted
        if question.category is QuestionCategory.RELATION
    }
    assert not relation_answers.intersection(scenario.missing_documents)

    insufficient = [
        question
        for question in outcome.accepted
        if question.answerability is Answerability.INSUFFICIENT_EVIDENCE
        and any(
            condition.startswith(MISSING_PREFIX)
            for condition in question.expected_answer.conditions
        )
    ]
    assert insufficient
    for question in insufficient:
        missing_ids = {
            condition[len(MISSING_PREFIX) :]
            for condition in question.expected_answer.conditions
            if condition.startswith(MISSING_PREFIX)
        }
        assert missing_ids
        assert missing_ids.issubset(scenario.missing_documents)
