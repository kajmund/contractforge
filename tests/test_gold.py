"""Tests for gold-set construction: real evidence spans, precision, no invention."""

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
    ClauseMode,
    EvidencePrecision,
    FactKind,
    FactStatus,
    Relation,
    RelationKind,
    Scenario,
)

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


def build(scenario: Scenario | None = None):
    scenario = scenario or clean_scenario()
    package = clean_package(scenario)
    rendered = make_rendered_documents(scenario, package)
    return scenario, package, rendered, build_gold_set(scenario, package, rendered)


def _slice(rendered, span) -> str:
    return rendered[span.document_id].canonical_text[span.start : span.end]


def _assert_evidence_valid(gold, rendered) -> None:
    for fact in gold.facts:
        for span in fact.evidence:
            assert span.document_id in rendered
            text = rendered[span.document_id].canonical_text
            assert 0 <= span.start < span.end <= len(text)
            assert text[span.start : span.end] == span.quote
            if span.clause_id is not None:
                clause_span = rendered[span.document_id].clause_spans[span.clause_id]
                assert clause_span.start <= span.start
                assert span.end <= clause_span.end
    for entity in gold.entities:
        for span in entity.evidence:
            assert _slice(rendered, span) == span.quote
    for reference in gold.references:
        for span in reference.evidence:
            assert _slice(rendered, span) == span.quote


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


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


def test_gold_covers_every_scenario_fact_with_normalised_metadata() -> None:
    scenario, _package, _rendered, gold = build()
    assert [fact.fact_id for fact in gold.facts] == [fact.fact_id for fact in scenario.facts]
    assert gold.contract_id == scenario.contract_id
    for gold_fact, scenario_fact in zip(gold.facts, scenario.facts, strict=True):
        assert gold_fact.kind == scenario_fact.kind
        assert gold_fact.value == scenario_fact.value
        assert gold_fact.unit == scenario_fact.unit
        assert gold_fact.currency == scenario_fact.currency
        assert gold_fact.status == scenario_fact.status
        assert gold_fact.question_ids == []


def test_all_evidence_spans_are_real_canonical_slices() -> None:
    _scenario, _package, rendered, gold = build()
    _assert_evidence_valid(gold, rendered)


def test_explicit_precision_for_explicit_surface() -> None:
    _scenario, _package, rendered, gold = build()
    fact = gold.fact("fact_hourly_rate")
    assert fact.precision is EvidencePrecision.EXACT
    assert fact.evidence
    quote = fact.evidence[0].quote
    assert "950" in quote
    assert _slice(rendered, fact.evidence[0]) == quote


def test_paraphrase_precision_for_quarter() -> None:
    scenario = clean_scenario()
    document = scenario.documents[0]
    clauses = list(document.clauses)
    clauses[2] = clauses[2].model_copy(update={"mode": ClauseMode.PARAPHRASE})
    scenario = scenario.model_copy(
        update={"documents": [document.model_copy(update={"clauses": clauses})]}
    )
    package = clean_package(scenario)
    package = package.model_copy(
        update={
            "documents": [
                package.document("main_agreement").model_copy(
                    update={
                        "clauses": [
                            clause.model_copy(
                                update={"text": "Uppsägningstiden är ett kvartal."}
                            )
                            if clause.clause_id == "clause_03"
                            else clause
                            for clause in package.document("main_agreement").clauses
                        ]
                    }
                )
            ]
        }
    )
    rendered = make_rendered_documents(scenario, package)
    gold = build_gold_set(scenario, package, rendered)
    fact = gold.fact("fact_notice_period")
    assert fact.precision is EvidencePrecision.PARAPHRASE
    assert any("ett kvartal" in span.quote.lower() for span in fact.evidence)
    _assert_evidence_valid(gold, rendered)


