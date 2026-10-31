"""Deterministic golden-question generation with text-grounded verification.

Questions are generated from the scenario truth and the verified gold set using
fixed templates per category and language (Swedish / English). Every candidate
is then verified against the canonical text: evidence spans must exist, quotes
must match their slices, required facts must exist in gold, and the expected
answer must really be supported by the cited evidence. Questions that fail are
moved to ``rejected`` and are never exported as validated facit.

Answerability follows difficulty:

* ``ANSWERABLE`` – levels 1–4: explicit facts, entities, relations,
  comparisons, conditions, legal reasoning and completeness.
* ``CONFLICTING_EVIDENCE`` – level 5: intentional contradictions.
* ``CONDITIONAL`` / ``INSUFFICIENT_EVIDENCE`` – level 6: conditions and missing
  information.

Machine-verifiable premises are stored in ``expected_answer.conditions`` with
stable prefixes (``missing_document:``, ``anomaly:``, ``clause:``,
``document:``) so verification can prove the premise instead of trusting the
generator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import (
    Answerability,
    ClauseMode,
    ClauseSpec,
    CompletenessRequirement,
    EvidencePrecision,
    EvidenceSpan,
    ExpectedAnswer,
    GoldenQuestion,
    GoldSet,
    Language,
    QuestionCategory,
    RelationKind,
    RenderedDocument,
    Scenario,
)
from ..utils import contains_span_text, find_span, normalize_for_search

__all__ = ["QuestionOutcome", "generate_and_verify_questions"]

MISSING_PREFIX = "missing_document:"
ANOMALY_PREFIX = "anomaly:"
CLAUSE_PREFIX = "clause:"
DOCUMENT_PREFIX = "document:"

_DEPENDENCY_KINDS = {
    RelationKind.DEPENDS_ON,
    RelationKind.CONDITIONS,
    RelationKind.MODIFIES,
    RelationKind.CLARIFIES,
    RelationKind.SUPERSEDES,
    RelationKind.DEFINES,
}


class _Stop(Exception):
    """Raised when ``max_questions`` accepted questions have been produced."""


@dataclass
class QuestionOutcome:
    accepted: list[GoldenQuestion]
    rejected: list[GoldenQuestion]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return normalize_for_search(text)[0]


def _contains(haystack: str, needle: str) -> bool:
    target = _norm(needle)
    return bool(target) and target in _norm(haystack)


def _text(language: Language, sv: str, en: str) -> str:
    return sv if language is Language.SV else en


def _label(fact: Any, language: Language) -> str:
    if language is Language.EN and getattr(fact, "label_en", ""):
        return fact.label_en
    return getattr(fact, "label_sv", "") or getattr(fact, "fact_id", "")


def _doc_title(scenario: Scenario, document_id: str, language: Language) -> str:
    for document in scenario.documents:
        if document.document_id == document_id:
            return document.title(language)
    return document_id


def _span_slice(span: EvidenceSpan, rendered: dict[str, RenderedDocument]) -> str | None:
    document = rendered.get(span.document_id)
    if document is None:
        return None
    text = document.canonical_text
    if not 0 <= span.start < span.end <= len(text):
        return None
    return text[span.start : span.end]


def _evidence_text(
    spans: list[EvidenceSpan], rendered: dict[str, RenderedDocument]
) -> str:
    parts = [_span_slice(span, rendered) for span in spans]
    return "\n".join(part for part in parts if part)


def _evidence_problems(
    spans: list[EvidenceSpan], rendered: dict[str, RenderedDocument]
) -> list[str]:
    problems: list[str] = []
    for index, span in enumerate(spans):
        document = rendered.get(span.document_id)
        if document is None:
            problems.append(
                f"evidensspann {index} pekar på okänt dokument {span.document_id!r}"
            )
            continue
        text = document.canonical_text
        if not 0 <= span.start < span.end <= len(text):
            problems.append(
                f"evidensspann {index} [{span.start}:{span.end}] ligger utanför "
                f"{span.document_id}"
            )
            continue
        slice_text = text[span.start : span.end]
        if not span.quote.strip():
            problems.append(f"evidensspann {index} saknar quote")
        elif not contains_span_text(slice_text, 0, len(slice_text), span.quote):
            problems.append(
                f"evidensspann {index}: quote {span.quote!r} matchar inte slice "
                f"{slice_text!r}"
            )
        if span.clause_id is not None:
            clause_span = document.clause_spans.get(span.clause_id)
            if clause_span is None:
                problems.append(f"evidensspann {index} anger okänd klausul {span.clause_id!r}")
            elif span.start < clause_span.start or span.end > clause_span.end:
                problems.append(
                    f"evidensspann {index} ligger utanför klausul {span.clause_id}"
                )
    return problems


def _span_covers(outer: EvidenceSpan, inner: EvidenceSpan) -> bool:
    return (
        outer.document_id == inner.document_id
        and outer.start <= inner.start
        and inner.end <= outer.end
    )


def _value_key(value: Any) -> str:
    if isinstance(value, bool):
        return f"bool:{value}"
    if isinstance(value, (int, float)):
        return f"num:{float(value)!r}"
    return f"text:{_norm(str(value))}"


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) == bool(right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return _value_key(left) == _value_key(right)


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_UNIT_SYNONYMS: dict[str, str] = {
    "month": "months",
    "months": "months",
    "månad": "months",
    "månader": "months",
    "mån": "months",
    "year": "years",
    "years": "years",
    "år": "years",
    "sek": "sek",
    "kr": "sek",
    "kronor": "sek",
    "krona": "sek",
    "%": "percent",
    "procent": "percent",
    "percent": "percent",
}


def _units_equal(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    normalized_left = _UNIT_SYNONYMS.get(left.strip().lower(), left.strip().lower())
    normalized_right = _UNIT_SYNONYMS.get(right.strip().lower(), right.strip().lower())
    return normalized_left == normalized_right


def _value_literal_in(value: Any, text: str) -> bool:
    if text == "":
        return False
    normalized_text = _norm(text)
    if isinstance(value, str):
        return bool(_norm(value)) and _norm(value) in normalized_text
    if isinstance(value, bool):
        return ("ja" in normalized_text) == value or (
            ("nej" in normalized_text) and not value
        )
    number = _numeric_value(value)
    if number is None:
        return _norm(str(value)) in normalized_text
    candidates = {f"{number:g}", str(int(number)) if number.is_integer() else f"{number:g}"}
    return any(candidate in normalized_text for candidate in candidates if candidate)


def _clause_spec(scenario: Scenario, clause_id: str) -> ClauseSpec | None:
    for document in scenario.documents:
        for clause in document.clauses:
            if clause.clause_id == clause_id:
                return clause
    return None


def _document_for_clause(scenario: Scenario, clause_id: str) -> str | None:
    for document in scenario.documents:
        for clause in document.clauses:
            if clause.clause_id == clause_id:
                return document.document_id
    return None


def _resolve(
    scenario: Scenario, token: str
) -> tuple[str | None, str | None, str | None]:
    if token.startswith("fact:"):
        fact_id = token[len("fact:") :]
        for document in scenario.documents:
            for clause in document.clauses:
                if fact_id in clause.fact_ids:
                    return document.document_id, clause.clause_id, fact_id
        return None, None, fact_id
    if ":" in token:
        document_id, clause_id = token.split(":", 1)
        for document in scenario.documents:
            if document.document_id == document_id:
                if any(clause.clause_id == clause_id for clause in document.clauses):
                    return document_id, clause_id, None
                return document_id, None, None
        return document_id, clause_id, None
    if token in scenario.missing_documents:
        return token, None, None
    for document in scenario.documents:
        if document.document_id == token:
            return token, None, None
    for document in scenario.documents:
        for clause in document.clauses:
            if clause.clause_id == token:
                return document.document_id, clause.clause_id, None
    for fact in scenario.facts:
        if fact.fact_id == token:
            for document in scenario.documents:
                for clause in document.clauses:
                    if fact.fact_id in clause.fact_ids:
                        return document.document_id, clause.clause_id, fact.fact_id
            return None, None, fact.fact_id
    return None, None, None


def _clause_evidence(
    scenario: Scenario,
    rendered: dict[str, RenderedDocument],
    document_id: str | None,
    clause_id: str | None,
) -> EvidenceSpan | None:
    if document_id is None or clause_id is None:
        return None
    document = rendered.get(document_id)
    if document is None:
        return None
    span = document.clause_spans.get(clause_id)
    if span is None:
        return None
    return EvidenceSpan(
        document_id=document_id,
        clause_id=clause_id,
        start=span.start,
        end=span.end,
        quote=document.canonical_text[span.start : span.end],
    )


def _anomaly_label(scenario: Scenario, anomaly: Any, language: Language) -> str:
    for fact_id in anomaly.fact_ids:
        try:
            return _label(scenario.fact(fact_id), language)
        except KeyError:
            continue
    return anomaly.anomaly_id


def _clause_for_anomaly(scenario: Scenario, anomaly: Any) -> tuple[str | None, str | None]:
    for document in scenario.documents:
        for clause in document.clauses:
            if set(clause.fact_ids) & set(anomaly.fact_ids):
                return document.document_id, clause.clause_id
    for document in scenario.documents:
        if document.document_id not in anomaly.document_ids:
            continue
        for clause in document.clauses:
            if clause.mode in (
                ClauseMode.CONDITIONAL,
                ClauseMode.EXCEPTION,
                ClauseMode.CONTRADICTION,
            ):
                return document.document_id, clause.clause_id
        if document.clauses:
            return document.document_id, document.clauses[0].clause_id
    return None, None


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _claim_problems(
    question: GoldenQuestion,
    value: Any,
    unit: str | None,
    currency: str | None,
    *,
    gold_facts: dict[str, Any],
    scenario_facts: dict[str, Any],
    evidence_text: str,
    rendered: dict[str, RenderedDocument],
) -> list[str]:
    candidates: list[Any] = []
    for fact_id in question.required_facts:
        gold_fact = gold_facts.get(fact_id)
        if gold_fact is None or not _values_equal(gold_fact.value, value):
            continue
        # The gold set must agree with the scenario truth; a corrupted gold
        # value can never make a question valid.
        scenario_fact = scenario_facts.get(fact_id)
        if scenario_fact is not None and not _values_equal(
            gold_fact.value, scenario_fact.value
        ):
            continue
        if not _units_equal(gold_fact.unit, unit):
            continue
        if currency is not None and gold_fact.currency != currency:
            continue
        candidates.append(gold_fact)
    for gold_fact in candidates:
        if gold_fact.precision is EvidencePrecision.CLAUSE:
            continue
        if not gold_fact.evidence:
            continue
        if all(
            any(_span_covers(question_span, gold_span) for question_span in question.evidence)
            for gold_span in gold_fact.evidence
        ):
            return []
    if _value_literal_in(value, evidence_text):
        return []
    if question.category is QuestionCategory.COMPLETENESS:
        canonical_text = "\n".join(document.canonical_text for document in rendered.values())
        if _value_literal_in(value, canonical_text):
            return []
    return [f"förväntat svar {value!r} stöds inte av frågans evidensspann"]


def _clause_condition_problems(
    condition: str,
    question: GoldenQuestion,
    scenario: Scenario,
    rendered: dict[str, RenderedDocument],
    evidence_text: str,
) -> list[str]:
    payload = condition[len(CLAUSE_PREFIX) :]
    parts = payload.split(":", 1)
    if len(parts) != 2:
        return [f"ogiltig klausulpremiss {condition!r}"]
    document_id, clause_id = parts
    clause = _clause_spec(scenario, clause_id)
    if clause is None or _document_for_clause(scenario, clause_id) != document_id:
        return [f"premissen {condition!r} pekar på en okänd klausul"]
    heading = clause.heading(question.language)
    if not _contains(evidence_text, heading):
        return [f"klausulpremissens rubrik {heading!r} saknas i evidensen"]
    return []


def _condition_problems(
    question: GoldenQuestion,
    scenario: Scenario,
    rendered: dict[str, RenderedDocument],
    evidence_text: str,
) -> list[str]:
    conditions = list(question.expected_answer.conditions)
    if not conditions:
        return ["frågan saknar verifierbara premisser i expected_answer.conditions"]
    problems: list[str] = []
    for condition in conditions:
        if condition.startswith(MISSING_PREFIX):
            document_id = condition[len(MISSING_PREFIX) :]
            if document_id not in scenario.missing_documents:
                problems.append(f"premissen {condition!r} är inte deklarerad som saknad")
            if document_id in rendered:
                problems.append(
                    f"premissen {condition!r} är falsk: dokumentet finns i rendered"
                )
        elif condition.startswith(ANOMALY_PREFIX):
            anomaly_id = condition[len(ANOMALY_PREFIX) :]
            anomaly = next(
                (item for item in scenario.anomalies if item.anomaly_id == anomaly_id), None
            )
            if anomaly is None:
                problems.append(f"premissen {condition!r} pekar på en okänd anomali")
            elif (
                question.answerability is Answerability.CONDITIONAL
                and anomaly.answerability is not Answerability.CONDITIONAL
            ):
                problems.append(
                    f"anomalin {anomaly_id} är inte deklarerad CONDITIONAL"
                )
            elif (
                question.answerability is Answerability.INSUFFICIENT_EVIDENCE
                and anomaly.answerability is not Answerability.INSUFFICIENT_EVIDENCE
            ):
                problems.append(
                    f"anomalin {anomaly_id} är inte deklarerad INSUFFICIENT_EVIDENCE"
                )
        elif condition.startswith(CLAUSE_PREFIX):
            problems.extend(
                _clause_condition_problems(condition, question, scenario, rendered, evidence_text)
            )
        elif condition.startswith(DOCUMENT_PREFIX):
            document_id = condition[len(DOCUMENT_PREFIX) :]
            known = {document.document_id for document in scenario.documents}
            if document_id not in known and document_id not in scenario.missing_documents:
                problems.append(f"premissen {condition!r} pekar på ett okänt dokument")
            if document_id in scenario.missing_documents and (
                question.answerability is Answerability.ANSWERABLE
            ):
                problems.append(
                    f"premissen {condition!r} avser ett saknat dokument men frågan är ANSWERABLE"
                )
        else:
            problems.append(f"okänd premistyp {condition!r}")
    return problems


def _relation_text_problems(
    question: GoldenQuestion, rendered: dict[str, RenderedDocument]
) -> list[str]:
    target = question.expected_answer.text
    if not target:
        return []
    candidates: list[str] = [target]
    # Long document titles are often referenced by a short form ("Bilaga A").
    for separator in ("–", "—", "-", ":"):
        if separator in target:
            short = target.split(separator, 1)[0].strip()
            if short:
                candidates.append(short)
    for condition in question.expected_answer.conditions:
        if condition.startswith(DOCUMENT_PREFIX):
            candidates.append(condition[len(DOCUMENT_PREFIX) :])
    for document_id in {span.document_id for span in question.evidence}:
        document = rendered.get(document_id)
        if document is None:
            continue
        for candidate in candidates:
            if find_span(document.canonical_text, candidate) is not None:
                return []
    return [f"relationssvaret {target!r} stöds inte av källdokumentets kanoniska text"]


def _contradiction_problems(
    question: GoldenQuestion, gold_facts: dict[str, Any]
) -> list[str]:
    facts = [
        gold_facts[fact_id] for fact_id in question.required_facts if fact_id in gold_facts
    ]
    problems: list[str] = []
    if len(facts) < 2:
        problems.append("motsägelsefrågan kräver minst två gold-fakta")
        return problems
    value_keys = {_value_key(fact.value) for fact in facts}
    if len(value_keys) < 2:
        problems.append("motsägelsefrågan kräver minst två olika gold-värden")
    span_keys = {
        (span.document_id, span.start, span.end)
        for fact in facts
        for span in fact.evidence
    }
    if len(span_keys) < 2:
        problems.append("motsägelsefrågan kräver evidens från minst två spann")
    expected_keys = {_value_key(value) for value in question.expected_answer.values}
    if not expected_keys <= value_keys:
        problems.append("förväntade värden matchar inte gold-värdena")
    return problems


def _completeness_problems(
    question: GoldenQuestion,
    scenario: Scenario,
    gold_facts: dict[str, Any],
    rendered: dict[str, RenderedDocument],
    evidence_text: str,
) -> list[str]:
    requirement = question.completeness
    if requirement is None:
        return ["flervärdesfrågan saknar CompletenessRequirement"]
    problems: list[str] = []
    if requirement.kind == "all_parties":
        expected_ids = {party.party_id for party in scenario.parties}
        if set(requirement.expected_ids) != expected_ids:
            problems.append(
                f"completeness för parter {requirement.expected_ids} matchar inte "
                f"scenariots parter {sorted(expected_ids)}"
            )
        for name in question.expected_answer.values:
            if not _value_literal_in(name, evidence_text):
                problems.append(f"parten {name!r} saknar stöd i evidensen")
    elif requirement.kind == "all_documents":
        expected_ids = {document.document_id for document in scenario.documents}
        if set(requirement.expected_ids) != expected_ids:
            problems.append(
                f"completeness för dokument {requirement.expected_ids} matchar inte "
                f"scenariots dokument {sorted(expected_ids)}"
            )
        covered = {span.document_id for span in question.evidence}
        missing_evidence = sorted(expected_ids - covered)
        if missing_evidence:
            problems.append(
                f"dokumentfrågan saknar evidensspann för dokumenten {missing_evidence}"
            )
        for document_id in expected_ids:
            document = rendered.get(document_id)
            if document is None:
                problems.append(f"dokumentet {document_id!r} är inte renderat")
                continue
            title = _doc_title(scenario, document_id, question.language)
            if find_span(document.canonical_text, title) is None:
                problems.append(
                    f"dokumenttiteln {title!r} saknas i kanonisk text för {document_id}"
                )
    elif requirement.kind == "all_values":
        if not requirement.expected_ids:
            problems.append("all_values kräver minst ett faktum-id")
        for fact_id in requirement.expected_ids:
            gold_fact = gold_facts.get(fact_id)
            if gold_fact is None:
                problems.append(f"okänt faktum {fact_id!r} i completeness")
                continue
            if not isinstance(gold_fact.value, list):
                problems.append(f"faktum {fact_id!r} har inte ett listvärde")
                continue
            for item in gold_fact.value:
                if not _value_literal_in(item, evidence_text):
                    problems.append(f"listvärdet {item!r} saknar stöd i evidensen")
    return problems


def _verify_question(
    question: GoldenQuestion,
    scenario: Scenario,
    gold: GoldSet,
    rendered: dict[str, RenderedDocument],
) -> list[str]:
    problems: list[str] = _evidence_problems(question.evidence, rendered)
    gold_facts = {gold_fact.fact_id: gold_fact for gold_fact in gold.facts}
    if question.answerability is Answerability.ANSWERABLE and not question.evidence:
        problems.append("ANSWERABLE fråga saknar evidensspann")
    for fact_id in question.required_facts:
        gold_fact = gold_facts.get(fact_id)
        if gold_fact is None:
            problems.append(f"required_fact {fact_id!r} saknas i gold")
        elif not gold_fact.evidence and question.answerability in (
            Answerability.ANSWERABLE,
            Answerability.CONFLICTING_EVIDENCE,
        ):
            problems.append(f"required_fact {fact_id!r} saknar evidens i gold")
    evidence_text = _evidence_text(question.evidence, rendered)
    scenario_facts = {fact.fact_id: fact for fact in scenario.facts}

    if question.expected_answer.value is not None:
        problems.extend(
            _claim_problems(
                question,
                question.expected_answer.value,
                question.expected_answer.unit,
                question.expected_answer.currency,
                gold_facts=gold_facts,
                scenario_facts=scenario_facts,
                evidence_text=evidence_text,
                rendered=rendered,
            )
        )
    for value in question.expected_answer.values:
        problems.extend(
            _claim_problems(
                question,
                value,
                question.expected_answer.unit,
                question.expected_answer.currency,
                gold_facts=gold_facts,
                scenario_facts=scenario_facts,
                evidence_text=evidence_text,
                rendered=rendered,
            )
        )
    if question.expected_answer.text:
        if question.category is QuestionCategory.RELATION:
            if question.answerability is Answerability.ANSWERABLE:
                problems.extend(_relation_text_problems(question, rendered))
        elif question.answerability in (
            Answerability.ANSWERABLE,
            Answerability.CONDITIONAL,
        ) and not _contains(evidence_text, question.expected_answer.text):
            problems.append(
                f"förväntat svar {question.expected_answer.text!r} saknas i evidensen"
            )
    if question.category is QuestionCategory.CONTRADICTION:
        problems.extend(_contradiction_problems(question, gold_facts))
    if question.answerability in (
        Answerability.CONDITIONAL,
        Answerability.INSUFFICIENT_EVIDENCE,
    ) or question.category in (
        QuestionCategory.CONDITIONAL,
        QuestionCategory.ABSENCE,
        QuestionCategory.INSUFFICIENT_EVIDENCE,
    ):
        problems.extend(_condition_problems(question, scenario, rendered, evidence_text))
    if question.completeness is not None:
        problems.extend(
            _completeness_problems(question, scenario, gold_facts, rendered, evidence_text)
        )
    return problems


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


class _Generator:
    def __init__(
        self,
        scenario: Scenario,
        gold: GoldSet,
        rendered: dict[str, RenderedDocument],
        max_questions: int | None,
    ) -> None:
        self.scenario = scenario
        self.gold = gold
        self.rendered = rendered
        self.max_questions = max_questions
        self.accepted: list[GoldenQuestion] = []
        self.rejected: list[GoldenQuestion] = []
        self.gold_facts = {gold_fact.fact_id: gold_fact for gold_fact in gold.facts}
        self._counter = 0

    @property
    def language(self) -> Language:
        return self.scenario.language

    def _next_id(self) -> str:
        self._counter += 1
        return f"q_{self._counter:04d}"

    def emit(self, question: GoldenQuestion) -> None:
        problems = _verify_question(question, self.scenario, self.gold, self.rendered)
        if problems:
            question.validated = False
            question.validation_notes = problems
            self.rejected.append(question)
            return
        question.validated = True
        question.validation_notes = []
        self.accepted.append(question)
        if self.max_questions is not None and len(self.accepted) >= self.max_questions:
            raise _Stop

    def _question(
        self,
        question: str,
        category: QuestionCategory,
        difficulty: int,
        expected_answer: ExpectedAnswer,
        answerability: Answerability,
        *,
        required_facts: list[str] | None = None,
        evidence: list[EvidenceSpan] | None = None,
        completeness: CompletenessRequirement | None = None,
    ) -> GoldenQuestion:
        return GoldenQuestion(
            question_id=self._next_id(),
            question=question,
            language=self.language,
            category=category,
            difficulty=difficulty,
            expected_answer=expected_answer,
            required_facts=required_facts or [],
            evidence=list(evidence or []),
            answerability=answerability,
            completeness=completeness,
            validated=False,
            source="deterministic",
        )

    def _gold_fact(self, fact_id: str) -> Any | None:
        return self.gold_facts.get(fact_id)

    def _scenario_fact(self, fact_id: str) -> Any | None:
        try:
            return self.scenario.fact(fact_id)
        except KeyError:
            return None

    # -- category generators ----------------------------------------------

    def fact_questions(self) -> None:
        for gold_fact in self.gold.facts:
            if gold_fact.status.value != "active":
                continue
            if not gold_fact.evidence or gold_fact.precision is EvidencePrecision.CLAUSE:
                continue
            scenario_fact = self._scenario_fact(gold_fact.fact_id)
            if scenario_fact is None:
                continue
            label = _label(scenario_fact, self.language)
            first_span = gold_fact.evidence[0]
            document = self.rendered.get(first_span.document_id)
            scope = document.title if document is not None else self.scenario.contract_id
            question_text = _text(
                self.language,
                f"Vilket värde har {label} enligt {scope}?",
                f"What is the value of {label} according to {scope}?",
            )
            expected = ExpectedAnswer(
                value=gold_fact.value,
                unit=gold_fact.unit,
                currency=gold_fact.currency,
            )
            difficulty = 1 if gold_fact.precision is EvidencePrecision.EXACT else 2
            self.emit(
                self._question(
                    question_text,
                    QuestionCategory.FACT_EXTRACTION,
                    difficulty,
                    expected,
                    Answerability.ANSWERABLE,
                    required_facts=[gold_fact.fact_id],
                    evidence=list(gold_fact.evidence),
                )
            )

    def entity_questions(self) -> None:
        for entity in self.gold.entities:
            if not entity.evidence:
                continue
            question_text = _text(
                self.language,
                f"Vilken part har rollen {entity.role} i avtalet?",
                f"Which party has the role {entity.role} in the agreement?",
            )
            expected = ExpectedAnswer(value=entity.name, text=entity.name)
            self.emit(
                self._question(
                    question_text,
                    QuestionCategory.ENTITY_IDENTIFICATION,
                    1,
                    expected,
                    Answerability.ANSWERABLE,
                    evidence=list(entity.evidence),
                )
            )

    def relation_questions(self) -> None:
        for reference in self.gold.references:
            if not reference.evidence:
                continue
            source_title = _doc_title(
                self.scenario, reference.source_document_id, self.language
            )
            target_spec = next(
                (
                    document
                    for document in self.scenario.documents
                    if document.document_id == reference.target_document_id
                ),
                None,
            )
            target_title = (
                target_spec.title(self.language)
                if target_spec is not None
                else reference.target_document_id
            )
            missing = reference.target_document_id in self.scenario.missing_documents
            answerability = (
                Answerability.INSUFFICIENT_EVIDENCE if missing else Answerability.ANSWERABLE
            )
            conditions = [f"{DOCUMENT_PREFIX}{reference.target_document_id}"]
            if missing:
                conditions.append(f"{MISSING_PREFIX}{reference.target_document_id}")
            question_text = _text(
                self.language,
                f"Vilket dokument refererar {source_title} till?",
                f"Which document does {source_title} refer to?",
            )
            expected = ExpectedAnswer(text=target_title, conditions=conditions)
            self.emit(
                self._question(
                    question_text,
                    QuestionCategory.RELATION,
                    4,
                    expected,
                    answerability,
                    evidence=list(reference.evidence),
                )
            )

    def comparison_questions(self) -> None:
        groups: dict[tuple[str, str], list[Any]] = {}
        for gold_fact in self.gold.facts:
            if gold_fact.status.value != "active":
                continue
            if not gold_fact.evidence or gold_fact.precision is EvidencePrecision.CLAUSE:
                continue
            scenario_fact = self._scenario_fact(gold_fact.fact_id)
            if scenario_fact is None or _numeric_value(gold_fact.value) is None:
                continue
            key = (scenario_fact.kind.value, scenario_fact.unit or scenario_fact.currency or "")
            groups.setdefault(key, []).append((gold_fact, scenario_fact))
        for entries in groups.values():
            if len(entries) < 2:
                continue
            (first_gold, first_fact), (second_gold, second_fact) = entries[0], entries[1]
            if _numeric_value(first_gold.value) == _numeric_value(second_gold.value):
                continue
            larger_gold = (
                first_gold
                if float(first_gold.value) > float(second_gold.value)
                else second_gold
            )
            label_first = _label(first_fact, self.language)
            label_second = _label(second_fact, self.language)
            question_text = _text(
                self.language,
                f"Vilket av följande värden är högst: {label_first} eller {label_second}?",
                f"Which of the following values is highest: {label_first} or {label_second}?",
            )
            expected = ExpectedAnswer(
                value=larger_gold.value,
                unit=larger_gold.unit,
                currency=larger_gold.currency,
            )
            evidence = list(first_gold.evidence) + list(second_gold.evidence)
            self.emit(
                self._question(
                    question_text,
                    QuestionCategory.COMPARISON,
                    3,
                    expected,
                    Answerability.ANSWERABLE,
                    required_facts=[first_gold.fact_id, second_gold.fact_id],
                    evidence=evidence,
                )
            )

    def conditional_questions(self) -> None:
        for anomaly in self.scenario.anomalies:
            if anomaly.answerability is not Answerability.CONDITIONAL:
                continue
            document_id, clause_id = _clause_for_anomaly(self.scenario, anomaly)
            evidence = _clause_evidence(self.scenario, self.rendered, document_id, clause_id)
            if evidence is None:
                continue
            clause = _clause_spec(self.scenario, clause_id or "")
            heading = clause.heading(self.language) if clause is not None else ""
            label = _anomaly_label(self.scenario, anomaly, self.language)
            question_text = _text(
                self.language,
                f"Under vilka förutsättningar gäller {label}?",
                f"Under which conditions does {label} apply?",
            )
            required = [
                fact_id
                for fact_id in anomaly.fact_ids
                if self._gold_fact(fact_id) is not None and self._gold_fact(fact_id).evidence
            ]
            expected = ExpectedAnswer(
                text=heading,
                conditions=[
                    f"{CLAUSE_PREFIX}{document_id}:{clause_id}",
                    f"{ANOMALY_PREFIX}{anomaly.anomaly_id}",
                ],
            )
            self.emit(
                self._question(
                    question_text,
                    QuestionCategory.CONDITIONAL,
                    6,
                    expected,
                    Answerability.CONDITIONAL,
                    required_facts=required,
                    evidence=[evidence],
                )
            )
        for document in self.scenario.documents:
            for clause in document.clauses:
                if clause.mode is not ClauseMode.CONDITIONAL:
                    continue
                facts = [
                    self._gold_fact(fact_id)
                    for fact_id in clause.fact_ids
                    if self._gold_fact(fact_id) is not None
                    and self._gold_fact(fact_id).evidence
                ]
                if not facts:
                    continue
                label = _label(self._scenario_fact(facts[0].fact_id), self.language)
                question_text = _text(
                    self.language,
                    f"Vilka villkor gäller för {label}?",
                    f"Which conditions apply to {label}?",
                )
                evidence = [span for fact in facts for span in fact.evidence]
                expected = ExpectedAnswer(values=[fact.value for fact in facts])
                self.emit(
                    self._question(
                        question_text,
                        QuestionCategory.CONDITIONAL,
                        4,
                        expected,
                        Answerability.ANSWERABLE,
                        required_facts=[fact.fact_id for fact in facts],
                        evidence=evidence,
                    )
                )

    def contradiction_questions(self) -> None:
        for anomaly in self.scenario.anomalies:
            if not (
                anomaly.kind.value == "intentional_contradiction"
                or anomaly.answerability is Answerability.CONFLICTING_EVIDENCE
            ):
                continue
            fact_ids = [
                fact_id
                for fact_id in [*anomaly.fact_ids, anomaly.governing_fact_id]
                if fact_id and self._gold_fact(fact_id) is not None
            ]
            facts = [self._gold_fact(fact_id) for fact_id in dict.fromkeys(fact_ids)]
            facts = [
                fact
                for fact in facts
                if fact is not None
                and fact.evidence
                and fact.precision is not EvidencePrecision.CLAUSE
            ]
            if len(facts) < 2:
                continue
            distinct_values: list[Any] = []
            for fact in facts:
                if all(not _values_equal(fact.value, value) for value in distinct_values):
                    distinct_values.append(fact.value)
            if len(distinct_values) < 2:
                continue
            label = _anomaly_label(self.scenario, anomaly, self.language)
            question_text = _text(
                self.language,
                f"Vilket värde gäller för {label} när dokumenten anger olika uppgifter?",
                f"Which value applies to {label} when the documents give conflicting information?",
            )
            expected = ExpectedAnswer(
                values=distinct_values,
                conditions=[f"{ANOMALY_PREFIX}{anomaly.anomaly_id}"],
            )
            evidence = [span for fact in facts for span in fact.evidence]
            self.emit(
                self._question(
                    question_text,
                    QuestionCategory.CONTRADICTION,
                    5,
                    expected,
                    Answerability.CONFLICTING_EVIDENCE,
                    required_facts=[fact.fact_id for fact in facts],
                    evidence=evidence,
                )
            )

    def legal_reasoning_questions(self) -> None:
        seen: set[tuple[str, str]] = set()

        def add_for_clauses(
            source_document: str | None,
            source_clause: str | None,
            target_document: str | None,
            target_clause: str | None,
        ) -> None:
            if source_clause is None or target_clause is None:
                return
            key = (source_clause, target_clause)
            if key in seen:
                return
            seen.add(key)
            source_spec = _clause_spec(self.scenario, source_clause)
            target_spec = _clause_spec(self.scenario, target_clause)
            if source_spec is None or target_spec is None:
                return
            fact_ids: list[str] = []
            for clause in (source_spec, target_spec):
                for fact_id in clause.fact_ids:
                    gold_fact = self._gold_fact(fact_id)
                    if gold_fact is not None and gold_fact.evidence:
                        fact_ids.append(fact_id)
            fact_ids = list(dict.fromkeys(fact_ids))
            if len(fact_ids) < 2:
                return
            facts = [self._gold_fact(fact_id) for fact_id in fact_ids]
            source_heading = source_spec.heading(self.language)
            target_heading = target_spec.heading(self.language)
            question_text = _text(
                self.language,
                f"Vilka villkor och följder gäller enligt {source_heading} "
                f"och {target_heading}?",
                f"Which conditions and consequences apply under {source_heading} "
                f"and {target_heading}?",
            )
            expected = ExpectedAnswer(values=[fact.value for fact in facts])
            evidence = [span for fact in facts for span in fact.evidence]
            self.emit(
                self._question(
                    question_text,
                    QuestionCategory.LEGAL_REASONING,
                    4,
                    expected,
                    Answerability.ANSWERABLE,
                    required_facts=fact_ids,
                    evidence=evidence,
                )
            )

        for relation in self.scenario.relations:
            if relation.kind not in _DEPENDENCY_KINDS:
                continue
            source_document, source_clause, _ = _resolve(self.scenario, relation.source)
            target_document, target_clause, _ = _resolve(self.scenario, relation.target)
            add_for_clauses(source_document, source_clause, target_document, target_clause)
        for document in self.scenario.documents:
            for clause in document.clauses:
                for dependency in clause.depends_on:
                    add_for_clauses(
                        document.document_id,
                        clause.clause_id,
                        _document_for_clause(self.scenario, dependency),
                        dependency,
                    )

    def absence_questions(self) -> None:
        for document_id in self.scenario.missing_documents:
            question_text = _text(
                self.language,
                f"Anges något innehåll från {document_id} i avtalet?",
                f"Does the agreement state any content from {document_id}?",
            )
            expected = ExpectedAnswer(
                text=_text(
                    self.language,
                    f"Nej, {document_id} saknas bland dokumenten.",
                    f"No, {document_id} is missing from the documents.",
                ),
                conditions=[f"{MISSING_PREFIX}{document_id}"],
            )
            self.emit(
                self._question(
                    question_text,
                    QuestionCategory.ABSENCE,
                    5,
                    expected,
                    Answerability.INSUFFICIENT_EVIDENCE,
                )
            )

    def insufficient_questions(self) -> None:
        for anomaly in self.scenario.anomalies:
            if anomaly.answerability is not Answerability.INSUFFICIENT_EVIDENCE:
                continue
            fact_ids = [
                fact_id for fact_id in anomaly.fact_ids if self._gold_fact(fact_id) is not None
            ]
            evidence = [
                span
                for fact_id in fact_ids
                for span in self._gold_fact(fact_id).evidence
            ]
            label = _anomaly_label(self.scenario, anomaly, self.language)
            text = anomaly.description_sv or _text(
                self.language,
                "Informationen saknas i dokumenten.",
                "The information is missing from the documents.",
            )
            conditions = [f"{ANOMALY_PREFIX}{anomaly.anomaly_id}"]
            conditions.extend(
                f"{MISSING_PREFIX}{document_id}"
                for document_id in self.scenario.missing_documents
            )
            question_text = _text(
                self.language,
                f"Vilken information saknas för att besvara frågan om {label}?",
                f"What information is missing to answer the question about {label}?",
            )
            expected = ExpectedAnswer(text=text, conditions=conditions)
            self.emit(
                self._question(
                    question_text,
                    QuestionCategory.INSUFFICIENT_EVIDENCE,
                    6,
                    expected,
                    Answerability.INSUFFICIENT_EVIDENCE,
                    required_facts=fact_ids,
                    evidence=evidence,
                )
            )
        if self.scenario.difficulty >= 6:
            for document_id in self.scenario.missing_documents:
                question_text = _text(
                    self.language,
                    f"Kan avtalet besvara frågor om innehållet i {document_id}?",
                    f"Can the agreement answer questions about the content of {document_id}?",
                )
                expected = ExpectedAnswer(
                    text=_text(
                        self.language,
                        f"Nej, underlaget {document_id} saknas.",
                        f"No, the source {document_id} is missing.",
                    ),
                    conditions=[f"{MISSING_PREFIX}{document_id}"],
                )
                self.emit(
                    self._question(
                        question_text,
                        QuestionCategory.INSUFFICIENT_EVIDENCE,
                        6,
                        expected,
                        Answerability.INSUFFICIENT_EVIDENCE,
                    )
                )

    def completeness_questions(self) -> None:
        if self.gold.entities and all(entity.evidence for entity in self.gold.entities):
            evidence = [span for entity in self.gold.entities for span in entity.evidence]
            question_text = _text(
                self.language,
                "Räkna upp samtliga parter som ingår i avtalet.",
                "List all parties to the agreement.",
            )
            expected = ExpectedAnswer(values=[entity.name for entity in self.gold.entities])
            completeness = CompletenessRequirement(
                kind="all_parties",
                expected_ids=[party.party_id for party in self.scenario.parties],
                description_sv="Alla avtalsparter måste räknas upp.",
            )
            self.emit(
                self._question(
                    question_text,
                    QuestionCategory.COMPLETENESS,
                    1,
                    expected,
                    Answerability.ANSWERABLE,
                    evidence=evidence,
                    completeness=completeness,
                )
            )
        document_ids = [document.document_id for document in self.scenario.documents]
        if len(document_ids) >= 2 and all(
            document_id in self.rendered for document_id in document_ids
        ):
            evidence: list[EvidenceSpan] = []
            for document_id in document_ids:
                spec = next(
                    document
                    for document in self.scenario.documents
                    if document.document_id == document_id
                )
                for clause in spec.clauses:
                    span = _clause_evidence(
                        self.scenario, self.rendered, document_id, clause.clause_id
                    )
                    if span is not None:
                        evidence.append(span)
                        break
            if evidence:
                question_text = _text(
                    self.language,
                    "Räkna upp samtliga dokument som ingår i avtalspaketet.",
                    "List all documents in the contract package.",
                )
                expected = ExpectedAnswer(
                    values=[
                        _doc_title(self.scenario, document_id, self.language)
                        for document_id in document_ids
                    ]
                )
                completeness = CompletenessRequirement(
                    kind="all_documents",
                    expected_ids=document_ids,
                    description_sv="Alla dokument i paketet måste räknas upp.",
                )
                self.emit(
                    self._question(
                        question_text,
                        QuestionCategory.COMPLETENESS,
                        2,
                        expected,
                        Answerability.ANSWERABLE,
                        evidence=evidence,
                        completeness=completeness,
                    )
                )
        for gold_fact in self.gold.facts:
            if not isinstance(gold_fact.value, list) or not gold_fact.value:
                continue
            if not gold_fact.evidence:
                continue
            scenario_fact = self._scenario_fact(gold_fact.fact_id)
            if scenario_fact is None:
                continue
            label = _label(scenario_fact, self.language)
            question_text = _text(
                self.language,
                f"Räkna upp samtliga {label}.",
                f"List all {label}.",
            )
            expected = ExpectedAnswer(values=list(gold_fact.value))
            completeness = CompletenessRequirement(
                kind="all_values",
                expected_ids=[gold_fact.fact_id],
                description_sv=f"Alla värden för {label} måste räknas upp.",
            )
            self.emit(
                self._question(
                    question_text,
                    QuestionCategory.COMPLETENESS,
                    2,
                    expected,
                    Answerability.ANSWERABLE,
                    required_facts=[gold_fact.fact_id],
                    evidence=list(gold_fact.evidence),
                    completeness=completeness,
                )
            )

    # -- driver ------------------------------------------------------------

    def run(self) -> QuestionOutcome:
        try:
            self.fact_questions()
            self.entity_questions()
            self.relation_questions()
            self.comparison_questions()
            self.conditional_questions()
            self.contradiction_questions()
            self.legal_reasoning_questions()
            self.absence_questions()
            self.insufficient_questions()
            self.completeness_questions()
        except _Stop:
            pass
        return QuestionOutcome(accepted=self.accepted, rejected=self.rejected)


def generate_and_verify_questions(
    scenario: Scenario,
    gold: GoldSet,
    rendered: dict[str, RenderedDocument],
    *,
    max_questions: int | None = None,
) -> QuestionOutcome:
    """Generate golden questions from scenario + gold and verify them on text.

    Accepted questions always have ``validated=True``; rejected questions keep
    their ``validation_notes`` explaining exactly which claim was unsupported.
    """

    return _Generator(scenario, gold, rendered, max_questions).run()
