"""ContractForge domain model.

This module is the shared contract between every subsystem:
scenario -> generation -> rendering -> validation -> gold -> questions -> export.

Design rule: *facts before documents*. A :class:`Scenario` is the machine-readable
truth. The generated text is a rendering of that truth, and every fact in the
final gold file must be traceable back to a scenario fact and forward to a
verifiable character span in a canonical document text.

All persisted objects are pydantic models with strict (``extra="forbid"``)
validation so malformed datasets fail at the boundary instead of silently
propagating into gold data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"
"""Version of the exported dataset schema (manifest, scenario, gold, questions)."""


class StrictModel(BaseModel):
    """Base model: unknown fields are errors, not noise."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ContractType(str, Enum):
    EMPLOYMENT = "employment"
    CONSULTING = "consulting"
    SHAREHOLDERS = "shareholders"
    NDA = "nda"
    SERVICES = "services"
    SUPPLY = "supply"
    LICENSE = "license"
    LEASE = "lease"
    COOPERATION = "cooperation"
    SALE = "sale"


class Language(str, Enum):
    SV = "sv"
    EN = "en"


class DocKind(str, Enum):
    MAIN_AGREEMENT = "main_agreement"
    APPENDIX = "appendix"
    AMENDMENT = "amendment"
    ORDER_FORM = "order_form"
    POLICY = "policy"


class FactKind(str, Enum):
    MONEY = "money"
    DURATION = "duration"
    PERCENTAGE = "percentage"
    INTEGER = "integer"
    DECIMAL = "decimal"
    TEXT = "text"
    DATE = "date"
    BOOLEAN = "boolean"
    ENTITY = "entity"
    LIST = "list"
    DEFINED_TERM = "defined_term"


class FactStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    PROPOSED = "proposed"


class ClauseMode(str, Enum):
    """How a clause is expected to express its facts."""

    EXPLICIT = "explicit"
    PARAPHRASE = "paraphrase"
    SPLIT = "split"  # one fact spread over several clauses
    VAGUE = "vague"
    DISTRACTOR = "distractor"
    DEFINITION = "definition"
    REFERENCE = "reference"
    CONDITIONAL = "conditional"
    EXCEPTION = "exception"
    CONTRADICTION = "contradiction"


class RelationKind(str, Enum):
    MODIFIES = "modifies"
    SUPERSEDES = "supersedes"
    REFERENCES = "references"
    DEFINES = "defines"
    CONDITIONS = "conditions"
    CONFLICTS_WITH = "conflicts_with"
    DEPENDS_ON = "depends_on"
    CLARIFIES = "clarifies"


class AnomalyKind(str, Enum):
    INTENTIONAL_CONTRADICTION = "intentional_contradiction"
    EXCEPTION = "exception"
    SUPERSESSION = "supersession"
    MISSING_INFORMATION = "missing_information"
    AMBIGUITY = "ambiguity"


class Answerability(str, Enum):
    ANSWERABLE = "ANSWERABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CONDITIONAL = "CONDITIONAL"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"


class QuestionCategory(str, Enum):
    FACT_EXTRACTION = "fact_extraction"
    ENTITY_IDENTIFICATION = "entity_identification"
    RELATION = "relation"
    COMPARISON = "comparison"
    CONDITIONAL = "conditional"
    CONTRADICTION = "contradiction"
    LEGAL_REASONING = "legal_reasoning"
    ABSENCE = "absence"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    COMPLETENESS = "completeness"


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"


class DocumentStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    REPAIRED = "REPAIRED"
    REJECTED = "REJECTED"


class EvidencePrecision(str, Enum):
    """How tightly an evidence span pins down a fact."""

    EXACT = "exact"  # span is the literal value/surface
    PARAPHRASE = "paraphrase"  # span is a verified paraphrase of the value
    CLAUSE = "clause"  # only the whole clause can be pinned down


class DocumentFormat(str, Enum):
    TXT = "txt"
    MARKDOWN = "markdown"
    DOCX = "docx"
    PDF = "pdf"


class ExportFormat(str, Enum):
    ZIP = "zip"
    JSONL = "jsonl"
    JSON = "json"


