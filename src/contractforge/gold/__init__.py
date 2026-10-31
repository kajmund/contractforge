"""Gold set construction: the verified facit for one contract.

``build_gold_set`` converts the scenario truth into atomic :class:`GoldFact`
objects with **real** evidence spans in the canonical text. Nothing is invented:

* explicit surfaces are located verbatim in the clause region;
* verified paraphrase surfaces get ``precision="paraphrase"``;
* when only the topic can be anchored (keyword present, or the fact is assigned
  to a clause but worded freely) the whole clause span is used with
  ``precision="clause"`` – the architecture explicitly allows that fallback;
* if a fact cannot be tied to any span, it is still listed (facit must mirror
  the scenario) but without evidence, so validation rejects the package instead
  of exporting an unsupported claim.

``question_ids`` is intentionally left empty; the pipeline fills it after the
golden questions have been generated.
"""

from __future__ import annotations

from ..models import (
    EvidencePrecision,
    EvidenceSpan,
    Fact,
    GeneratedClause,
    GeneratedDocument,
    GeneratedPackage,
    GoldAnomaly,
    GoldEntity,
    GoldFact,
    GoldReference,
    GoldSet,
    RelationKind,
    RenderedDocument,
    Scenario,
    TextSpan,
)
from ..utils import find_span, normalize_for_search

__all__ = ["build_gold_set"]

_PRECISION_RANK: dict[EvidencePrecision, int] = {
    EvidencePrecision.EXACT: 3,
    EvidencePrecision.PARAPHRASE: 2,
    EvidencePrecision.CLAUSE: 1,
}


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def _generated_document(
    package: GeneratedPackage, document_id: str
) -> GeneratedDocument | None:
    for document in package.documents:
        if document.document_id == document_id:
            return document
    return None


def _generated_clause(
    document: GeneratedDocument | None, clause_id: str
) -> GeneratedClause | None:
    if document is None:
        return None
    for clause in document.clauses:
        if clause.clause_id == clause_id:
            return clause
    return None


def _clause_span(rendered_document: RenderedDocument, clause_id: str) -> TextSpan | None:
    return rendered_document.clause_spans.get(clause_id)


def _clause_at(rendered_document: RenderedDocument, start: int, end: int) -> str | None:
    for clause_id, span in rendered_document.clause_spans.items():
        if span.start <= start and end <= span.end:
            return clause_id
    return None


def _evidence(
    rendered_document: RenderedDocument,
    start: int,
    end: int,
    clause_id: str | None,
) -> EvidenceSpan:
    return EvidenceSpan(
        document_id=rendered_document.document_id,
        clause_id=clause_id,
        start=start,
        end=end,
        quote=rendered_document.canonical_text[start:end],
    )


def _evidence_key(span: EvidenceSpan) -> tuple[str, int, int]:
    return (span.document_id, span.start, span.end)


def _dedupe_evidence(spans: list[EvidenceSpan]) -> list[EvidenceSpan]:
    seen: set[tuple[str, int, int]] = set()
    result: list[EvidenceSpan] = []
    for span in spans:
        key = _evidence_key(span)
        if key in seen:
            continue
        seen.add(key)
        result.append(span)
    return result


def _intentionally_missing_facts(scenario: Scenario) -> set[str]:
    """Facts the scenario deliberately leaves unregulated (level 6).

    Only ``MISSING_INFORMATION`` facts get no evidence: inventing a span for
    information the scenario says is missing would make facit claim something
    the text does not support. Facts of ``AMBIGUITY``/``CONDITIONAL`` anomalies
    are still stated in the text (e.g. the conditional value "10 timmar") and
    keep their normal value evidence.
    """

    missing: set[str] = set()
    for anomaly in scenario.anomalies:
        if anomaly.kind.value == "missing_information":
            missing.update(anomaly.fact_ids)
    return missing


def _locate_in_clause(
    rendered_document: RenderedDocument, clause_id: str, needle: str
) -> EvidenceSpan | None:
    span = _clause_span(rendered_document, clause_id)
    if span is None or not needle:
        return None
    found = find_span(rendered_document.canonical_text, needle, span.start, span.end)
    if found is None:
        return None
    return _evidence(rendered_document, found[0], found[1], clause_id)