def test_clause_precision_uses_keyword_or_whole_clause_span() -> None:
    scenario = clean_scenario()
    document = scenario.documents[0]
    clauses = list(document.clauses)
    clauses[2] = clauses[2].model_copy(update={"heading_sv": "Övrigt"})
    scenario = scenario.model_copy(
        update={"documents": [document.model_copy(update={"clauses": clauses})]}
    )
    package = clean_package(scenario)
    signed = []
    for clause in package.document("main_agreement").clauses:
        if clause.clause_id == "clause_03":
            signed.append(
                clause.model_copy(update={"heading": "Övrigt", "text": "Regleringen sker i separat avtal."})
            )
        else:
            signed.append(clause)
    package = package.model_copy(
        update={
            "documents": [
                package.document("main_agreement").model_copy(update={"clauses": signed})
            ]
        }
    )
    rendered = make_rendered_documents(scenario, package)
    gold = build_gold_set(scenario, package, rendered)
    fact = gold.fact("fact_notice_period")
    assert fact.precision is EvidencePrecision.CLAUSE
    assert fact.evidence
    clause_span = rendered["main_agreement"].clause_spans["clause_03"]
    assert fact.evidence[0].start == clause_span.start
    assert fact.evidence[0].end == clause_span.end
    assert _slice(rendered, fact.evidence[0]) == fact.evidence[0].quote


def test_unplanned_fact_has_no_invented_evidence() -> None:
    scenario = make_consulting_scenario()
    unplanned = make_fact(
        "fact_unplanned",
        "aldrig uttalat",
        kind=FactKind.TEXT,
        explicit_surfaces=["aldrig uttalat"],
        keywords_sv=["aldrig"],
    )
    scenario = scenario.model_copy(update={"facts": [*scenario.facts, unplanned]})
    package = clean_package(scenario)
    rendered = make_rendered_documents(scenario, package)
    gold = build_gold_set(scenario, package, rendered)
    fact = gold.fact("fact_unplanned")
    assert fact.evidence == []
    assert "evidensspann" in fact.notes_sv


def test_superseded_status_is_carried_to_gold() -> None:
    scenario = make_consulting_scenario()
    old_rate = make_fact(
        "fact_hourly_rate_old",
        900,
        kind=FactKind.MONEY,
        currency="SEK",
        explicit_surfaces=["900 SEK"],
        keywords_sv=["timarvode"],
    ).model_copy(
        update={"status": FactStatus.SUPERSEDED, "superseded_by": "fact_hourly_rate"}
    )
    document = scenario.documents[0]
    clauses = list(document.clauses)
    clauses[1] = clauses[1].model_copy(
        update={"fact_ids": ["fact_hourly_rate", "fact_hourly_rate_old"]}
    )
    scenario = scenario.model_copy(
        update={
            "facts": [*scenario.facts, old_rate],
            "documents": [document.model_copy(update={"clauses": clauses})],
        }
    )
    package = clean_package(scenario)
    rendered = make_rendered_documents(scenario, package)
    gold = build_gold_set(scenario, package, rendered)
    assert gold.fact("fact_hourly_rate_old").status is FactStatus.SUPERSEDED
    assert gold.fact("fact_hourly_rate").status is FactStatus.ACTIVE


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def test_entities_have_roles_names_and_evidence() -> None:
    scenario, _package, rendered, gold = build()
    entities = {entity.entity_id: entity for entity in gold.entities}
    assert set(entities) == {party.party_id for party in scenario.parties}
    for party in scenario.parties:
        entity = entities[party.party_id]
        assert entity.name == party.name
        assert entity.role == party.role
        assert entity.evidence
        assert any(party.name in span.quote for span in entity.evidence)
        for span in entity.evidence:
            assert _slice(rendered, span) == span.quote


def test_entity_missing_from_text_gets_no_evidence() -> None:
    scenario = make_consulting_scenario()
    package = make_generated_package(scenario)  # no party names in this fixture
    rendered = make_rendered_documents(scenario, package)
    gold = build_gold_set(scenario, package, rendered)
    assert all(entity.evidence == [] for entity in gold.entities)


# ---------------------------------------------------------------------------
# References, anomalies and missing documents
# ---------------------------------------------------------------------------