FORMAT_EXTENSIONS: dict[DocumentFormat, str] = {
    DocumentFormat.TXT: ".txt",
    DocumentFormat.MARKDOWN: ".md",
    DocumentFormat.DOCX: ".docx",
    DocumentFormat.PDF: ".pdf",
}


class VariationStyle(str, Enum):
    PLAIN = "plain"
    FORMAL = "formal"
    COMPLEX = "complex"


class Directness(str, Enum):
    EXPLICIT = "explicit"
    PARAPHRASE = "paraphrase"
    MIXED = "mixed"


class StructureMode(str, Enum):
    SINGLE_CLAUSE = "single_clause"
    MULTI_CLAUSE = "multi_clause"


class DocumentQuality(str, Enum):
    CLEAN = "clean"
    LINE_BREAKS = "line_breaks"
    TABLES = "tables"
    PAGE_BREAKS = "page_breaks"


class DependencyMode(str, Enum):
    CONDITIONS = "conditions"
    EXCEPTIONS = "exceptions"
    CONTRADICTIONS = "contradictions"


class ReferenceMode(str, Enum):
    CLAUSE = "clause"
    APPENDIX = "appendix"
    EXTERNAL_SOURCE = "external_source"


# ---------------------------------------------------------------------------
# Scenario: the machine-readable truth
# ---------------------------------------------------------------------------


class Party(StrictModel):
    party_id: str
    name: str
    role: str
    org_number: str | None = None
    address: str | None = None
    synthetic: bool = True


class Fact(StrictModel):
    """An atomic, normalized fact that the generated contract must express.

    ``explicit_surfaces`` are textual forms that count as an *explicit*
    rendering (checked deterministically). ``paraphrase_surfaces`` are verified
    alternative legal phrasings (checked semantically). ``keywords_sv`` /
    ``keywords_en`` anchor the topic so the semantic checker can find the
    operative value even when the wording is new.
    """

    fact_id: str
    kind: FactKind
    value: Any
    unit: str | None = None
    currency: str | None = None
    entity_id: str | None = None
    label_sv: str
    label_en: str = ""
    description_sv: str = ""
    explicit_surfaces: list[str] = Field(default_factory=list)
    paraphrase_surfaces: list[str] = Field(default_factory=list)
    keywords_sv: list[str] = Field(default_factory=list)
    keywords_en: list[str] = Field(default_factory=list)
    status: FactStatus = FactStatus.ACTIVE
    superseded_by: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None

    @property
    def is_superseded(self) -> bool:
        return self.status == FactStatus.SUPERSEDED


class ClauseSpec(StrictModel):
    clause_id: str
    document_id: str
    order: int
    heading_sv: str
    heading_en: str = ""
    fact_ids: list[str] = Field(default_factory=list)
    mode: ClauseMode = ClauseMode.EXPLICIT
    depends_on: list[str] = Field(default_factory=list)
    reference_to: list[str] = Field(default_factory=list)
    instruction_sv: str = ""
    instruction_en: str = ""

    def heading(self, language: Language) -> str:
        if language == Language.EN and self.heading_en:
            return self.heading_en
        return self.heading_sv


class DocumentSpec(StrictModel):
    document_id: str
    kind: DocKind
    title_sv: str
    title_en: str = ""
    filename_stem: str
    clauses: list[ClauseSpec] = Field(default_factory=list)
    references_documents: list[str] = Field(default_factory=list)
    effective_date: date | None = None
    notes: str = ""

    def title(self, language: Language) -> str:
        if language == Language.EN and self.title_en:
            return self.title_en
        return self.title_sv


class Relation(StrictModel):
    relation_id: str
    kind: RelationKind
    source: str  # "document_id:clause_id" or "fact:<fact_id>"
    target: str
    description_sv: str = ""


class Anomaly(StrictModel):
    """Deliberate inconsistency, exception, supersession or information gap.

    Difficulty 5 uses contradictions/exceptions; difficulty 6 uses missing
    information. Gold must distinguish *intentional* anomalies from accidental
    generation errors: intentional ones are documented here and are expected to
    be found in the text; accidental ones are rejected by validation.
    """

    anomaly_id: str
    kind: AnomalyKind
    fact_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    governing_fact_id: str | None = None
    answerability: Answerability = Answerability.ANSWERABLE
    description_sv: str = ""
    description_en: str = ""
    applies_from: date | None = None
    applies_to: date | None = None