def _clause_evidence(
    rendered_document: RenderedDocument, clause_id: str
) -> EvidenceSpan | None:
    span = _clause_span(rendered_document, clause_id)
    if span is None:
        return None
    return _evidence(rendered_document, span.start, span.end, clause_id)


def _clauses_for_fact(scenario: Scenario, fact_id: str) -> list[tuple[str, str]]:
    """Return ``(document_id, clause_id)`` pairs that plan to express ``fact_id``."""

    pairs: list[tuple[str, str]] = []
    for document in scenario.documents:
        for clause in document.clauses:
            if fact_id in clause.fact_ids:
                pairs.append((document.document_id, clause.clause_id))
    return pairs


def _documents_in_order(
    scenario: Scenario, rendered: dict[str, RenderedDocument]
) -> list[RenderedDocument]:
    documents: list[RenderedDocument] = []
    for spec in scenario.documents:
        rendered_document = rendered.get(spec.document_id)
        if rendered_document is not None:
            documents.append(rendered_document)
    for document_id, rendered_document in rendered.items():
        if all(document.document_id != document_id for document in documents):
            documents.append(rendered_document)
    return documents


def _global_evidence(
    needle: str, documents: list[RenderedDocument]
) -> EvidenceSpan | None:
    for rendered_document in documents:
        found = find_span(rendered_document.canonical_text, needle)
        if found is None:
            continue
        clause_id = _clause_at(rendered_document, found[0], found[1])
        return _evidence(rendered_document, found[0], found[1], clause_id)
    return None


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


def _fact_evidence(
    scenario: Scenario,
    rendered: dict[str, RenderedDocument],
    fact: Fact,
) -> tuple[list[EvidenceSpan], EvidencePrecision]:
    exact: list[EvidenceSpan] = []
    paraphrased: list[EvidenceSpan] = []
    clause_spans: list[EvidenceSpan] = []
    documents = _documents_in_order(scenario, rendered)
    planned = _clauses_for_fact(scenario, fact.fact_id)

    for document_id, clause_id in planned:
        rendered_document = rendered.get(document_id)
        if rendered_document is None:
            continue
        for surface in fact.explicit_surfaces:
            span = _locate_in_clause(rendered_document, clause_id, surface)
            if span is not None:
                exact.append(span)
                break
        if not exact:
            for surface in fact.paraphrase_surfaces:
                span = _locate_in_clause(rendered_document, clause_id, surface)
                if span is not None:
                    paraphrased.append(span)
                    break
        clause_span = _clause_evidence(rendered_document, clause_id)
        if clause_span is not None:
            clause_spans.append(clause_span)

    if exact:
        return _dedupe_evidence(exact), EvidencePrecision.EXACT
    if paraphrased:
        return _dedupe_evidence(paraphrased), EvidencePrecision.PARAPHRASE

    # The fact is planned in a clause but worded freely: keyword anchor if
    # present, otherwise the whole clause. Both stay honest ("precision=clause").
    keyword_spans: list[EvidenceSpan] = []
    for document_id, clause_id in planned:
        rendered_document = rendered.get(document_id)
        if rendered_document is None:
            continue
        for keyword in [*fact.keywords_sv, *fact.keywords_en]:
            span = _locate_in_clause(rendered_document, clause_id, keyword)
            if span is not None:
                keyword_spans.append(span)
                break
    if keyword_spans:
        return _dedupe_evidence(keyword_spans), EvidencePrecision.CLAUSE
    if clause_spans:
        return _dedupe_evidence(clause_spans), EvidencePrecision.CLAUSE

    # Not planned anywhere: look for an explicit/paraphrase surface globally,
    # otherwise no evidence at all (validation will reject the package).
    for surface in [*fact.explicit_surfaces, *fact.paraphrase_surfaces]:
        span = _global_evidence(surface, documents)
        if span is not None:
            precision = (
                EvidencePrecision.EXACT
                if surface in fact.explicit_surfaces
                else EvidencePrecision.PARAPHRASE
            )
            return [span], precision
    return [], EvidencePrecision.CLAUSE


