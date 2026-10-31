"""Shared test factories.

Owned by the Lead. Teammates may use these helpers and add local helpers inside
their own test modules, but should not edit this file without coordinating:
other suites depend on the shapes below.
"""

from __future__ import annotations

from datetime import date

from contractforge.models import (
    Answerability,
    ClauseMode,
    ClauseSpec,
    ContractResult,
    ContractType,
    CostSummary,
    DocKind,
    DocumentFormat,
    DocumentSpec,
    DocumentStatus,
    DocumentVerification,
    Fact,
    FactKind,
    GeneratedClause,
    GeneratedDocument,
    GeneratedPackage,
    GenerationConfigSnapshot,
    GoldenQuestion,
    GoldSet,
    Language,
    PackageVerification,
    Party,
    RenderedDocument,
    Scenario,
    TextSpan,
    VariationProfile,
)


def make_party(party_id: str, name: str, role: str) -> Party:
    return Party(party_id=party_id, name=name, role=role)


def make_fact(
    fact_id: str,
    value: object,
    *,
    kind: FactKind = FactKind.TEXT,
    unit: str | None = None,
    currency: str | None = None,
    label_sv: str | None = None,
    explicit_surfaces: list[str] | None = None,
    paraphrase_surfaces: list[str] | None = None,
    keywords_sv: list[str] | None = None,
    entity_id: str | None = None,
) -> Fact:
    return Fact(
        fact_id=fact_id,
        kind=kind,
        value=value,
        unit=unit,
        currency=currency,
        entity_id=entity_id,
        label_sv=label_sv or fact_id,
        explicit_surfaces=explicit_surfaces or [],
        paraphrase_surfaces=paraphrase_surfaces or [],
        keywords_sv=keywords_sv or [],
    )


def make_consulting_scenario(difficulty: int = 1) -> Scenario:
    """A small, realistic consulting scenario used across unit tests."""

    facts = [
        make_fact(
            "fact_hourly_rate",
            950,
            kind=FactKind.MONEY,
            currency="SEK",
            label_sv="Timarvode",
            explicit_surfaces=["950 SEK", "950 kronor"],
            keywords_sv=["timarvode", "arvode"],
        ),
        make_fact(
            "fact_notice_period",
            3,
            kind=FactKind.DURATION,
            unit="months",
            label_sv="Uppsägningstid",
            explicit_surfaces=["tre (3) månader", "3 månader"],
            paraphrase_surfaces=["ett kvartal"],
            keywords_sv=["uppsägningstid", "uppsägning"],
        ),
        make_fact(
            "fact_extension_period",
            12,
            kind=FactKind.DURATION,
            unit="months",
            label_sv="Förlängningsperiod",
            explicit_surfaces=["tolv (12) månader", "12 månader"],
            keywords_sv=["förlängning", "förlängs"],
        ),
        make_fact(
            "fact_liability_cap",
            500000,
            kind=FactKind.MONEY,
            currency="SEK",
            label_sv="Ansvarsbegränsning",
            explicit_surfaces=["500 000 SEK", "500 000 kronor"],
            keywords_sv=["ansvarsbegränsning", "ansvar"],
        ),
    ]
    clauses = [
        ClauseSpec(
            clause_id="clause_01",
            document_id="main_agreement",
            order=1,
            heading_sv="Avtalsparter",
            mode=ClauseMode.EXPLICIT,
        ),
        ClauseSpec(
            clause_id="clause_02",
            document_id="main_agreement",
            order=2,
            heading_sv="Ersättning",
            fact_ids=["fact_hourly_rate"],
            mode=ClauseMode.EXPLICIT,
        ),
        ClauseSpec(
            clause_id="clause_03",
            document_id="main_agreement",
            order=3,
            heading_sv="Uppsägningstid",
            fact_ids=["fact_notice_period"],
            mode=ClauseMode.EXPLICIT,
        ),
        ClauseSpec(
            clause_id="clause_04",
            document_id="main_agreement",
            order=4,
            heading_sv="Förlängning",
            fact_ids=["fact_extension_period"],
            mode=ClauseMode.EXPLICIT,
        ),
        ClauseSpec(
            clause_id="clause_05",
            document_id="main_agreement",
            order=5,
            heading_sv="Ansvarsbegränsning",
            fact_ids=["fact_liability_cap"],
            mode=ClauseMode.EXPLICIT,
        ),
    ]
    document = DocumentSpec(
        document_id="main_agreement",
        kind=DocKind.MAIN_AGREEMENT,
        title_sv="Konsultavtal",
        title_en="Consulting Agreement",
        filename_stem="main_agreement",
        clauses=clauses,
        effective_date=date(2026, 1, 1),
    )
    return Scenario(
        contract_id="contract_0001",
        contract_type=ContractType.CONSULTING,
        difficulty=difficulty,
        seed=42,
        parties=[
            make_party("party_a", "Nordvik Teknik AB", "client"),
            make_party("party_b", "Sundberg Konsult AB", "supplier"),
        ],
        facts=facts,
        documents=[document],
        variation=VariationProfile(language=Language.SV),
        anomalies=[],
    )