class VariationProfile(StrictModel):
    language: Language = Language.SV
    style: VariationStyle = VariationStyle.FORMAL
    directness: Directness = Directness.EXPLICIT
    structure: StructureMode = StructureMode.SINGLE_CLAUSE
    reference_modes: list[ReferenceMode] = Field(default_factory=list)
    distractor_count: int = 0
    document_quality: DocumentQuality = DocumentQuality.CLEAN
    dependency_modes: list[DependencyMode] = Field(default_factory=list)


class Scenario(StrictModel):
    schema_version: str = SCHEMA_VERSION
    contract_id: str
    contract_type: ContractType
    jurisdiction: str = "SE"
    language: Language = Language.SV
    difficulty: int = Field(ge=1, le=6)
    seed: int
    parties: list[Party]
    facts: list[Fact]
    documents: list[DocumentSpec]
    variation: VariationProfile = Field(default_factory=VariationProfile)
    relations: list[Relation] = Field(default_factory=list)
    anomalies: list[Anomaly] = Field(default_factory=list)
    missing_documents: list[str] = Field(default_factory=list)
    synthetic: bool = True
    notes_sv: str = ""

    # -- lookups ---------------------------------------------------------
    def fact(self, fact_id: str) -> Fact:
        for fact in self.facts:
            if fact.fact_id == fact_id:
                return fact
        raise KeyError(f"unknown fact_id {fact_id!r} in {self.contract_id}")

    def document(self, document_id: str) -> DocumentSpec:
        for document in self.documents:
            if document.document_id == document_id:
                return document
        raise KeyError(f"unknown document_id {document_id!r} in {self.contract_id}")

    def clause(self, clause_id: str) -> ClauseSpec:
        for document in self.documents:
            for clause in document.clauses:
                if clause.clause_id == clause_id:
                    return clause
        raise KeyError(f"unknown clause_id {clause_id!r} in {self.contract_id}")

    def party(self, party_id: str) -> Party:
        for party in self.parties:
            if party.party_id == party_id:
                return party
        raise KeyError(f"unknown party_id {party_id!r} in {self.contract_id}")

    @property
    def fact_ids(self) -> list[str]:
        return [fact.fact_id for fact in self.facts]

    @property
    def clause_ids(self) -> list[str]:
        return [clause.clause_id for document in self.documents for clause in document.clauses]

    def anomaly_for_fact(self, fact_id: str) -> list[Anomaly]:
        return [anomaly for anomaly in self.anomalies if fact_id in anomaly.fact_ids]

    @model_validator(mode="after")
    def _validate_references(self) -> Scenario:
        errors: list[str] = []

        def _unique(values: list[str], what: str) -> None:
            seen: set[str] = set()
            for value in values:
                if value in seen:
                    errors.append(f"duplicate {what}: {value}")
                seen.add(value)

        _unique([party.party_id for party in self.parties], "party_id")
        _unique([fact.fact_id for fact in self.facts], "fact_id")
        _unique([document.document_id for document in self.documents], "document_id")
        _unique(self.clause_ids, "clause_id")
        _unique([relation.relation_id for relation in self.relations], "relation_id")
        _unique([anomaly.anomaly_id for anomaly in self.anomalies], "anomaly_id")

        fact_ids = set(self.fact_ids)
        document_ids = {document.document_id for document in self.documents}
        party_ids = {party.party_id for party in self.parties}

        for document in self.documents:
            for clause in document.clauses:
                if clause.document_id != document.document_id:
                    errors.append(
                        f"clause {clause.clause_id} belongs to {clause.document_id} "
                        f"but is nested in {document.document_id}"
                    )
                for fact_id in clause.fact_ids:
                    if fact_id not in fact_ids:
                        errors.append(f"clause {clause.clause_id} references missing fact {fact_id}")
                for dependency in clause.depends_on:
                    if dependency not in self.clause_ids:
                        errors.append(
                            f"clause {clause.clause_id} depends on missing clause {dependency}"
                        )
            for referenced in document.references_documents:
                if referenced not in document_ids and referenced not in self.missing_documents:
                    errors.append(
                        f"document {document.document_id} references unknown document {referenced}"
                    )

        for fact in self.facts:
            if fact.entity_id is not None and fact.entity_id not in party_ids:
                errors.append(f"fact {fact.fact_id} references unknown entity {fact.entity_id}")
            if fact.superseded_by is not None and fact.superseded_by not in fact_ids:
                errors.append(f"fact {fact.fact_id} is superseded by missing fact")
            if fact.is_superseded and fact.superseded_by is None:
                errors.append(f"superseded fact {fact.fact_id} has no superseded_by")

        for anomaly in self.anomalies:
            for fact_id in anomaly.fact_ids:
                if fact_id not in fact_ids:
                    errors.append(f"anomaly {anomaly.anomaly_id} references missing fact {fact_id}")
            for document_id in anomaly.document_ids:
                if document_id not in document_ids:
                    errors.append(
                        f"anomaly {anomaly.anomaly_id} references missing document {document_id}"
                    )
            if anomaly.governing_fact_id and anomaly.governing_fact_id not in fact_ids:
                errors.append(
                    f"anomaly {anomaly.anomaly_id} has unknown governing fact "
                    f"{anomaly.governing_fact_id}"
                )

        for missing in self.missing_documents:
            if missing in document_ids:
                errors.append(f"missing_documents lists present document {missing}")

        if errors:
            raise ValueError("invalid scenario: " + "; ".join(errors))
        return self