def _gold_facts(
    scenario: Scenario, rendered: dict[str, RenderedDocument]
) -> tuple[list[GoldFact], dict[str, list[EvidenceSpan]]]:
    facts: list[GoldFact] = []
    evidence_by_fact: dict[str, list[EvidenceSpan]] = {}
    intentionally_missing = _intentionally_missing_facts(scenario)
    for fact in scenario.facts:
        if fact.fact_id in intentionally_missing:
            evidence, precision = [], EvidencePrecision.CLAUSE
            evidence_by_fact[fact.fact_id] = evidence
            facts.append(
                GoldFact(
                    fact_id=fact.fact_id,
                    kind=fact.kind,
                    value=fact.value,
                    unit=fact.unit,
                    currency=fact.currency,
                    entity_id=fact.entity_id,
                    status=fact.status,
                    precision=precision,
                    evidence=[],
                    question_ids=[],
                    notes_sv=(
                        "avsiktligt oreglerat enligt scenariots "
                        "missing_information-anomali; ingen evidens krävs"
                    ),
                )
            )
            continue
        evidence, precision = _fact_evidence(scenario, rendered, fact)
        evidence_by_fact[fact.fact_id] = evidence
        facts.append(
            GoldFact(
                fact_id=fact.fact_id,
                kind=fact.kind,
                value=fact.value,
                unit=fact.unit,
                currency=fact.currency,
                entity_id=fact.entity_id,
                status=fact.status,
                precision=precision,
                evidence=evidence,
                question_ids=[],
                notes_sv=(
                    ""
                    if evidence
                    else "inget evidensspann kunde hittas i kanonisk text"
                ),
            )
        )
    return facts, evidence_by_fact


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def _entity_evidence(
    scenario: Scenario, rendered: dict[str, RenderedDocument], needles: list[str]
) -> list[EvidenceSpan]:
    documents = _documents_in_order(scenario, rendered)
    evidence: list[EvidenceSpan] = []
    for needle in needles:
        if not needle or not needle.strip():
            continue
        span = _global_evidence(needle, documents)
        if span is not None:
            evidence.append(span)
    return _dedupe_evidence(evidence)


def _gold_entities(
    scenario: Scenario, rendered: dict[str, RenderedDocument]
) -> list[GoldEntity]:
    entities: list[GoldEntity] = []
    for party in scenario.parties:
        needles = [party.name]
        if party.org_number:
            needles.append(party.org_number)
        if party.address:
            needles.append(party.address)
        entities.append(
            GoldEntity(
                entity_id=party.party_id,
                name=party.name,
                role=party.role,
                evidence=_entity_evidence(scenario, rendered, needles),
            )
        )
    return entities


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def _resolve(
    scenario: Scenario, token: str
) -> tuple[str | None, str | None, str | None]:
    """Resolve a scenario reference token to ``(document_id, clause_id, fact_id)``."""

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


def _document_identity_needles(
    scenario: Scenario, target_document_id: str
) -> list[str]:
    """Verbatim strings that name the target *document* in body text.

    ``document_id`` is included because the canonical metadata header lists only
    the *source* document id, so a target id in the slice must come from the
    body. Titles are preferred (searched first).
    """

    for document in scenario.documents:
        if document.document_id == target_document_id:
            return [
                needle
                for needle in dict.fromkeys(
                    [document.title_sv, document.title_en, document.document_id]
                )
                if needle and needle.strip()
            ]
    return [target_document_id] if target_document_id.strip() else []


def _clause_identity_needles(
    scenario: Scenario, target_clause_id: str | None
) -> list[str]:
    """Verbatim strings that name the target *clause* (its headings)."""

    if target_clause_id is None:
        return []
    for document in scenario.documents:
        for clause in document.clauses:
            if clause.clause_id == target_clause_id:
                return [
                    needle
                    for needle in dict.fromkeys([clause.heading_sv, clause.heading_en])
                    if needle and needle.strip()
                ]
    return []


def _first_needle_span(
    text: str, needles: list[str], start: int, end: int
) -> tuple[str, tuple[int, int]] | None:
    for needle in needles:
        found = find_span(text, needle, start, end)
        if found is not None:
            return needle, found
    return None