def _clause_text(fact_surfaces: dict[str, str], clause: ClauseSpec) -> str:
    surfaces = [fact_surfaces[factor] for factor in clause.fact_ids if factor in fact_surfaces]
    if surfaces:
        return "Parterna är överens om följande: " + " och ".join(surfaces) + "."
    return f"Parterna är överens om {clause.heading_sv.lower()}."


def make_generated_package(scenario: Scenario) -> GeneratedPackage:
    """Deterministic package whose clause texts embed explicit fact surfaces."""

    surfaces = {
        fact.fact_id: (fact.explicit_surfaces[0] if fact.explicit_surfaces else str(fact.value))
        for fact in scenario.facts
    }
    documents = []
    for spec in scenario.documents:
        clauses = [
            GeneratedClause(
                clause_id=clause.clause_id,
                document_id=spec.document_id,
                heading=clause.heading_sv,
                text=_clause_text(surfaces, clause),
            )
            for clause in spec.clauses
        ]
        documents.append(
            GeneratedDocument(
                document_id=spec.document_id,
                kind=spec.kind,
                title=spec.title_sv,
                clauses=clauses,
            )
        )
    return GeneratedPackage(contract_id=scenario.contract_id, documents=documents)


def make_rendered_documents(
    scenario: Scenario, package: GeneratedPackage
) -> dict[str, RenderedDocument]:
    """Minimal canonical renderer for tests that must not depend on production rendering."""

    rendered: dict[str, RenderedDocument] = {}
    for document in package.documents:
        lines = [
            "SYNTETISK TESTDATA – ContractForge",
            f"contract_id: {scenario.contract_id}",
            f"document_id: {document.document_id}",
            "",
            document.title,
            "",
        ]
        spans: dict[str, TextSpan] = {}
        text = "\n".join(lines) + "\n"
        for index, clause in enumerate(document.clauses, start=1):
            heading = f"{index}. {clause.heading}"
            block = f"{heading}\n{clause.text}\n"
            start = len(text)
            text += block
            spans[clause.clause_id] = TextSpan(
                document_id=document.document_id,
                clause_id=clause.clause_id,
                start=start,
                end=start + len(block),
            )
            text += "\n"
        rendered[document.document_id] = RenderedDocument(
            document_id=document.document_id,
            kind=document.kind,
            title=document.title,
            language=scenario.language,
            canonical_text=text,
            clause_spans=spans,
        )
    return rendered


def make_config_snapshot(**overrides) -> GenerationConfigSnapshot:
    payload = {
        "contract_type": ContractType.CONSULTING,
        "count": 1,
        "clauses": 5,
        "difficulty": 1,
        "language": Language.SV,
        "jurisdiction": "SE",
        "provider": "mock",
        "model": "mock-1",
        "temperature": 0.0,
        "seed": 42,
        "concurrency": 1,
        "max_repair_attempts": 2,
        "prompt_version": "v1",
        "prompt_sha256": "0" * 64,
        "formats": [DocumentFormat.TXT, DocumentFormat.MARKDOWN],
        "contract_id_prefix": "contract",
    }
    payload.update(overrides)
    return GenerationConfigSnapshot(**payload)


def make_contract_result(
    *,
    scenario: Scenario | None = None,
    status: DocumentStatus = DocumentStatus.ACCEPTED,
    with_questions: bool = True,
) -> ContractResult:
    scenario = scenario or make_consulting_scenario()
    package = make_generated_package(scenario)
    rendered = make_rendered_documents(scenario, package)
    gold = GoldSet(contract_id=scenario.contract_id)
    questions: list[GoldenQuestion] = []
    if with_questions:
        questions = [
            GoldenQuestion(
                question_id="q_001",
                question="Vilken uppsägningstid gäller?",
                language=Language.SV,
                category="fact_extraction",
                difficulty=scenario.difficulty,
                expected_answer={"value": 3, "unit": "months"},
                required_facts=["fact_notice_period"],
                evidence=[],
                answerability=Answerability.ANSWERABLE,
                validated=True,
                source="deterministic",
            )
        ]
    verification = PackageVerification(
        contract_id=scenario.contract_id,
        status=status,
        documents=[
            DocumentVerification(
                document_id=document.document_id,
                status=status,
            )
            for document in package.documents
        ],
    )
    verification.recompute_stats()
    return ContractResult(
        contract_id=scenario.contract_id,
        scenario=scenario,
        package=package,
        rendered=rendered,
        verification=verification,
        gold=gold,
        questions=questions,
        cost=CostSummary(calls=1, input_tokens=100, output_tokens=200, total_tokens=300),
        config=make_config_snapshot(difficulty=scenario.difficulty),
        status=status,
    )