class ScenarioRequest(StrictModel):
    contract_type: ContractType
    count: int = Field(default=1, ge=1)
    clauses: int = Field(default=10, ge=1, le=60)
    difficulty: int = Field(default=1, ge=1, le=6)
    language: Language = Language.SV
    jurisdiction: str = "SE"
    seed: int | None = None
    contract_id_prefix: str = "contract"
    package: bool = True
    variation_overrides: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Generated + rendered documents
# ---------------------------------------------------------------------------


class GeneratedClause(StrictModel):
    clause_id: str
    document_id: str
    heading: str
    text: str


class GeneratedDocument(StrictModel):
    document_id: str
    kind: DocKind
    title: str
    clauses: list[GeneratedClause] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)

    def clause(self, clause_id: str) -> GeneratedClause:
        for clause in self.clauses:
            if clause.clause_id == clause_id:
                return clause
        raise KeyError(f"unknown generated clause {clause_id!r}")

    @model_validator(mode="after")
    def _unique_clauses(self) -> GeneratedDocument:
        seen: set[str] = set()
        for clause in self.clauses:
            if clause.clause_id in seen:
                raise ValueError(f"duplicate generated clause {clause.clause_id}")
            seen.add(clause.clause_id)
            if clause.document_id != self.document_id:
                raise ValueError(
                    f"generated clause {clause.clause_id} belongs to {clause.document_id}, "
                    f"not {self.document_id}"
                )
        return self


class GeneratedPackage(StrictModel):
    contract_id: str
    documents: list[GeneratedDocument] = Field(default_factory=list)

    def document(self, document_id: str) -> GeneratedDocument:
        for document in self.documents:
            if document.document_id == document_id:
                return document
        raise KeyError(f"unknown generated document {document_id!r}")

    @model_validator(mode="after")
    def _unique_documents(self) -> GeneratedPackage:
        seen: set[str] = set()
        for document in self.documents:
            if document.document_id in seen:
                raise ValueError(f"duplicate generated document {document.document_id}")
            seen.add(document.document_id)
        return self


class TextSpan(StrictModel):
    """Character span in a rendered document's canonical text.

    ``start`` is inclusive, ``end`` exclusive, offsets are Unicode code points
    in the UTF-8 decoded canonical text.
    """

    document_id: str
    clause_id: str | None = None
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> TextSpan:
        if self.end <= self.start:
            raise ValueError(f"invalid span {self.start}:{self.end}")
        return self

    def slice(self, canonical_text: str) -> str:
        return canonical_text[self.start : self.end]