def _all_needle_spans(
    text: str, needles: list[str], start: int, end: int
) -> list[tuple[str, tuple[int, int]]]:
    """All occurrences of ``needles`` in ``text[start:end]``, in scan order."""

    spans: list[tuple[str, tuple[int, int]]] = []
    for needle in needles:
        position = start
        while position < end:
            found = find_span(text, needle, position, end)
            if found is None:
                break
            spans.append((needle, found))
            position = found[0] + 1
    return spans


def _slice_names_any(text: str, start: int, end: int, needles: list[str]) -> bool:
    slice_text = normalize_for_search(text[start:end])[0]
    return any(
        normalize_for_search(needle)[0]
        and normalize_for_search(needle)[0] in slice_text
        for needle in needles
    )


def _reference_evidence(
    scenario: Scenario,
    rendered: dict[str, RenderedDocument],
    source_document_id: str,
    source_clause_id: str | None,
    target_document_id: str,
    target_clause_id: str | None = None,
) -> list[EvidenceSpan]:
    """Return a span proving that the source text really names the target.

    Review finding F2 requires the evidence slice to name the target:

    * same-document reference: a span containing the target clause heading (or
      the document title) is enough;
    * cross-document reference: the span must name the target *document*
      (title_sv/title_en/document_id). If the source clause also names the
      target clause heading, a broader span covering both is returned (stronger
      evidence). A clause-heading-only hit is never accepted across documents:
      such a heading can be the source document's own heading and does not say
      which document is meant.
    * no textual support -> ``[]``; callers omit in-package references and keep
      empty evidence only for declared ``missing_documents`` (F6).
    """

    rendered_document = rendered.get(source_document_id)
    if rendered_document is None:
        return []
    canonical = rendered_document.canonical_text
    document_needles = _document_identity_needles(scenario, target_document_id)
    clause_needles = _clause_identity_needles(scenario, target_clause_id)
    cross_document = source_document_id != target_document_id
    clause_region: tuple[str, int, int] | None = None
    if source_clause_id is not None:
        span = _clause_span(rendered_document, source_clause_id)
        if span is not None:
            clause_region = (source_clause_id, span.start, span.end)

    if cross_document:
        if clause_region is not None:
            clause_id, start, end = clause_region
            document_hits = _all_needle_spans(canonical, document_needles, start, end)
            if document_hits:
                clause_hits = (
                    _all_needle_spans(canonical, clause_needles, start, end)
                    if clause_needles
                    else []
                )
                if clause_hits:
                    # Prefer the closest (narrowest) document-heading pair so
                    # the slice is minimal (e.g. ”Anställningsavtal” avsnitt
                    # ”Lön och arbetstid”) instead of spanning the whole clause.
                    pairs = [
                        (
                            min(document_span[1][0], clause_span[1][0]),
                            max(document_span[1][1], clause_span[1][1]),
                        )
                        for document_span in document_hits
                        for clause_span in clause_hits
                    ]
                    span_start, span_end = min(
                        pairs, key=lambda pair: pair[1] - pair[0]
                    )
                else:
                    span_start, span_end = document_hits[0][1]
                return [_evidence(rendered_document, span_start, span_end, clause_id)]
        document_hit = _first_needle_span(canonical, document_needles, 0, len(canonical))
        if document_hit is not None and _slice_names_any(
            canonical, document_hit[1][0], document_hit[1][1], document_needles
        ):
            owner = _clause_at(rendered_document, document_hit[1][0], document_hit[1][1])
            return [
                _evidence(rendered_document, document_hit[1][0], document_hit[1][1], owner)
            ]
        return []

    # Same document: the target clause heading or the document title both name
    # the target; search the source clause first, then the whole document.
    needles = [*clause_needles, *document_needles]
    regions: list[tuple[str | None, int, int]] = []
    if clause_region is not None:
        regions.append((clause_region[0], clause_region[1], clause_region[2]))
    regions.append((None, 0, len(canonical)))
    for owned_clause_id, start, end in regions:
        hit = _first_needle_span(canonical, needles, start, end)
        if hit is None:
            continue
        found_start, found_end = hit[1]
        owner = owned_clause_id or _clause_at(rendered_document, found_start, found_end)
        return [_evidence(rendered_document, found_start, found_end, owner)]
    return []


