"""Tests for deterministic verification, semantic normalisation and dataset validation."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from factories import (
    make_config_snapshot,
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
    ArtifactRecord,
    CheckStatus,
    ClauseMode,
    CostSummary,
    DatasetContractRecord,
    DatasetManifest,
    DatasetStats,
    DocumentRecord,
    DocumentStatus,
    FactKind,
    GeneratedClause,
    GoldenQuestion,
    GoldSet,
    LoadedContract,
    Manifest,
    PackageVerification,
    RenderedDocument,
    Scenario,
    TextSpan,
    VerificationSummary,
)
from contractforge.questions import generate_and_verify_questions
from contractforge.utils import read_json, sha256_file, sha256_text
from contractforge.validation import semantic, validate_package
from contractforge.validation.dataset import validate_dataset

PARTY_TEXT = "Parterna Nordvik Teknik AB och Sundberg Konsult AB ingår avtalet."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def clean_scenario() -> Scenario:
    return make_consulting_scenario()


def clean_package(scenario: Scenario) -> object:
    package = make_generated_package(scenario)
    document = package.document("main_agreement")
    clauses = list(document.clauses)
    clauses[0] = clauses[0].model_copy(update={"text": PARTY_TEXT})
    document = document.model_copy(update={"clauses": clauses})
    return package.model_copy(update={"documents": [document]})


def bundle(difficulty: int = 1):
    scenario = make_consulting_scenario(difficulty)
    package = clean_package(scenario)
    rendered = make_rendered_documents(scenario, package)
    return scenario, package, rendered


def set_clause_text(package, clause_id: str, text: str):
    document = package.document("main_agreement")
    clauses = [
        clause.model_copy(update={"text": text}) if clause.clause_id == clause_id else clause
        for clause in document.clauses
    ]
    return package.model_copy(
        update={"documents": [document.model_copy(update={"clauses": clauses})]}
    )


def all_checks(verification: PackageVerification) -> list:
    checks = list(verification.checks)
    for document in verification.documents:
        checks.extend(document.checks)
    return checks


def fail_checks(verification: PackageVerification) -> list:
    return [check for check in all_checks(verification) if check.status is CheckStatus.FAIL]


def check_with(verification: PackageVerification, check_id: str):
    matches = [check for check in all_checks(verification) if check.check_id == check_id]
    assert len(matches) == 1, f"expected exactly one {check_id}, got {len(matches)}"
    return matches[0]


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


def intentionally_missing_scenario() -> Scenario:
    scenario = make_consulting_scenario(difficulty=6)
    anomaly = Anomaly(
        anomaly_id="anomaly_missing",
        kind=AnomalyKind.MISSING_INFORMATION,
        fact_ids=["fact_notice_period"],
        answerability=Answerability.INSUFFICIENT_EVIDENCE,
        description_sv="Uppsägningstiden regleras i ett saknat sidodokument.",
    )
    return scenario.model_copy(
        update={"anomalies": [anomaly], "missing_documents": ["side_letter_2026"]}
    )


def scenario_with_text_fact() -> Scenario:
    scenario = make_consulting_scenario()
    fact = make_fact(
        "fact_governing_law",
        "svensk lag",
        kind=FactKind.TEXT,
        label_sv="Tillämplig lag",
        explicit_surfaces=["svensk lag"],
        paraphrase_surfaces=["lagstiftningen i Sverige"],
        keywords_sv=["lag"],
    )
    document = scenario.documents[0]
    clauses = list(document.clauses)
    clauses[0] = clauses[0].model_copy(
        update={"fact_ids": ["fact_governing_law"], "mode": ClauseMode.PARAPHRASE}
    )
    document = document.model_copy(update={"clauses": clauses})
    return scenario.model_copy(
        update={"facts": [*scenario.facts, fact], "documents": [document]}
    )


def package_with_text_fact(scenario: Scenario):
    package = make_generated_package(scenario)
    document = package.document("main_agreement")
    clauses = list(document.clauses)
    clauses[0] = clauses[0].model_copy(
        update={"text": PARTY_TEXT + " Svensk lag gäller för avtalet."}
    )
    document = document.model_copy(update={"clauses": clauses})
    return package.model_copy(update={"documents": [document]})


# ---------------------------------------------------------------------------
# Happy path + explicit facts
# ---------------------------------------------------------------------------


def test_clean_package_is_accepted() -> None:
    scenario, package, rendered = bundle()
    verification = validate_package(scenario, package, rendered)
    assert verification.status is DocumentStatus.ACCEPTED
    assert fail_checks(verification) == []
    assert verification.stats.documents_total == 1
    assert verification.stats.documents_accepted == 1
    assert verification.stats.documents_rejected == 0
    total_checks = len(verification.checks) + sum(
        len(document.checks) for document in verification.documents
    )
    assert verification.stats.checks_total == total_checks
    assert verification.stats.checks_failed == 0


def test_missing_explicit_surface_fails_with_actionable_feedback() -> None:
    scenario, package, rendered = bundle()
    package = set_clause_text(
        package, "clause_03", "Uppsägningstiden regleras i separat avtal."
    )
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    assert verification.status is DocumentStatus.REJECTED
    check_id = "document.main_agreement.clause.clause_03.fact.fact_notice_period"
    check = check_with(verification, check_id)
    assert check.status is CheckStatus.FAIL
    assert "fact_notice_period" in check.message
    assert "tre (3) månader" in check.message
    assert "clause_03" in check.message
    assert verification.documents[0].status is DocumentStatus.REJECTED
    assert verification.documents[0].reasons


def test_wrong_value_is_a_semantic_fail_with_found_values() -> None:
    scenario, package, rendered = bundle()
    package = set_clause_text(
        package, "clause_03", "Uppsägningstiden är sex (6) månader."
    )
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    semantic_check = check_with(
        verification,
        "document.main_agreement.clause.clause_03.fact.fact_notice_period.semantic",
    )
    assert semantic_check.status is CheckStatus.FAIL
    assert "operativt värde" in semantic_check.message
    assert "sex (6) månader" in semantic_check.message
    assert "3 months" in semantic_check.message


def test_correct_value_plus_distractor_only_warns() -> None:
    scenario, package, rendered = bundle()
    package = set_clause_text(
        package,
        "clause_03",
        "Uppsägningstiden är tre (3) månader, alternativt sex (6) månader.",
    )
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    semantic_check = check_with(
        verification,
        "document.main_agreement.clause.clause_03.fact.fact_notice_period.semantic",
    )
    assert semantic_check.status is CheckStatus.WARN
    assert "sex (6) månader" in semantic_check.message
    assert verification.status is DocumentStatus.ACCEPTED


# ---------------------------------------------------------------------------
# Paraphrase mode
# ---------------------------------------------------------------------------


def test_paraphrase_mode_accepts_quarter_paraphrase() -> None:
    scenario = make_consulting_scenario()
    document = scenario.documents[0]
    clauses = list(document.clauses)
    clauses[2] = clauses[2].model_copy(update={"mode": ClauseMode.PARAPHRASE})
    scenario = scenario.model_copy(
        update={"documents": [document.model_copy(update={"clauses": clauses})]}
    )
    package = clean_package(scenario)
    package = set_clause_text(package, "clause_03", "Uppsägningstiden är ett kvartal.")
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    check = check_with(
        verification,
        "document.main_agreement.clause.clause_03.fact.fact_notice_period",
    )
    assert check.status is CheckStatus.PASS
    assert "ett kvartal" in check.message
    assert verification.status is DocumentStatus.ACCEPTED


def test_paraphrase_mode_with_only_explicit_form_warns() -> None:
    scenario = scenario_with_text_fact()
    package = package_with_text_fact(scenario)
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    check = check_with(
        verification,
        "document.main_agreement.clause.clause_01.fact.fact_governing_law",
    )
    assert check.status is CheckStatus.WARN
    assert "parafrasläge" in check.message
    assert verification.status is DocumentStatus.ACCEPTED


# ---------------------------------------------------------------------------
# Structure, duplicates, references, entities
# ---------------------------------------------------------------------------


def test_entity_missing_from_text_fails() -> None:
    scenario, package, rendered = bundle()
    package = set_clause_text(package, "clause_01", "Parterna ingår avtalet.")
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    assert verification.status is DocumentStatus.REJECTED
    for party_id, name in (("party_a", "Nordvik Teknik AB"), ("party_b", "Sundberg Konsult AB")):
        check = check_with(verification, f"package.entity.{party_id}")
        assert check.status is CheckStatus.FAIL
        assert name in check.message


def test_missing_clause_fails_structure_check() -> None:
    scenario, package, rendered = bundle()
    document = package.document("main_agreement")
    clauses = [clause for clause in document.clauses if clause.clause_id != "clause_04"]
    package = package.model_copy(
        update={"documents": [document.model_copy(update={"clauses": clauses})]}
    )
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    assert verification.status is DocumentStatus.REJECTED
    check = check_with(verification, "document.main_agreement.structure.clause_ids")
    assert check.status is CheckStatus.FAIL
    assert "clause_04" in check.message


def test_extra_clause_fails_structure_check() -> None:
    scenario, package, rendered = bundle()
    document = package.document("main_agreement")
    extra = GeneratedClause(
        clause_id="clause_99",
        document_id="main_agreement",
        heading="Ovälkommen klausul",
        text="Detta ska inte finnas.",
    )
    package = package.model_copy(
        update={
            "documents": [
                document.model_copy(update={"clauses": [*document.clauses, extra]})
            ]
        }
    )
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    check = check_with(verification, "document.main_agreement.structure.clause_ids")
    assert check.status is CheckStatus.FAIL
    assert "clause_99" in check.message


def test_wrong_clause_order_fails() -> None:
    scenario, package, rendered = bundle()
    document = package.document("main_agreement")
    clauses = list(document.clauses)
    clauses[2], clauses[3] = clauses[3], clauses[2]
    package = package.model_copy(
        update={"documents": [document.model_copy(update={"clauses": clauses})]}
    )
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    check = check_with(verification, "document.main_agreement.structure.clause_order")
    assert check.status is CheckStatus.FAIL
    assert "klausulordningen" in check.message


def test_duplicate_headings_fail() -> None:
    scenario, package, rendered = bundle()
    document = package.document("main_agreement")
    clauses = [
        clause.model_copy(update={"heading": "Uppsägningstid"})
        if clause.clause_id == "clause_04"
        else clause
        for clause in document.clauses
    ]
    package = package.model_copy(
        update={"documents": [document.model_copy(update={"clauses": clauses})]}
    )
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    check = check_with(verification, "document.main_agreement.duplicates.headings")
    assert check.status is CheckStatus.FAIL
    assert "clause_03" in check.message and "clause_04" in check.message


def test_duplicate_clause_text_fails() -> None:
    scenario, package, rendered = bundle()
    document = package.document("main_agreement")
    source = next(clause for clause in document.clauses if clause.clause_id == "clause_03")
    clauses = [
        clause.model_copy(update={"text": source.text})
        if clause.clause_id == "clause_04"
        else clause
        for clause in document.clauses
    ]
    package = package.model_copy(
        update={"documents": [document.model_copy(update={"clauses": clauses})]}
    )
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    check = check_with(
        verification, "document.main_agreement.duplicates.clause_texts"
    )
    assert check.status is CheckStatus.FAIL


def test_reference_to_unknown_document_fails() -> None:
    scenario, package, rendered = bundle()
    document = package.document("main_agreement")
    package = package.model_copy(
        update={
            "documents": [document.model_copy(update={"references": ["appendix_z"]})]
        }
    )
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    check = check_with(verification, "document.main_agreement.references")
    assert check.status is CheckStatus.FAIL
    assert "appendix_z" in check.message


def test_reference_to_declared_missing_document_passes() -> None:
    scenario, package, rendered = bundle()
    scenario = scenario.model_copy(update={"missing_documents": ["appendix_b"]})
    document = package.document("main_agreement")
    package = package.model_copy(
        update={
            "documents": [document.model_copy(update={"references": ["appendix_b"]})]
        }
    )
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    check = check_with(verification, "document.main_agreement.references")
    assert check.status is CheckStatus.PASS
    assert "appendix_b" in check.message
    assert verification.status is DocumentStatus.ACCEPTED


def test_missing_rendered_document_is_rejected() -> None:
    scenario, package, rendered = bundle()
    rendered.pop("main_agreement")
    verification = validate_package(scenario, package, rendered)
    assert verification.status is DocumentStatus.REJECTED
    check = check_with(
        verification, "document.main_agreement.rendered.present"
    )
    assert check.status is CheckStatus.FAIL
    assert verification.stats.documents_rejected == 1


def test_missing_clause_span_fails() -> None:
    scenario, package, rendered = bundle()
    document = rendered["main_agreement"]
    spans = {
        clause_id: span
        for clause_id, span in document.clause_spans.items()
        if clause_id != "clause_05"
    }
    rendered["main_agreement"] = document.model_copy(update={"clause_spans": spans})
    verification = validate_package(scenario, package, rendered)
    check = check_with(verification, "document.main_agreement.rendered.spans")
    assert check.status is CheckStatus.FAIL
    assert "clause_05" in check.message


def test_reflowed_clause_text_fails_text_presence() -> None:
    scenario, package, rendered = bundle()
    reference = rendered["main_agreement"]
    generated = package.document("main_agreement")
    lines = [
        "SYNTETISK TESTDATA – ContractForge",
        f"contract_id: {scenario.contract_id}",
        "document_id: main_agreement",
        "",
        reference.title,
        "",
    ]
    text = "\n".join(lines) + "\n"
    spans: dict[str, TextSpan] = {}
    for index, clause in enumerate(generated.clauses, start=1):
        block = f"{index}. {clause.heading}\nFelaktig OCH reflowad text.\n"
        start = len(text)
        text += block
        spans[clause.clause_id] = TextSpan(
            document_id="main_agreement",
            clause_id=clause.clause_id,
            start=start,
            end=start + len(block),
        )
        text += "\n"
    rendered["main_agreement"] = RenderedDocument(
        document_id="main_agreement",
        kind=reference.kind,
        title=reference.title,
        language=reference.language,
        canonical_text=text,
        clause_spans=spans,
    )
    verification = validate_package(scenario, package, rendered)
    assert verification.status is DocumentStatus.REJECTED
    text_checks = [
        check
        for check in all_checks(verification)
        if check.check_id.endswith(".text_present")
    ]
    assert text_checks and all(check.status is CheckStatus.FAIL for check in text_checks)


def test_expected_fact_coverage_fails_for_unsupported_fact() -> None:
    scenario, package, rendered = bundle()
    extra = make_fact(
        "fact_secret_sauce",
        "hemlig sås",
        kind=FactKind.TEXT,
        explicit_surfaces=["hemlig sås"],
        keywords_sv=["sås"],
    )
    document = scenario.documents[0]
    clauses = list(document.clauses)
    clauses[1] = clauses[1].model_copy(
        update={"fact_ids": [*clauses[1].fact_ids, "fact_secret_sauce"]}
    )
    scenario = scenario.model_copy(
        update={
            "facts": [*scenario.facts, extra],
            "documents": [document.model_copy(update={"clauses": clauses})],
        }
    )
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    check = check_with(verification, "package.expected_counts.facts")
    assert check.status is CheckStatus.FAIL
    assert "fact_secret_sauce" in check.message
    assert verification.status is DocumentStatus.REJECTED


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------


def test_intentional_missing_fact_is_skipped() -> None:
    scenario = intentionally_missing_scenario()
    package = clean_package(scenario)
    package = set_clause_text(
        package,
        "clause_03",
        "Parterna har inte reglerat uppsägningstiden i detta avtal.",
    )
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    main_check = check_with(
        verification,
        "document.main_agreement.clause.clause_03.fact.fact_notice_period",
    )
    assert main_check.status is CheckStatus.SKIP
    assert "avsiktligt oreglerat" in main_check.message
    assert check_with(verification, "package.anomaly.anomaly_missing").status is CheckStatus.PASS
    assert verification.status is DocumentStatus.ACCEPTED


def test_conflicting_evidence_anomaly_present_passes() -> None:
    scenario = conflicting_scenario()
    package = clean_package(scenario)
    rendered = make_rendered_documents(scenario, package)
    verification = validate_package(scenario, package, rendered)
    assert check_with(verification, "package.anomaly.anomaly_conflict").status is CheckStatus.PASS
    semantic_check = check_with(
        verification,
        "document.main_agreement.clause.clause_03.fact.fact_notice_period.semantic",
    )
    assert semantic_check.status is CheckStatus.WARN
    assert verification.status is DocumentStatus.ACCEPTED


# ---------------------------------------------------------------------------
# Gold verification
# ---------------------------------------------------------------------------


def test_gold_evidence_quote_mismatch_fails() -> None:
    scenario, package, rendered = bundle()
    gold = build_gold_set(scenario, package, rendered)
    broken_facts = []
    for gold_fact in gold.facts:
        if gold_fact.fact_id == "fact_notice_period":
            spans = [
                span.model_copy(update={"quote": "felaktig quote"})
                for span in gold_fact.evidence
            ]
            broken_facts.append(gold_fact.model_copy(update={"evidence": spans}))
        else:
            broken_facts.append(gold_fact)
    broken_gold = gold.model_copy(update={"facts": broken_facts})
    verification = validate_package(scenario, package, rendered, gold=broken_gold)
    check = check_with(
        verification, "gold.fact.fact_notice_period.evidence"
    )
    assert check.status is CheckStatus.FAIL
    assert "matchar inte slice" in check.message


def test_gold_value_mismatch_fails() -> None:
    scenario, package, rendered = bundle()
    gold = build_gold_set(scenario, package, rendered)
    broken_facts = [
        gold_fact.model_copy(update={"value": 99})
        if gold_fact.fact_id == "fact_notice_period"
        else gold_fact
        for gold_fact in gold.facts
    ]
    broken_gold = gold.model_copy(update={"facts": broken_facts})
    verification = validate_package(scenario, package, rendered, gold=broken_gold)
    check = check_with(verification, "gold.fact.fact_notice_period.value")
    assert check.status is CheckStatus.FAIL
    assert "scenariot" in check.message


def test_gold_missing_fact_fails() -> None:
    scenario, package, rendered = bundle()
    gold = build_gold_set(scenario, package, rendered)
    broken_gold = gold.model_copy(
        update={
            "facts": [
                gold_fact
                for gold_fact in gold.facts
                if gold_fact.fact_id != "fact_notice_period"
            ]
        }
    )
    verification = validate_package(scenario, package, rendered, gold=broken_gold)
    check = check_with(verification, "gold.fact.fact_notice_period.present")
    assert check.status is CheckStatus.FAIL
    assert "facit får inte utelämna sanningen" in check.message


def test_gold_built_from_package_validates_clean() -> None:
    scenario, package, rendered = bundle()
    gold = build_gold_set(scenario, package, rendered)
    verification = validate_package(scenario, package, rendered, gold=gold)
    assert fail_checks(verification) == []
    assert verification.status is DocumentStatus.ACCEPTED


# ---------------------------------------------------------------------------
# Stability and advisory judge
# ---------------------------------------------------------------------------


def test_check_ids_and_statuses_are_stable() -> None:
    def signature(verification: PackageVerification):
        return [(check.check_id, check.status) for check in all_checks(verification)]

    scenario, package, rendered = bundle()
    first = validate_package(scenario, package, rendered)
    second = validate_package(scenario, package, rendered)
    assert signature(first) == signature(second)
    broken = set_clause_text(package, "clause_03", "Uppsägningstiden saknas.")
    rendered_broken = make_rendered_documents(scenario, broken)
    third = validate_package(scenario, broken, rendered_broken)
    first_ids = {check_id for check_id, _status in signature(first)}
    third_ids = {check.check_id for check in all_checks(third)}
    assert first_ids <= third_ids


def test_advisory_judge_only_produces_warns() -> None:
    class Judge:
        def review(self, request):  # noqa: ANN001 - protocol argument
            return ["möjligen otydlig formulering"]

    scenario, package, rendered = bundle()
    fact = scenario.fact("fact_notice_period")
    results = semantic.advisory_llm_checks(
        Judge(),
        contract_id=scenario.contract_id,
        document_id="main_agreement",
        clause_id="clause_03",
        fact=fact,
        clause_text="Uppsägningstiden är tre (3) månader.",
    )
    assert results
    assert all(result.status is CheckStatus.WARN for result in results)
    assert all(result.details["independent"] is False for result in results)
    verification = validate_package(scenario, package, rendered)
    assert not [
        check for check in all_checks(verification) if check.check_id.startswith("semantic.judge")
    ]


# ---------------------------------------------------------------------------
# Deterministic semantic normalisation
# ---------------------------------------------------------------------------


def test_duration_units_normalise_to_months_including_quarter() -> None:
    def canonical(fact):
        return semantic.canonical_quantity_for_fact(fact)

    quarter = canonical(
        make_fact("f", 3, kind=FactKind.DURATION, unit="months")
    )
    text_quarter = [
        quantity
        for quantity in semantic.extract_quantities("Uppsägningstiden är ett kvartal.")
        if quantity.unit == semantic.DURATION
    ]
    assert text_quarter and text_quarter[0].value == quarter.value
    assert quarter.value == 3

    year = canonical(make_fact("f", 1, kind=FactKind.DURATION, unit="years"))
    year_text = [
        quantity
        for quantity in semantic.extract_quantities("Avtalet gäller i 12 månader.")
        if quantity.unit == semantic.DURATION
    ]
    assert year_text and year_text[0].value == year.value

    days = canonical(make_fact("f", 30, kind=FactKind.DURATION, unit="days"))
    days_text = [
        quantity
        for quantity in semantic.extract_quantities("Betalning inom 30 dagar.")
        if quantity.unit == semantic.DURATION
    ]
    assert days_text and days_text[0].value == days.value


def test_integer_with_unit_maps_to_duration_and_currency() -> None:
    days = make_fact("f", 30, kind=FactKind.INTEGER, unit="days")
    assert semantic.canonical_quantity_for_fact(days).unit == semantic.DURATION
    money = make_fact("f", 40250, kind=FactKind.INTEGER, currency="SEK")
    assert semantic.canonical_quantity_for_fact(money).unit == semantic.MONEY
    money_text = semantic.extract_quantities("månadslön om 40 250 kronor")
    assert any(quantity.unit == semantic.MONEY and quantity.value == 40250 for quantity in money_text)


def test_currency_synonyms_and_compound_number_words() -> None:
    money = make_fact("f", 450000, kind=FactKind.MONEY, currency="EUR")
    analysis = semantic.analyze_fact(money, "årlig licensavgift 450 000 euro")
    assert analysis.semantic_span is not None
    compound = semantic.extract_quantities(
        "avgiften uppgår till fyrtiotusentvåhundrafemtio kronor"
    )
    assert any(
        quantity.unit == semantic.MONEY and quantity.value == 40250
        for quantity in compound
    )


def test_locale_date_and_weekday_forms_are_semantic_hits() -> None:
    fact = make_fact("f", "2026-02-01", kind=FactKind.DATE)
    swedish = semantic.analyze_fact(fact, "tillträdesdag den 1 februari 2026")
    english = semantic.analyze_fact(fact, "the commencement date is 1 February 2026")
    assert swedish.semantic_span is not None
    assert english.semantic_span is not None


def test_llm_judge_is_never_independent_facit() -> None:
    class Judge:
        def review(self, request):  # noqa: ANN001 - protocol argument
            return ["osäker tolkning"]

    results = semantic.advisory_llm_checks(
        Judge(),
        contract_id="contract_0001",
        document_id="main_agreement",
        clause_id="clause_03",
        fact=make_fact("f", 3, kind=FactKind.DURATION, unit="months"),
        clause_text="tre (3) månader",
    )
    assert results and all(check.status is CheckStatus.WARN for check in results)
    assert all(check.details["advisory_only"] is True for check in results)


# ---------------------------------------------------------------------------
# Dataset validation (exercise the documented export.loader API via a fake)
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload) -> None:  # noqa: ANN001 - test helper
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _install_fake_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("contractforge.export.loader")

    def find_contract_dirs(dataset_root: Path) -> list[Path]:
        root = Path(dataset_root)
        return sorted(
            path
            for path in root.iterdir()
            if path.is_dir() and path.name != "rejected" and not path.name.startswith(".")
        )

    def load_contract_dir(contract_dir: Path) -> LoadedContract:
        root = Path(contract_dir)
        scenario_path = root / "scenario.json"
        manifest_path = root / "manifest.json"
        if not scenario_path.is_file() or not manifest_path.is_file():
            raise ValueError(f"missing mandatory files in {root}")
        scenario = Scenario.model_validate(read_json(scenario_path))
        manifest = Manifest.model_validate(read_json(manifest_path))
        loaded = LoadedContract(root=root, scenario=scenario, manifest=manifest)
        rendered_path = root / "rendered.json"
        if rendered_path.is_file():
            payload = read_json(rendered_path)
            loaded.rendered = {
                key: RenderedDocument.model_validate(value) for key, value in payload.items()
            }
        for spec in scenario.documents:
            text_path = root / f"{spec.filename_stem}.txt"
            if text_path.is_file():
                loaded.texts[spec.document_id] = text_path.read_text(encoding="utf-8")
        gold_path = root / "gold_facts.json"
        if gold_path.is_file():
            loaded.gold = GoldSet.model_validate(read_json(gold_path))
        verification_path = root / "verification.json"
        if verification_path.is_file():
            loaded.verification = PackageVerification.model_validate(
                read_json(verification_path)
            )
        questions_path = root / "golden_questions.json"
        if questions_path.is_file():
            loaded.questions = [
                GoldenQuestion.model_validate(item) for item in read_json(questions_path)
            ]
        return loaded

    def load_dataset_manifest(dataset_root: Path) -> DatasetManifest | None:
        path = Path(dataset_root) / "dataset_manifest.json"
        if not path.is_file():
            return None
        return DatasetManifest.model_validate(read_json(path))

    module.MANIFEST_FILENAME = "manifest.json"
    module.SCENARIO_FILENAME = "scenario.json"
    module.RENDERED_FILENAME = "rendered.json"
    module.GOLD_FILENAME = "gold_facts.json"
    module.QUESTIONS_FILENAME = "golden_questions.json"
    module.VERIFICATION_FILENAME = "verification.json"
    module.DATASET_MANIFEST_FILENAME = "dataset_manifest.json"
    module.REJECTED_DIRNAME = "rejected"
    module.find_contract_dirs = find_contract_dirs
    module.load_contract_dir = load_contract_dir
    module.load_dataset_manifest = load_dataset_manifest
    module.load_dataset = lambda dataset_root: [
        load_contract_dir(directory) for directory in find_contract_dirs(dataset_root)
    ]
    monkeypatch.setitem(sys.modules, "contractforge.export.loader", module)
    import contractforge.export as export_package

    monkeypatch.setattr(export_package, "loader", module, raising=False)


def _build_dataset(root: Path, *, tamper_txt: bool = False, tamper_hash: bool = False) -> Path:
    scenario, package, rendered = bundle()
    gold = build_gold_set(scenario, package, rendered)
    questions = generate_and_verify_questions(scenario, gold, rendered).accepted
    verification = validate_package(scenario, package, rendered, gold=gold)
    assert verification.status is DocumentStatus.ACCEPTED
    contract_dir = root / "contract_0001"
    contract_dir.mkdir(parents=True, exist_ok=True)
    _write_json(contract_dir / "scenario.json", scenario.model_dump(mode="json"))
    _write_json(
        contract_dir / "rendered.json",
        {document_id: rendered_doc.model_dump(mode="json") for document_id, rendered_doc in rendered.items()},
    )
    _write_json(contract_dir / "gold_facts.json", gold.model_dump(mode="json"))
    _write_json(
        contract_dir / "golden_questions.json",
        [question.model_dump(mode="json") for question in questions],
    )
    _write_json(contract_dir / "verification.json", verification.model_dump(mode="json"))
    canonical = rendered["main_agreement"].canonical_text
    text = canonical + ("\nTAMPER\n" if tamper_txt else "")
    txt_path = contract_dir / "main_agreement.txt"
    txt_path.write_text(text, encoding="utf-8")
    artifact_paths = [
        "scenario.json",
        "rendered.json",
        "gold_facts.json",
        "golden_questions.json",
        "verification.json",
        "main_agreement.txt",
    ]
    artifacts = [
        ArtifactRecord(
            path=name,
            sha256=sha256_file(contract_dir / name),
            size_bytes=(contract_dir / name).stat().st_size,
            kind="document" if name.endswith(".txt") else "other",
        )
        for name in artifact_paths
    ]
    manifest = Manifest(
        contractforge_version="0.1.0",
        synthetic=True,
        contract_id=scenario.contract_id,
        contract_type=scenario.contract_type,
        difficulty=scenario.difficulty,
        language=scenario.language,
        jurisdiction=scenario.jurisdiction,
        provider="mock",
        model="mock-1",
        prompt_version="v1",
        prompt_sha256="0" * 64,
        seed=scenario.seed,
        generation_config=make_config_snapshot(),
        documents=[
            DocumentRecord(
                document_id="main_agreement",
                kind=scenario.documents[0].kind,
                title=rendered["main_agreement"].title,
                canonical_sha256=sha256_text(canonical),
                files=[artifact for artifact in artifacts if artifact.path.endswith(".txt")],
            )
        ],
        artifacts=artifacts,
        verification=VerificationSummary(
            status=verification.status,
            documents_total=1,
            documents_accepted=1,
            checks_failed=0,
            checks_warned=0,
        ),
        cost=CostSummary(),
    )
    _write_json(contract_dir / "manifest.json", manifest.model_dump(mode="json"))
    if tamper_hash:
        scenario_path = contract_dir / "scenario.json"
        scenario_path.write_text(
            scenario_path.read_text(encoding="utf-8") + " ", encoding="utf-8"
        )
    dataset_manifest = DatasetManifest(
        contractforge_version="0.1.0",
        synthetic=True,
        config=make_config_snapshot(),
        contracts=[
            DatasetContractRecord(
                contract_id=scenario.contract_id,
                path="contract_0001",
                status=verification.status,
                manifest_sha256=sha256_file(contract_dir / "manifest.json"),
            )
        ],
        totals=DatasetStats(contracts_total=1, contracts_accepted=1, contracts_rejected=0),
    )
    _write_json(root / "dataset_manifest.json", dataset_manifest.model_dump(mode="json"))
    return contract_dir


def test_dataset_valid_dataset_passes(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _install_fake_loader(monkeypatch)
    _build_dataset(tmp_path)
    report = validate_dataset(tmp_path)
    assert report.status is CheckStatus.PASS
    assert report.contracts_checked == 1
    assert report.contracts_passed == 1
    assert report.contracts_failed == 0
    assert not [check for check in report.checks if check.status is CheckStatus.FAIL]


def test_dataset_txt_must_equal_canonical_text(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _install_fake_loader(monkeypatch)
    _build_dataset(tmp_path, tamper_txt=True)
    report = validate_dataset(tmp_path)
    assert report.status is CheckStatus.FAIL
    check = next(
        check for check in report.checks if check.check_id.endswith(".canonical_text")
    )
    assert check.status is CheckStatus.FAIL
    assert "canonical_text" in check.message


def test_dataset_detects_artifact_hash_mismatch(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _install_fake_loader(monkeypatch)
    _build_dataset(tmp_path, tamper_hash=True)
    report = validate_dataset(tmp_path)
    assert report.status is CheckStatus.FAIL
    check = next(
        check for check in report.checks if check.check_id.endswith(".manifest.hashes")
    )
    assert check.status is CheckStatus.FAIL
    assert "scenario.json" in check.message


def test_dataset_broken_contract_becomes_fail_not_crash(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _install_fake_loader(monkeypatch)
    broken_dir = tmp_path / "contract_broken"
    broken_dir.mkdir()
    report = validate_dataset(tmp_path)
    assert report.status is CheckStatus.FAIL
    assert report.contracts_checked == 1
    assert report.contracts_failed == 1
    check = next(
        check for check in report.checks if check.check_id == "dataset.contract.contract_broken.load"
    )
    assert check.status is CheckStatus.FAIL
    assert "kunde inte läsas in" in check.message


def test_dataset_expect_min_contracts_fails(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _install_fake_loader(monkeypatch)
    _build_dataset(tmp_path)
    report = validate_dataset(tmp_path, expect_min_contracts=2)
    check = next(
        check for check in report.checks if check.check_id == "dataset.expect_min_contracts"
    )
    assert check.status is CheckStatus.FAIL
    assert report.status is CheckStatus.FAIL


def test_dataset_empty_dataset_passes(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _install_fake_loader(monkeypatch)
    report = validate_dataset(tmp_path)
    assert report.status is CheckStatus.PASS
    assert report.contracts_checked == 0
    manifest_check = next(
        check for check in report.checks if check.check_id == "dataset.manifest"
    )
    assert manifest_check.status is CheckStatus.SKIP


def test_dataset_missing_root_is_reported(tmp_path) -> None:
    report = validate_dataset(tmp_path / "does_not_exist")
    assert report.status is CheckStatus.FAIL
    assert report.errors


def test_dataset_with_only_rejected_contracts_passes(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _install_fake_loader(monkeypatch)
    scenario, package, rendered = bundle()
    verification = validate_package(scenario, package, rendered)
    rejected_dir = tmp_path / "rejected"
    rejected_dir.mkdir(parents=True)
    _write_json(
        rejected_dir / f"{scenario.contract_id}.json",
        {
            "schema_version": "1.0",
            "synthetic": True,
            "contract_id": scenario.contract_id,
            "status": "REJECTED",
            "source_status": "REJECTED",
            "attempts": 1,
            "scenario": scenario.model_dump(mode="json"),
            "verification": verification.model_dump(mode="json"),
            "notes": ["Rejected contract"],
        },
    )
    dataset_manifest = DatasetManifest(
        contractforge_version="0.1.0",
        synthetic=True,
        config=make_config_snapshot(),
        contracts=[],
        totals=DatasetStats(
            contracts_total=1, contracts_accepted=0, contracts_rejected=1
        ),
    )
    _write_json(
        tmp_path / "dataset_manifest.json", dataset_manifest.model_dump(mode="json")
    )
    report = validate_dataset(tmp_path)
    assert report.status is CheckStatus.PASS
    assert report.contracts_checked == 0
    assert next(
        check for check in report.checks if check.check_id == "dataset.rejected"
    ).status is CheckStatus.PASS
    assert next(
        check for check in report.checks if check.check_id == "dataset.manifest.totals"
    ).status is CheckStatus.PASS


def test_dataset_rejected_totals_mismatch_fails(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _install_fake_loader(monkeypatch)
    scenario, package, rendered = bundle()
    verification = validate_package(scenario, package, rendered)
    rejected_dir = tmp_path / "rejected"
    rejected_dir.mkdir(parents=True)
    _write_json(
        rejected_dir / f"{scenario.contract_id}.json",
        {
            "contract_id": scenario.contract_id,
            "status": "REJECTED",
            "scenario": scenario.model_dump(mode="json"),
            "verification": verification.model_dump(mode="json"),
        },
    )
    dataset_manifest = DatasetManifest(
        contractforge_version="0.1.0",
        synthetic=True,
        config=make_config_snapshot(),
        contracts=[],
        totals=DatasetStats(
            contracts_total=2, contracts_accepted=1, contracts_rejected=1
        ),
    )
    _write_json(
        tmp_path / "dataset_manifest.json", dataset_manifest.model_dump(mode="json")
    )
    report = validate_dataset(tmp_path)
    assert report.status is CheckStatus.FAIL
    check = next(
        check for check in report.checks if check.check_id == "dataset.manifest.totals"
    )
    assert check.status is CheckStatus.FAIL
    assert "contracts_total" in check.message


def test_dataset_rejects_approved_contract_marked_rejected(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _install_fake_loader(monkeypatch)
    _build_dataset(tmp_path)
    manifest_payload = read_json(tmp_path / "dataset_manifest.json")
    manifest_payload["contracts"][0]["status"] = "REJECTED"
    _write_json(tmp_path / "dataset_manifest.json", manifest_payload)
    report = validate_dataset(tmp_path)
    assert report.status is CheckStatus.FAIL
    check = next(
        check for check in report.checks if check.check_id == "dataset.manifest.records"
    )
    assert check.status is CheckStatus.FAIL
    assert "REJECTED" in check.message