class RenderedDocument(StrictModel):
    document_id: str
    kind: DocKind
    title: str
    language: Language
    canonical_text: str
    clause_spans: dict[str, TextSpan] = Field(default_factory=dict)

    def span_text(self, clause_id: str) -> str:
        return self.clause_spans[clause_id].slice(self.canonical_text)

    def validate_spans(self) -> list[str]:
        problems: list[str] = []
        length = len(self.canonical_text)
        for clause_id, span in self.clause_spans.items():
            if span.document_id != self.document_id:
                problems.append(f"span for {clause_id} points at {span.document_id}")
            if span.end > length:
                problems.append(
                    f"span for {clause_id} ends at {span.end} beyond text length {length}"
                )
            if span.clause_id is not None and span.clause_id != clause_id:
                problems.append(f"span for {clause_id} carries clause_id {span.clause_id}")
        return problems

    @model_validator(mode="after")
    def _check_spans(self) -> RenderedDocument:
        problems = self.validate_spans()
        if problems:
            raise ValueError("invalid rendered document: " + "; ".join(problems))
        return self


# ---------------------------------------------------------------------------
# Evidence + gold
# ---------------------------------------------------------------------------


class EvidenceSpan(StrictModel):
    document_id: str
    clause_id: str | None = None
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: str = ""

    @model_validator(mode="after")
    def _ordered(self) -> EvidenceSpan:
        if self.end <= self.start:
            raise ValueError(f"invalid evidence span {self.start}:{self.end}")
        return self


class GoldFact(StrictModel):
    fact_id: str
    kind: FactKind
    value: Any
    unit: str | None = None
    currency: str | None = None
    entity_id: str | None = None
    status: FactStatus = FactStatus.ACTIVE
    precision: EvidencePrecision = EvidencePrecision.EXACT
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)
    notes_sv: str = ""


class GoldEntity(StrictModel):
    entity_id: str
    name: str
    role: str
    evidence: list[EvidenceSpan] = Field(default_factory=list)


class GoldReference(StrictModel):
    source_document_id: str
    source_clause_id: str | None = None
    target_document_id: str
    target_clause_id: str | None = None
    kind: RelationKind = RelationKind.REFERENCES
    evidence: list[EvidenceSpan] = Field(default_factory=list)


class GoldAnomaly(StrictModel):
    anomaly_id: str
    kind: AnomalyKind
    answerability: Answerability
    fact_ids: list[str] = Field(default_factory=list)
    governing_fact_id: str | None = None
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    description_sv: str = ""


class GoldSet(StrictModel):
    schema_version: str = SCHEMA_VERSION
    contract_id: str
    facts: list[GoldFact] = Field(default_factory=list)
    entities: list[GoldEntity] = Field(default_factory=list)
    references: list[GoldReference] = Field(default_factory=list)
    anomalies: list[GoldAnomaly] = Field(default_factory=list)
    missing_documents: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)

    def fact(self, fact_id: str) -> GoldFact:
        for fact in self.facts:
            if fact.fact_id == fact_id:
                return fact
        raise KeyError(f"unknown gold fact {fact_id!r}")

    @model_validator(mode="after")
    def _unique_facts(self) -> GoldSet:
        seen: set[str] = set()
        for fact in self.facts:
            if fact.fact_id in seen:
                raise ValueError(f"duplicate gold fact {fact.fact_id}")
            seen.add(fact.fact_id)
        return self


# ---------------------------------------------------------------------------
# Golden questions
# ---------------------------------------------------------------------------


class ExpectedAnswer(StrictModel):
    value: Any = None
    values: list[Any] = Field(default_factory=list)
    unit: str | None = None
    currency: str | None = None
    text: str = ""
    conditions: list[str] = Field(default_factory=list)


class CompletenessRequirement(StrictModel):
    kind: Literal["all_parties", "all_documents", "all_values"] = "all_values"
    expected_ids: list[str] = Field(default_factory=list)
    description_sv: str = ""