def _gold_references(
    scenario: Scenario, rendered: dict[str, RenderedDocument]
) -> list[GoldReference]:
    references: list[GoldReference] = []
    seen: set[tuple[str, str | None, str, str | None, RelationKind]] = set()

    def add(
        source_document_id: str | None,
        source_clause_id: str | None,
        target_document_id: str | None,
        target_clause_id: str | None,
        kind: RelationKind,
    ) -> None:
        if source_document_id is None or target_document_id is None:
            return
        key = (
            source_document_id,
            source_clause_id,
            target_document_id,
            target_clause_id,
            kind,
        )
        if key in seen:
            return
        evidence = _reference_evidence(
            scenario,
            rendered,
            source_document_id,
            source_clause_id,
            target_document_id,
            target_clause_id,
        )
        if not evidence and target_document_id not in set(scenario.missing_documents):
            # F2c/F6: an in-package reference that the source text never names
            # cannot be supported by a real span. Facit must not claim it (and
            # validation would reject an empty span), so it is omitted here.
            # References to declared *missing* documents are kept with no
            # evidence because they are unsupported by definition; validation
            # explicitly allows evidence=[] for those. The unanchored
            # relation templates themselves are a scenario-generator concern (F6).
            return
        seen.add(key)
        references.append(
            GoldReference(
                source_document_id=source_document_id,
                source_clause_id=source_clause_id,
                target_document_id=target_document_id,
                target_clause_id=target_clause_id,
                kind=kind,
                evidence=evidence,
            )
        )

    for relation in scenario.relations:
        source_document_id, source_clause_id, _ = _resolve(scenario, relation.source)
        target_document_id, target_clause_id, _ = _resolve(scenario, relation.target)
        add(
            source_document_id,
            source_clause_id,
            target_document_id,
            target_clause_id,
            relation.kind,
        )

    for document in scenario.documents:
        for clause in document.clauses:
            for reference in clause.reference_to:
                target_document_id, target_clause_id, _ = _resolve(scenario, reference)
                add(
                    document.document_id,
                    clause.clause_id,
                    target_document_id,
                    target_clause_id,
                    RelationKind.REFERENCES,
                )
        for reference in document.references_documents:
            target_document_id, target_clause_id, _ = _resolve(scenario, reference)
            add(
                document.document_id,
                None,
                target_document_id,
                target_clause_id,
                RelationKind.REFERENCES,
            )
    return references


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------


def _gold_anomalies(
    scenario: Scenario, evidence_by_fact: dict[str, list[EvidenceSpan]]
) -> list[GoldAnomaly]:
    anomalies: list[GoldAnomaly] = []
    for anomaly in scenario.anomalies:
        evidence: list[EvidenceSpan] = []
        for fact_id in [*anomaly.fact_ids]:
            evidence.extend(evidence_by_fact.get(fact_id, []))
        if anomaly.governing_fact_id is not None:
            evidence.extend(evidence_by_fact.get(anomaly.governing_fact_id, []))
        anomalies.append(
            GoldAnomaly(
                anomaly_id=anomaly.anomaly_id,
                kind=anomaly.kind,
                answerability=anomaly.answerability,
                fact_ids=list(anomaly.fact_ids),
                governing_fact_id=anomaly.governing_fact_id,
                evidence=_dedupe_evidence(evidence),
                description_sv=anomaly.description_sv,
            )
        )
    return anomalies


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_gold_set(
    scenario: Scenario,
    package: GeneratedPackage,
    rendered: dict[str, RenderedDocument],
) -> GoldSet:
    """Build the verified facit for ``scenario`` from the generated package.

    Every evidence span is a real span in ``rendered[document_id].canonical_text``
    and the ``quote`` is the exact slice. No span is fabricated.
    """

    facts, evidence_by_fact = _gold_facts(scenario, rendered)
    return GoldSet(
        contract_id=scenario.contract_id,
        facts=facts,
        entities=_gold_entities(scenario, rendered),
        references=_gold_references(scenario, rendered),
        anomalies=_gold_anomalies(scenario, evidence_by_fact),
        missing_documents=list(scenario.missing_documents),
    )