def test_relations_become_gold_references_with_evidence() -> None:
    scenario = make_consulting_scenario()
    relation = Relation(
        relation_id="rel_001",
        kind=RelationKind.REFERENCES,
        source="main_agreement:clause_02",
        target="fact:fact_liability_cap",
        description_sv="Ersättningsklausulen refererar ansvarsbegränsningen.",
    )
    scenario = scenario.model_copy(update={"relations": [relation]})
    package = clean_package(scenario)
    rendered = make_rendered_documents(scenario, package)
    gold = build_gold_set(scenario, package, rendered)
    assert len(gold.references) == 1
    reference = gold.references[0]
    assert reference.kind is RelationKind.REFERENCES
    assert reference.source_document_id == "main_agreement"
    assert reference.source_clause_id == "clause_02"
    assert reference.target_document_id == "main_agreement"
    assert reference.target_clause_id == "clause_05"
    assert reference.evidence
    _assert_evidence_valid(gold, rendered)


def test_missing_documents_and_document_reference_are_preserved() -> None:
    scenario = make_consulting_scenario()
    document = scenario.documents[0]
    scenario = scenario.model_copy(
        update={
            "documents": [
                document.model_copy(update={"references_documents": ["appendix_b"]})
            ],
            "missing_documents": ["appendix_b"],
        }
    )
    package = clean_package(scenario)
    rendered = make_rendered_documents(scenario, package)
    gold = build_gold_set(scenario, package, rendered)
    assert gold.missing_documents == ["appendix_b"]
    targets = {reference.target_document_id for reference in gold.references}
    assert "appendix_b" in targets
    reference = next(
        reference for reference in gold.references if reference.target_document_id == "appendix_b"
    )
    assert reference.evidence


def test_anomalies_are_mapped_with_governing_fact_and_evidence() -> None:
    scenario = conflicting_scenario()
    package = clean_package(scenario)
    rendered = make_rendered_documents(scenario, package)
    gold = build_gold_set(scenario, package, rendered)
    assert len(gold.anomalies) == 1
    anomaly = gold.anomalies[0]
    source = scenario.anomalies[0]
    assert anomaly.anomaly_id == source.anomaly_id
    assert anomaly.kind is source.kind
    assert anomaly.answerability is Answerability.CONFLICTING_EVIDENCE
    assert anomaly.governing_fact_id == source.governing_fact_id
    assert set(anomaly.fact_ids) == set(source.fact_ids)
    assert anomaly.evidence
    _assert_evidence_valid(gold, rendered)


def test_intentionally_missing_fact_has_no_evidence() -> None:
    scenario = make_consulting_scenario(difficulty=6)
    anomaly = Anomaly(
        anomaly_id="anomaly_missing",
        kind=AnomalyKind.MISSING_INFORMATION,
        fact_ids=["fact_extension_period"],
        answerability=Answerability.INSUFFICIENT_EVIDENCE,
        description_sv="Förlängningsperioden regleras i ett saknat sidodokument.",
    )
    scenario = scenario.model_copy(
        update={"anomalies": [anomaly], "missing_documents": ["side_letter_2026"]}
    )
    package = clean_package(scenario)
    rendered = make_rendered_documents(scenario, package)
    gold = build_gold_set(scenario, package, rendered)
    fact = gold.fact("fact_extension_period")
    assert fact.evidence == []
    assert fact.precision is EvidencePrecision.CLAUSE
    assert "avsiktligt oreglerat" in fact.notes_sv


# ---------------------------------------------------------------------------
# Determinism / no invention
# ---------------------------------------------------------------------------


def test_gold_build_is_deterministic() -> None:
    scenario, package, rendered = build()[:3]
    first = build_gold_set(scenario, package, rendered)
    second = build_gold_set(scenario, package, rendered)
    assert first.model_dump(exclude={"generated_at"}) == second.model_dump(
        exclude={"generated_at"}
    )


def test_evidence_quotes_are_never_shorter_than_slice_and_never_outside_text() -> None:
    _scenario, _package, rendered, gold = build()
    for fact in gold.facts:
        for span in fact.evidence:
            text = rendered[span.document_id].canonical_text
            assert span.quote == text[span.start : span.end]
            assert span.end <= len(text)
    for entity in gold.entities:
        for span in entity.evidence:
            text = rendered[span.document_id].canonical_text
            assert span.quote == text[span.start : span.end]