class GoldenQuestion(StrictModel):
    question_id: str
    question: str
    language: Language
    category: QuestionCategory
    difficulty: int = Field(ge=1, le=6)
    expected_answer: ExpectedAnswer
    required_facts: list[str] = Field(default_factory=list)
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    answerability: Answerability
    completeness: CompletenessRequirement | None = None
    validated: bool = False
    validation_notes: list[str] = Field(default_factory=list)
    source: Literal["deterministic", "llm", "hybrid"] = "deterministic"

    @model_validator(mode="after")
    def _answerable_has_answer(self) -> GoldenQuestion:
        if self.answerability == Answerability.ANSWERABLE:
            has_answer = (
                self.expected_answer.value is not None
                or bool(self.expected_answer.values)
                or bool(self.expected_answer.text)
            )
            if not has_answer:
                raise ValueError(f"answerable question {self.question_id} has no expected answer")
            if not self.evidence and not self.required_facts:
                raise ValueError(f"question {self.question_id} has no evidence or required facts")
        return self


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class CheckResult(StrictModel):
    check_id: str
    status: CheckStatus
    message: str = ""
    document_id: str | None = None
    clause_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class VerificationStats(StrictModel):
    documents_total: int = 0
    documents_accepted: int = 0
    documents_repaired: int = 0
    documents_rejected: int = 0
    checks_total: int = 0
    checks_failed: int = 0
    checks_warned: int = 0


class DocumentVerification(StrictModel):
    document_id: str
    status: DocumentStatus = DocumentStatus.ACCEPTED
    checks: list[CheckResult] = Field(default_factory=list)
    attempts: int = 1
    reasons: list[str] = Field(default_factory=list)

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [check for check in self.checks if check.status == CheckStatus.FAIL]


class PackageVerification(StrictModel):
    contract_id: str
    status: DocumentStatus = DocumentStatus.ACCEPTED
    documents: list[DocumentVerification] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    stats: VerificationStats = Field(default_factory=VerificationStats)
    attempts: int = 1

    def recompute_stats(self) -> VerificationStats:
        documents = self.documents
        checks = [check for document in documents for check in document.checks] + self.checks
        self.stats = VerificationStats(
            documents_total=len(documents),
            documents_accepted=sum(
                1 for document in documents if document.status == DocumentStatus.ACCEPTED
            ),
            documents_repaired=sum(
                1 for document in documents if document.status == DocumentStatus.REPAIRED
            ),
            documents_rejected=sum(
                1 for document in documents if document.status == DocumentStatus.REJECTED
            ),
            checks_total=len(checks),
            checks_failed=sum(1 for check in checks if check.status == CheckStatus.FAIL),
            checks_warned=sum(1 for check in checks if check.status == CheckStatus.WARN),
        )
        if any(document.status == DocumentStatus.REJECTED for document in documents):
            self.status = DocumentStatus.REJECTED
        elif any(document.status == DocumentStatus.REPAIRED for document in documents):
            self.status = DocumentStatus.REPAIRED
        else:
            self.status = DocumentStatus.ACCEPTED
        return self.stats

    @property
    def failed_checks(self) -> list[CheckResult]:
        checks = [check for document in self.documents for check in document.checks]
        return [check for check in checks if check.status == CheckStatus.FAIL]


# ---------------------------------------------------------------------------
# Provider boundary
# ---------------------------------------------------------------------------


class LLMRequest(StrictModel):
    provider: str
    model: str
    prompt_version: str
    system: str
    user: str
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    response_format: Literal["text", "json"] = "json"
    metadata: dict[str, Any] = Field(default_factory=dict)


class Usage(StrictModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class LLMResponse(StrictModel):
    text: str
    provider: str
    model: str
    usage: Usage = Field(default_factory=Usage)
    latency_ms: float = 0.0
    finish_reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


class LLMCallRecord(StrictModel):
    call_id: str
    purpose: str
    contract_id: str
    document_id: str | None = None
    attempt: int = 1
    request: LLMRequest
    response_text: str
    usage: Usage = Field(default_factory=Usage)
    latency_ms: float = 0.0
    finish_reason: str | None = None
    provider: str
    model: str
    timestamp: datetime = Field(default_factory=utc_now)
    error: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


class ModelPrice(StrictModel):
    input_usd_per_million: float = Field(ge=0)
    output_usd_per_million: float = Field(ge=0)


class PricingTable(StrictModel):
    """Explicit, user-supplied price table. No price is hardcoded in the code."""

    providers: dict[str, dict[str, ModelPrice]] = Field(default_factory=dict)

    def price_for(self, provider: str, model: str) -> ModelPrice | None:
        return self.providers.get(provider, {}).get(model)


class CostSummary(StrictModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tokens_known: bool = True
    cost_usd: float | None = None
    cost_basis: str = "unknown"  # "configured_price_table" | "unknown"
    mean_latency_ms: float | None = None


# ---------------------------------------------------------------------------
# Configuration snapshots + manifest
# ---------------------------------------------------------------------------


class ScenarioDefinitionRecord(StrictModel):
    """Identity of one contract-type definition file used for a run."""

    contract_type: str
    path: str
    sha256: str


class RuntimeIdentity(StrictModel):
    """Runtime that produced a dataset, so a run can be reproduced and audited."""

    contractforge_version: str = ""
    python_version: str = ""
    python_implementation: str = ""
    platform: str = ""
    git_commit: str | None = None
    git_dirty: bool | None = None
    dependencies: dict[str, str] = Field(default_factory=dict)


class GenerationConfigSnapshot(StrictModel):
    contract_type: ContractType
    count: int = 1
    clauses: int = 10
    difficulty: int = Field(ge=1, le=6)
    language: Language
    jurisdiction: str
    provider: str
    model: str
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    concurrency: int = 1
    max_repair_attempts: int = 2
    prompt_version: str = "v1"
    prompt_sha256: str = ""
    formats: list[DocumentFormat] = Field(default_factory=list)
    contract_id_prefix: str = "contract"
    # Complete scenario inputs: two runs that differ only here must not produce
    # identical config snapshots.
    package: bool = True
    variation_overrides: dict[str, Any] = Field(default_factory=dict)
    scenario_definitions: list[ScenarioDefinitionRecord] = Field(default_factory=list)
    runtime: RuntimeIdentity = Field(default_factory=RuntimeIdentity)


class ArtifactRecord(StrictModel):
    path: str  # relative to the contract directory
    sha256: str
    size_bytes: int = 0
    kind: Literal[
        "document",
        "scenario",
        "gold_facts",
        "golden_questions",
        "verification",
        "llm_calls",
        "raw_response",
        "manifest",
        "other",
    ] = "other"


class DocumentRecord(StrictModel):
    document_id: str
    kind: DocKind
    title: str
    canonical_sha256: str
    files: list[ArtifactRecord] = Field(default_factory=list)


class VerificationSummary(StrictModel):
    status: DocumentStatus
    documents_total: int = 0
    documents_accepted: int = 0
    documents_repaired: int = 0
    documents_rejected: int = 0
    checks_failed: int = 0
    checks_warned: int = 0


class Manifest(StrictModel):
    schema_version: str = SCHEMA_VERSION
    contractforge_version: str
    generated_at: datetime = Field(default_factory=utc_now)
    synthetic: bool = True
    contract_id: str
    contract_type: ContractType
    difficulty: int
    language: Language
    jurisdiction: str
    provider: str
    model: str
    prompt_version: str
    prompt_sha256: str
    seed: int | None = None
    generation_config: GenerationConfigSnapshot
    documents: list[DocumentRecord] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    verification: VerificationSummary
    cost: CostSummary = Field(default_factory=CostSummary)
    timings: dict[str, float] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class DatasetContractRecord(StrictModel):
    contract_id: str
    path: str
    status: DocumentStatus
    manifest_sha256: str


class DatasetStats(StrictModel):
    schema_version: str = SCHEMA_VERSION
    generated_at: datetime = Field(default_factory=utc_now)
    contracts_total: int = 0
    contracts_accepted: int = 0
    contracts_rejected: int = 0
    documents_total: int = 0
    documents_accepted: int = 0
    documents_repaired: int = 0
    documents_rejected: int = 0
    clauses_total: int = 0
    facts_total: int = 0
    questions_total: int = 0
    questions_validated: int = 0
    questions_rejected: int = 0
    questions_answerable: int = 0
    questions_insufficient_evidence: int = 0
    questions_conditional: int = 0
    questions_conflicting_evidence: int = 0
    calls_total: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tokens_known: bool = True
    cost_usd: float | None = None
    cost_basis: str = "unknown"
    total_seconds: float | None = None
    documents_per_minute: float | None = None
    clauses_per_minute: float | None = None
    mean_document_latency_ms: float | None = None
    cost_per_accepted_document_usd: float | None = None
    cost_per_validated_question_usd: float | None = None
    verification_pass_rate: float = 0.0
    repair_rate: float = 0.0
    rejection_rate: float = 0.0


class RunRecord(StrictModel):
    """What one generation run owns inside its output directory."""

    contract_id_prefix: str
    count: int
    contract_ids: list[str] = Field(default_factory=list)
    data_files: list[str] = Field(default_factory=list)


class DatasetManifest(StrictModel):
    schema_version: str = SCHEMA_VERSION
    contractforge_version: str
    generated_at: datetime = Field(default_factory=utc_now)
    synthetic: bool = True
    config: GenerationConfigSnapshot
    contracts: list[DatasetContractRecord] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    totals: DatasetStats = Field(default_factory=DatasetStats)
    run: RunRecord | None = None
    notes: list[str] = Field(default_factory=list)


class DatasetValidationReport(StrictModel):
    root: str
    generated_at: datetime = Field(default_factory=utc_now)
    contracts_checked: int = 0
    contracts_passed: int = 0
    contracts_failed: int = 0
    checks: list[CheckResult] = Field(default_factory=list)
    status: CheckStatus = CheckStatus.PASS
    errors: list[str] = Field(default_factory=list)

    def finalize(self) -> DatasetValidationReport:
        failures = [check for check in self.checks if check.status == CheckStatus.FAIL]
        self.contracts_failed = len({check.details.get("contract_id") for check in failures} - {None})
        self.contracts_passed = max(0, self.contracts_checked - self.contracts_failed)
        self.status = CheckStatus.FAIL if failures or self.errors else CheckStatus.PASS
        return self


class BenchmarkReport(StrictModel):
    schema_version: str = SCHEMA_VERSION
    benchmark_id: str
    created_at: datetime = Field(default_factory=utc_now)
    provider: str
    model: str
    provider_is_mock: bool
    seed: int | None
    config: GenerationConfigSnapshot
    stats: DatasetStats
    details: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# In-flight pipeline result + loaded dataset
# ---------------------------------------------------------------------------


class ContractResult(StrictModel):
    """Everything produced for one contract, before it is written to disk."""

    contract_id: str
    scenario: Scenario
    package: GeneratedPackage
    rendered: dict[str, RenderedDocument]
    verification: PackageVerification
    gold: GoldSet
    questions: list[GoldenQuestion] = Field(default_factory=list)
    rejected_questions: list[GoldenQuestion] = Field(default_factory=list)
    llm_calls: list[LLMCallRecord] = Field(default_factory=list)
    cost: CostSummary = Field(default_factory=CostSummary)
    config: GenerationConfigSnapshot
    status: DocumentStatus = DocumentStatus.ACCEPTED
    attempts: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    timings: dict[str, float] = Field(default_factory=dict)

    def canonical_texts(self) -> dict[str, str]:
        return {
            document_id: rendered.canonical_text
            for document_id, rendered in self.rendered.items()
        }


@dataclass
class LoadedContract:
    """A contract read back from an exported dataset directory."""

    root: Path
    scenario: Scenario
    manifest: Manifest
    texts: dict[str, str] = field(default_factory=dict)
    rendered: dict[str, RenderedDocument] = field(default_factory=dict)
    gold: GoldSet | None = None
    questions: list[GoldenQuestion] = field(default_factory=list)
    verification: PackageVerification | None = None
    load_errors: list[str] = field(default_factory=list)

    @property
    def contract_id(self) -> str:
        return self.scenario.contract_id
