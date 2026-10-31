"""Deterministic package verification: the gate between generated text and facit.

``validate_package`` never calls a model. Every check is a pure function of the
scenario (truth), the generated package and the rendered canonical documents.
The verdict therefore does not depend on which model generated the text.

Checks produce stable ``check_id`` values and concrete messages that are used
directly as repair feedback in the pipeline, for example::

    fact_notice_period: explicit surface 'tre (3) månader' saknas i clause_03
    fact_liability_cap: operativt värde '500 000 SEK' saknas i clause_05;
        funna avvikande värden '2 000 000 SEK' kan ersätta det

The key invariant: gold and questions must never claim anything the canonical
text does not support. Therefore ``validate_package`` also verifies gold when it
is supplied: every gold evidence span must be inside the canonical text, its
quote must match the slice exactly, and gold values must equal scenario values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import (
    Answerability,
    CheckResult,
    CheckStatus,
    ClauseMode,
    DocumentSpec,
    DocumentStatus,
    DocumentVerification,
    EvidencePrecision,
    EvidenceSpan,
    Fact,
    GeneratedClause,
    GeneratedDocument,
    GeneratedPackage,
    GoldAnomaly,
    GoldEntity,
    GoldFact,
    GoldSet,
    PackageVerification,
    RenderedDocument,
    Scenario,
    TextSpan,
)
from ..utils import contains_span_text, find_span, normalize_for_search
from . import semantic

__all__ = ["validate_package"]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _check(
    check_id: str,
    status: CheckStatus,
    message: str = "",
    *,
    document_id: str | None = None,
    clause_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        status=status,
        message=message,
        document_id=document_id,
        clause_id=clause_id,
        details=details or {},
    )


def _norm(text: str) -> str:
    return normalize_for_search(text)[0]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _generated_document(package: GeneratedPackage, document_id: str) -> GeneratedDocument | None:
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


def _document_verification(
    document_id: str, checks: list[CheckResult], attempts: int = 1
) -> DocumentVerification:
    failed = [check for check in checks if check.status == CheckStatus.FAIL]
    return DocumentVerification(
        document_id=document_id,
        status=DocumentStatus.REJECTED if failed else DocumentStatus.ACCEPTED,
        checks=checks,
        attempts=attempts,
        reasons=[check.message for check in failed],
    )


def _canonical_span(
    rendered_document: RenderedDocument | None, clause_id: str, needle: str
) -> tuple[int, int] | None:
    if rendered_document is None or not needle:
        return None
    span = rendered_document.clause_spans.get(clause_id)
    if span is None:
        return None
    return find_span(rendered_document.canonical_text, needle, span.start, span.end)


def _span_problems(
    spans: list[EvidenceSpan], rendered: dict[str, RenderedDocument], label: str
) -> list[str]:
    """Validate evidence spans against canonical text. Empty list means valid."""

    problems: list[str] = []
    seen: set[tuple[str, int, int]] = set()
    for index, span in enumerate(spans):
        key = (span.document_id, span.start, span.end)
        if key in seen:
            problems.append(
                f"{label}: dubblerat evidensspann {span.document_id}[{span.start}:{span.end}]"
            )
            continue
        seen.add(key)
        document = rendered.get(span.document_id)
        if document is None:
            problems.append(
                f"{label}: evidensspann {index} pekar på okänt dokument {span.document_id!r}"
            )
            continue
        text = document.canonical_text
        if not 0 <= span.start < span.end <= len(text):
            problems.append(
                f"{label}: evidensspann {index} [{span.start}:{span.end}] ligger utanför "
                f"{span.document_id} (längd {len(text)})"
            )
            continue
        slice_text = text[span.start : span.end]
        if not span.quote.strip():
            problems.append(f"{label}: evidensspann {index} saknar quote")
        elif not contains_span_text(slice_text, 0, len(slice_text), span.quote):
            problems.append(
                f"{label}: quote {span.quote!r} matchar inte slice "
                f"{slice_text!r} i {span.document_id}[{span.start}:{span.end}]"
            )
        if span.clause_id is not None:
            clause_span = document.clause_spans.get(span.clause_id)
            if clause_span is None:
                problems.append(
                    f"{label}: evidensspann {index} anger okänd klausul {span.clause_id!r}"
                )
            elif span.start < clause_span.start or span.end > clause_span.end:
                problems.append(
                    f"{label}: evidensspann {index} ligger utanför klausul "
                    f"{span.clause_id} i {span.document_id}"
                )
    return problems


def _clause_for_fact(scenario: Scenario, fact_id: str) -> str | None:
    for document in scenario.documents:
        for clause in document.clauses:
            if fact_id in clause.fact_ids:
                return clause.clause_id
    return None


def _document_for_clause(scenario: Scenario, clause_id: str | None) -> str | None:
    if clause_id is None:
        return None
    for document in scenario.documents:
        for clause in document.clauses:
            if clause.clause_id == clause_id:
                return document.document_id
    return None


def _intentionally_missing_facts(scenario: Scenario) -> set[str]:
    """Facts that the scenario deliberately leaves unregulated (level 5/6).

    Facts referenced by a ``MISSING_INFORMATION``/``AMBIGUITY`` anomaly must not
    be required in the text: the scenario says the contract is silent about
    them, so demanding an explicit surface would force a false repair.
    """

    missing: set[str] = set()
    for anomaly in scenario.anomalies:
        if anomaly.kind.value in ("missing_information", "ambiguity"):
            missing.update(anomaly.fact_ids)
    return missing


@dataclass(frozen=True)
class _ResolvedToken:
    kind: str  # "document" | "clause" | "fact" | "missing" | "unknown"
    document_id: str | None = None
    clause_id: str | None = None
    fact_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.kind != "unknown"


def _resolve_token(scenario: Scenario, token: str) -> _ResolvedToken:
    if token.startswith("fact:"):
        fact_id = token[len("fact:") :]
        if any(fact.fact_id == fact_id for fact in scenario.facts):
            clause_id = _clause_for_fact(scenario, fact_id)
            return _ResolvedToken(
                "fact",
                document_id=_document_for_clause(scenario, clause_id),
                clause_id=clause_id,
                fact_id=fact_id,
            )
        return _ResolvedToken("unknown", fact_id=fact_id)
    if token in scenario.missing_documents:
        return _ResolvedToken("missing", document_id=token)
    if ":" in token:
        document_id, clause_id = token.split(":", 1)
        document = next(
            (item for item in scenario.documents if item.document_id == document_id), None
        )
        if document is None:
            return _ResolvedToken("unknown", document_id=document_id, clause_id=clause_id)
        if any(clause.clause_id == clause_id for clause in document.clauses):
            return _ResolvedToken("clause", document_id=document_id, clause_id=clause_id)
        return _ResolvedToken("unknown", document_id=document_id, clause_id=clause_id)
    if any(document.document_id == token for document in scenario.documents):
        return _ResolvedToken("document", document_id=token)
    for document in scenario.documents:
        for clause in document.clauses:
            if clause.clause_id == token:
                return _ResolvedToken(
                    "clause", document_id=document.document_id, clause_id=token
                )
    for fact in scenario.facts:
        if fact.fact_id == token:
            clause_id = _clause_for_fact(scenario, fact.fact_id)
            return _ResolvedToken(
                "fact",
                document_id=_document_for_clause(scenario, clause_id),
                clause_id=clause_id,
                fact_id=fact.fact_id,
            )
    return _ResolvedToken("unknown")


# ---------------------------------------------------------------------------
# Fact sites: where a scenario fact is expected to appear
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FactSite:
    document_id: str
    clause_id: str
    mode: ClauseMode
    fact: Fact
    text: str
    analysis: semantic.FactAnalysis
    clause_span: TextSpan | None
    rendered_document: RenderedDocument | None

    @property
    def supported(self) -> bool:
        return self.analysis.supported


def _collect_fact_sites(
    scenario: Scenario,
    package: GeneratedPackage,
    rendered: dict[str, RenderedDocument],
) -> list[_FactSite]:
    sites: list[_FactSite] = []
    for spec_document in scenario.documents:
        generated = _generated_document(package, spec_document.document_id)
        rendered_document = rendered.get(spec_document.document_id)
        for spec_clause in spec_document.clauses:
            generated_clause = _generated_clause(generated, spec_clause.clause_id)
            clause_span = (
                rendered_document.clause_spans.get(spec_clause.clause_id)
                if rendered_document is not None
                else None
            )
            if generated_clause is not None:
                text = generated_clause.text
            elif rendered_document is not None and clause_span is not None:
                text = clause_span.slice(rendered_document.canonical_text)
            else:
                text = ""
            for fact_id in _dedupe(list(spec_clause.fact_ids)):
                try:
                    fact = scenario.fact(fact_id)
                except KeyError:  # pragma: no cover - scenario validation forbids it
                    continue
                sites.append(
                    _FactSite(
                        document_id=spec_document.document_id,
                        clause_id=spec_clause.clause_id,
                        mode=spec_clause.mode,
                        fact=fact,
                        text=text,
                        analysis=semantic.analyze_fact(fact, text, scenario.language),
                        clause_span=clause_span,
                        rendered_document=rendered_document,
                    )
                )
    return sites


def _support_index(sites: list[_FactSite]) -> dict[str, bool]:
    support: dict[str, bool] = {}
    for site in sites:
        support[site.fact.fact_id] = support.get(site.fact.fact_id, False) or site.supported
    return support


def _sites_for(
    sites: list[_FactSite], document_id: str, clause_id: str, fact_id: str
) -> _FactSite | None:
    for site in sites:
        if (
            site.document_id == document_id
            and site.clause_id == clause_id
            and site.fact.fact_id == fact_id
        ):
            return site
    return None


# ---------------------------------------------------------------------------
# Fact checks
# ---------------------------------------------------------------------------


def _fact_checks(
    site: _FactSite, intentionally_missing: set[str]
) -> list[CheckResult]:
    fact = site.fact
    document_id = site.document_id
    clause_id = site.clause_id
    base_id = f"document.{document_id}.clause.{clause_id}.fact.{fact.fact_id}"
    analysis = site.analysis
    expected = semantic.display_expected(fact)
    checks: list[CheckResult] = []

    if fact.fact_id in intentionally_missing:
        return [
            _check(
                base_id,
                CheckStatus.SKIP,
                (
                    f"{fact.fact_id}: avsiktligt oreglerat enligt scenariots "
                    "missing_information/ambiguity-anomali; ingen närvarocheck krävs."
                ),
                document_id=document_id,
                clause_id=clause_id,
                details={"intentionally_missing": True},
            ),
            _check(
                f"{base_id}.semantic",
                CheckStatus.SKIP,
                (
                    f"{fact.fact_id}: ingen semantisk värdekontroll för avsiktligt "
                    "oreglerat faktum."
                ),
                document_id=document_id,
                clause_id=clause_id,
                details={"intentionally_missing": True},
            ),
        ]

    if site.mode is ClauseMode.EXPLICIT:
        if fact.explicit_surfaces:
            surfaces = ", ".join(repr(surface) for surface in fact.explicit_surfaces)
            if analysis.explicit_span is None:
                checks.append(
                    _check(
                        base_id,
                        CheckStatus.FAIL,
                        (
                            f"{fact.fact_id}: explicit surface {surfaces} saknas i {clause_id} "
                            f"(dokument {document_id}); förväntat värde {expected}. "
                            "Klausulen måste uttrycka någon av de explicita formerna ordagrant."
                        ),
                        document_id=document_id,
                        clause_id=clause_id,
                        details={"expected": expected, "surfaces": list(fact.explicit_surfaces)},
                    )
                )
            else:
                checks.append(
                    _check(
                        base_id,
                        CheckStatus.PASS,
                        f"{fact.fact_id}: explicit surface {analysis.explicit_surface!r} finns i {clause_id}.",
                        document_id=document_id,
                        clause_id=clause_id,
                    )
                )
        elif analysis.supported:
            checks.append(
                _check(
                    base_id,
                    CheckStatus.PASS,
                    (
                        f"{fact.fact_id}: inga explicit_surfaces deklarerade; värdet är "
                        f"verifierat semantiskt i {clause_id}."
                    ),
                    document_id=document_id,
                    clause_id=clause_id,
                )
            )
        else:
            checks.append(
                _check(
                    base_id,
                    CheckStatus.FAIL,
                    (
                        f"{fact.fact_id}: värdet {expected} hittades inte i {clause_id} "
                        f"(dokument {document_id}) och inga explicit_surfaces är deklarerade."
                    ),
                    document_id=document_id,
                    clause_id=clause_id,
                    details={"expected": expected},
                )
            )
    elif site.mode is ClauseMode.PARAPHRASE:
        paraphrases = ", ".join(repr(surface) for surface in fact.paraphrase_surfaces)
        if analysis.paraphrase_span is not None:
            checks.append(
                _check(
                    base_id,
                    CheckStatus.PASS,
                    f"{fact.fact_id}: parafrasform {analysis.paraphrase_surface!r} finns i {clause_id}.",
                    document_id=document_id,
                    clause_id=clause_id,
                )
            )
        elif analysis.semantic_span is not None and (
            analysis.semantic_quantity is not None or analysis.explicit_span is None
        ):
            checks.append(
                _check(
                    base_id,
                    CheckStatus.PASS,
                    (
                        f"{fact.fact_id}: ingen av parafrasformerna ({paraphrases}) finns, "
                        f"men värdet {expected} verifieras semantiskt i {clause_id}."
                    ),
                    document_id=document_id,
                    clause_id=clause_id,
                )
            )
        elif analysis.explicit_span is not None:
            checks.append(
                _check(
                    base_id,
                    CheckStatus.WARN,
                    (
                        f"{fact.fact_id}: klausulen är i parafrasläge men använder den "
                        f"explicita formen {analysis.explicit_surface!r} i {clause_id}. "
                        "Faktumet är korrekt men en parafras vore mer representativ; "
                        "ingen reparation krävs."
                    ),
                    document_id=document_id,
                    clause_id=clause_id,
                    details={"explicit_only": True},
                )
            )
        elif analysis.semantic_span is not None:
            checks.append(
                _check(
                    base_id,
                    CheckStatus.PASS,
                    (
                        f"{fact.fact_id}: värdet {expected} verifieras semantiskt i "
                        f"{clause_id}."
                    ),
                    document_id=document_id,
                    clause_id=clause_id,
                )
            )
        else:
            checks.append(
                _check(
                    base_id,
                    CheckStatus.FAIL,
                    (
                        f"{fact.fact_id}: parafrasläget kräver en parafrasform ({paraphrases}) "
                        f"eller semantisk träff för {expected} i {clause_id}; ingendera hittades."
                    ),
                    document_id=document_id,
                    clause_id=clause_id,
                    details={"expected": expected, "paraphrases": list(fact.paraphrase_surfaces)},
                )
            )
    elif site.mode in (
        ClauseMode.DEFINITION,
        ClauseMode.REFERENCE,
        ClauseMode.CONDITIONAL,
        ClauseMode.EXCEPTION,
        ClauseMode.CONTRADICTION,
        ClauseMode.SPLIT,
        ClauseMode.VAGUE,
        ClauseMode.DISTRACTOR,
    ):
        if analysis.supported or analysis.keyword_span is not None:
            checks.append(
                _check(
                    base_id,
                    CheckStatus.PASS,
                    (
                        f"{fact.fact_id}: klausultypen {site.mode.value} har stöd "
                        f"(nyckelord eller värde) i {clause_id}."
                    ),
                    document_id=document_id,
                    clause_id=clause_id,
                )
            )
        else:
            checks.append(
                _check(
                    base_id,
                    CheckStatus.WARN,
                    (
                        f"{fact.fact_id}: klausultypen {site.mode.value} saknar både nyckelord "
                        f"och värde för {expected} i {clause_id}; manuell granskning rekommenderas."
                    ),
                    document_id=document_id,
                    clause_id=clause_id,
                )
            )
    else:  # pragma: no cover - all ClauseMode members are covered above
        checks.append(
            _check(
                base_id,
                CheckStatus.WARN,
                f"{fact.fact_id}: okänd klausultyp {site.mode!r} i {clause_id}.",
                document_id=document_id,
                clause_id=clause_id,
            )
        )

    checks.extend(_semantic_checks(site))
    return checks


def _semantic_checks(site: _FactSite) -> list[CheckResult]:
    fact = site.fact
    analysis = site.analysis
    check_id = f"document.{site.document_id}.clause.{site.clause_id}.fact.{fact.fact_id}.semantic"
    canonical = semantic.canonical_quantity_for_fact(fact)
    if canonical is None:
        return [
            _check(
                check_id,
                CheckStatus.SKIP,
                f"{fact.fact_id}: faktatypen {fact.kind.value} har ingen numerisk normalisering.",
                document_id=site.document_id,
                clause_id=site.clause_id,
            )
        ]
    expected = semantic.display_expected(fact)
    conflicts = analysis.conflicts
    if analysis.semantic_span is not None and not conflicts:
        return [
            _check(
                check_id,
                CheckStatus.PASS,
                f"{fact.fact_id}: operativt värde {expected} verifieras i {site.clause_id}.",
                document_id=site.document_id,
                clause_id=site.clause_id,
            )
        ]
    if analysis.semantic_span is not None:
        return [
            _check(
                check_id,
                CheckStatus.WARN,
                (
                    f"{fact.fact_id}: rätt värde {expected} finns i {site.clause_id}, men även "
                    f"avvikande värden {semantic.display_conflicts(conflicts)}. "
                    "Distraktorvärden får inte ersätta det operativa värdet."
                ),
                document_id=site.document_id,
                clause_id=site.clause_id,
                details={"conflicts": [quantity.surface for quantity in conflicts]},
            )
        ]
    if conflicts:
        status = (
            CheckStatus.FAIL
            if site.mode in (ClauseMode.EXPLICIT, ClauseMode.PARAPHRASE, ClauseMode.SPLIT)
            else CheckStatus.WARN
        )
        return [
            _check(
                check_id,
                status,
                (
                    f"{fact.fact_id}: operativt värde {expected} saknas i {site.clause_id}; funna "
                    f"avvikande värden {semantic.display_conflicts(conflicts)} kan ersätta det. "
                    "Reparera klausulen så att exakt rätt värde uttrycks."
                ),
                document_id=site.document_id,
                clause_id=site.clause_id,
                details={"conflicts": [quantity.surface for quantity in conflicts]},
            )
        ]
    if analysis.explicit_span is not None or analysis.paraphrase_span is not None:
        return [
            _check(
                check_id,
                CheckStatus.PASS,
                (
                    f"{fact.fact_id}: värdet {expected} verifieras via en deklarerad "
                    f"explicit/parafras-form i {site.clause_id}."
                ),
                document_id=site.document_id,
                clause_id=site.clause_id,
                details={"matched_surface": analysis.explicit_surface or analysis.paraphrase_surface},
            )
        ]
    status = (
        CheckStatus.FAIL
        if site.mode in (ClauseMode.EXPLICIT, ClauseMode.PARAPHRASE, ClauseMode.SPLIT)
        else CheckStatus.WARN
    )
    return [
        _check(
            check_id,
            status,
            (
                f"{fact.fact_id}: inget värde med enheten '{canonical.unit}' för {expected} "
                f"hittades i {site.clause_id}."
            ),
            document_id=site.document_id,
            clause_id=site.clause_id,
            details={"expected": expected},
        )
    ]


# ---------------------------------------------------------------------------
# Document verification
# ---------------------------------------------------------------------------


def _verify_document(
    scenario: Scenario,
    package: GeneratedPackage,
    rendered: dict[str, RenderedDocument],
    spec_document: DocumentSpec,
    sites: list[_FactSite],
    intentionally_missing: set[str],
) -> DocumentVerification:
    document_id = spec_document.document_id
    checks: list[CheckResult] = []
    generated = _generated_document(package, document_id)
    rendered_document = rendered.get(document_id)

    if generated is None:
        checks.append(
            _check(
                f"document.{document_id}.generated.present",
                CheckStatus.FAIL,
                (
                    f"dokumentet {document_id} saknas i genererat paket; förväntade klausuler: "
                    f"{[clause.clause_id for clause in spec_document.clauses]}."
                ),
                document_id=document_id,
            )
        )
        return _document_verification(document_id, checks)

    checks.append(
        _check(
            f"document.{document_id}.generated.present",
            CheckStatus.PASS,
            f"dokumentet {document_id} finns i genererat paket.",
            document_id=document_id,
        )
    )

    if rendered_document is None:
        checks.append(
            _check(
                f"document.{document_id}.rendered.present",
                CheckStatus.FAIL,
                f"kanonisk text saknas för {document_id}; dokumentet kan inte verifieras.",
                document_id=document_id,
            )
        )
    else:
        checks.append(
            _check(
                f"document.{document_id}.rendered.present",
                CheckStatus.PASS,
                (
                    f"kanonisk text finns för {document_id} "
                    f"({len(rendered_document.canonical_text)} tecken)."
                ),
                document_id=document_id,
            )
        )
        expected_clause_ids = [clause.clause_id for clause in spec_document.clauses]
        missing_spans = [
            clause_id
            for clause_id in expected_clause_ids
            if clause_id not in rendered_document.clause_spans
        ]
        span_problems = rendered_document.validate_spans()
        if rendered_document.document_id != document_id:
            span_problems.append(
                f"kanoniskt dokument-id {rendered_document.document_id!r} matchar inte "
                f"{document_id!r}"
            )
        if span_problems or missing_spans:
            checks.append(
                _check(
                    f"document.{document_id}.rendered.spans",
                    CheckStatus.FAIL,
                    (
                        f"{document_id}: ogiltiga/missade klausulspann "
                        f"(saknas: {missing_spans}, problem: {span_problems})."
                    ),
                    document_id=document_id,
                    details={"missing_clause_spans": missing_spans, "problems": span_problems},
                )
            )
        else:
            checks.append(
                _check(
                    f"document.{document_id}.rendered.spans",
                    CheckStatus.PASS,
                    f"{document_id}: alla {len(expected_clause_ids)} klausulspann är giltiga.",
                    document_id=document_id,
                )
            )
        if find_span(rendered_document.canonical_text, generated.title) is None:
            checks.append(
                _check(
                    f"document.{document_id}.rendered.title",
                    CheckStatus.FAIL,
                    f"dokumenttiteln {generated.title!r} saknas i kanonisk text för {document_id}.",
                    document_id=document_id,
                )
            )
        else:
            checks.append(
                _check(
                    f"document.{document_id}.rendered.title",
                    CheckStatus.PASS,
                    f"dokumenttiteln finns i kanonisk text för {document_id}.",
                    document_id=document_id,
                )
            )

    # -- clause structure --------------------------------------------------
    expected_ids = [clause.clause_id for clause in spec_document.clauses]
    generated_ids = [clause.clause_id for clause in generated.clauses]
    generated_set = set(generated_ids)
    expected_set = set(expected_ids)
    missing = [clause_id for clause_id in expected_ids if clause_id not in generated_set]
    extra = [clause_id for clause_id in generated_ids if clause_id not in expected_set]
    duplicates = sorted(
        {clause_id for clause_id in generated_ids if generated_ids.count(clause_id) > 1}
    )
    if missing or extra or duplicates:
        checks.append(
            _check(
                f"document.{document_id}.structure.clause_ids",
                CheckStatus.FAIL,
                (
                    f"{document_id}: fel klausul-id:n (saknas: {missing}, oväntade: {extra}, "
                    f"dubbletter: {duplicates}). Exakt de planerade klausulerna krävs."
                ),
                document_id=document_id,
                details={"missing": missing, "extra": extra, "duplicates": duplicates},
            )
        )
    else:
        checks.append(
            _check(
                f"document.{document_id}.structure.clause_ids",
                CheckStatus.PASS,
                f"{document_id}: alla klausul-id:n matchar planen och är unika.",
                document_id=document_id,
            )
        )
    if len(expected_ids) == len(generated_ids):
        checks.append(
            _check(
                f"document.{document_id}.structure.clause_count",
                CheckStatus.PASS,
                f"{document_id}: {len(expected_ids)} klausuler som förväntat.",
                document_id=document_id,
            )
        )
    else:
        checks.append(
            _check(
                f"document.{document_id}.structure.clause_count",
                CheckStatus.FAIL,
                (
                    f"{document_id}: {len(generated_ids)} klausuler genererades, "
                    f"{len(expected_ids)} förväntades."
                ),
                document_id=document_id,
            )
        )
    expected_present = [clause_id for clause_id in expected_ids if clause_id in generated_set]
    generated_present = [clause_id for clause_id in generated_ids if clause_id in expected_set]
    if expected_present != generated_present:
        checks.append(
            _check(
                f"document.{document_id}.structure.clause_order",
                CheckStatus.FAIL,
                (
                    f"{document_id}: klausulordningen avviker; förväntad {expected_present}, "
                    f"genererad {generated_present}."
                ),
                document_id=document_id,
                details={"expected": expected_present, "generated": generated_present},
            )
        )
    else:
        checks.append(
            _check(
                f"document.{document_id}.structure.clause_order",
                CheckStatus.PASS,
                f"{document_id}: klausulordningen är korrekt.",
                document_id=document_id,
            )
        )

    # -- duplicates --------------------------------------------------------
    heading_groups: dict[str, list[str]] = {}
    text_groups: dict[str, list[str]] = {}
    for clause in generated.clauses:
        if clause.heading.strip():
            heading_groups.setdefault(_norm(clause.heading), []).append(clause.clause_id)
        if clause.text.strip():
            text_groups.setdefault(_norm(clause.text), []).append(clause.clause_id)
    duplicate_headings = {key: ids for key, ids in heading_groups.items() if len(ids) > 1}
    duplicate_texts = {key: ids for key, ids in text_groups.items() if len(ids) > 1}
    if duplicate_headings:
        checks.append(
            _check(
                f"document.{document_id}.duplicates.headings",
                CheckStatus.FAIL,
                f"{document_id}: dubbla klausulrubriker {duplicate_headings}.",
                document_id=document_id,
                details={"duplicates": duplicate_headings},
            )
        )
    else:
        checks.append(
            _check(
                f"document.{document_id}.duplicates.headings",
                CheckStatus.PASS,
                f"{document_id}: inga dubbla klausulrubriker.",
                document_id=document_id,
            )
        )
    if duplicate_texts:
        checks.append(
            _check(
                f"document.{document_id}.duplicates.clause_texts",
                CheckStatus.FAIL,
                f"{document_id}: identisk klausultext upprepas {duplicate_texts}.",
                document_id=document_id,
                details={"duplicates": duplicate_texts},
            )
        )
    else:
        checks.append(
            _check(
                f"document.{document_id}.duplicates.clause_texts",
                CheckStatus.PASS,
                f"{document_id}: ingen klausultext upprepas ordagrant.",
                document_id=document_id,
            )
        )
    definition_owners: dict[str, list[str]] = {}
    for clause in spec_document.clauses:
        if clause.mode is not ClauseMode.DEFINITION:
            continue
        for fact_id in clause.fact_ids:
            definition_owners.setdefault(fact_id, []).append(clause.clause_id)
    duplicate_definitions = {
        fact_id: owners for fact_id, owners in definition_owners.items() if len(owners) > 1
    }
    if duplicate_definitions:
        checks.append(
            _check(
                f"document.{document_id}.duplicates.definitions",
                CheckStatus.FAIL,
                f"{document_id}: samma term definieras i flera klausuler {duplicate_definitions}.",
                document_id=document_id,
                details={"duplicates": duplicate_definitions},
            )
        )
    else:
        checks.append(
            _check(
                f"document.{document_id}.duplicates.definitions",
                CheckStatus.PASS,
                f"{document_id}: inga dubbla definitioner.",
                document_id=document_id,
            )
        )

    # -- clause contents + facts ------------------------------------------
    for spec_clause in spec_document.clauses:
        clause_id = spec_clause.clause_id
        generated_clause = _generated_clause(generated, clause_id)
        if generated_clause is None:
            checks.append(
                _check(
                    f"document.{document_id}.clause.{clause_id}.generated",
                    CheckStatus.FAIL,
                    f"{document_id}/{clause_id}: klausulen saknas i genererat dokument.",
                    document_id=document_id,
                    clause_id=clause_id,
                )
            )
            continue
        if not generated_clause.heading.strip():
            checks.append(
                _check(
                    f"document.{document_id}.clause.{clause_id}.heading",
                    CheckStatus.FAIL,
                    f"{document_id}/{clause_id}: klausulrubriken är tom.",
                    document_id=document_id,
                    clause_id=clause_id,
                )
            )
        if not generated_clause.text.strip():
            checks.append(
                _check(
                    f"document.{document_id}.clause.{clause_id}.text",
                    CheckStatus.FAIL,
                    f"{document_id}/{clause_id}: klausultexten är tom.",
                    document_id=document_id,
                    clause_id=clause_id,
                )
            )
        if rendered_document is not None and clause_id in rendered_document.clause_spans:
            heading_span = _canonical_span(rendered_document, clause_id, generated_clause.heading)
            if heading_span is None:
                checks.append(
                    _check(
                        f"document.{document_id}.clause.{clause_id}.heading_present",
                        CheckStatus.FAIL,
                        (
                            f"{document_id}/{clause_id}: rubriken "
                            f"{generated_clause.heading!r} saknas i spannet."
                        ),
                        document_id=document_id,
                        clause_id=clause_id,
                    )
                )
            else:
                checks.append(
                    _check(
                        f"document.{document_id}.clause.{clause_id}.heading_present",
                        CheckStatus.PASS,
                        f"{document_id}/{clause_id}: rubriken finns i spannet.",
                        document_id=document_id,
                        clause_id=clause_id,
                    )
                )
            text_span = _canonical_span(rendered_document, clause_id, generated_clause.text)
            if text_span is None:
                checks.append(
                    _check(
                        f"document.{document_id}.clause.{clause_id}.text_present",
                        CheckStatus.FAIL,
                        (
                            f"{document_id}/{clause_id}: klausultexten återfinns inte ordagrant "
                            "i sitt spann; kanonisk text får inte reflowa klausulen."
                        ),
                        document_id=document_id,
                        clause_id=clause_id,
                    )
                )
            else:
                checks.append(
                    _check(
                        f"document.{document_id}.clause.{clause_id}.text_present",
                        CheckStatus.PASS,
                        f"{document_id}/{clause_id}: klausultexten finns ordagrant i spannet.",
                        document_id=document_id,
                        clause_id=clause_id,
                    )
                )
        for fact_id in _dedupe(list(spec_clause.fact_ids)):
            try:
                scenario.fact(fact_id)
            except KeyError:  # pragma: no cover - scenario validation forbids it
                checks.append(
                    _check(
                        f"document.{document_id}.clause.{clause_id}.fact.{fact_id}.known",
                        CheckStatus.FAIL,
                        f"{fact_id}: klausulen refererar ett okänt faktum.",
                        document_id=document_id,
                        clause_id=clause_id,
                    )
                )
                continue
            site = _sites_for(sites, document_id, clause_id, fact_id)
            if site is None:  # pragma: no cover - _collect_fact_sites builds every site
                continue
            checks.extend(_fact_checks(site, intentionally_missing))
        for reference in _dedupe(list(spec_clause.reference_to)):
            resolved = _resolve_token(scenario, reference)
            if resolved.ok:
                checks.append(
                    _check(
                        f"document.{document_id}.clause.{clause_id}.references",
                        CheckStatus.PASS,
                        f"{document_id}/{clause_id}: referensen {reference!r} är giltig.",
                        document_id=document_id,
                        clause_id=clause_id,
                    )
                )
            else:
                checks.append(
                    _check(
                        f"document.{document_id}.clause.{clause_id}.references",
                        CheckStatus.FAIL,
                        (
                            f"{document_id}/{clause_id}: referensen {reference!r} pekar på ett "
                            "okänt dokument/klausul och är inte deklarerad i missing_documents."
                        ),
                        document_id=document_id,
                        clause_id=clause_id,
                        details={"reference": reference},
                    )
                )

    # -- document references ----------------------------------------------
    reference_targets = _dedupe(
        [*list(spec_document.references_documents), *list(generated.references)]
    )
    known_targets = {document.document_id for document in scenario.documents}
    missing_targets = set(scenario.missing_documents)
    unknown_targets = [
        target for target in reference_targets if target not in known_targets | missing_targets
    ]
    if unknown_targets:
        checks.append(
            _check(
                f"document.{document_id}.references",
                CheckStatus.FAIL,
                (
                    f"{document_id}: dokumentreferenser till okända mål {unknown_targets}; "
                    "de måste finnas i paketet eller deklareras i missing_documents."
                ),
                document_id=document_id,
                details={"unknown_targets": unknown_targets},
            )
        )
    else:
        checks.append(
            _check(
                f"document.{document_id}.references",
                CheckStatus.PASS,
                (
                    f"{document_id}: alla dokumentreferenser {reference_targets} är giltiga "
                    "eller deklareras som saknade."
                ),
                document_id=document_id,
                details={"targets": reference_targets},
            )
        )

    return _document_verification(document_id, checks)


# ---------------------------------------------------------------------------
# Package-level checks
# ---------------------------------------------------------------------------


def _package_structure_checks(
    scenario: Scenario, package: GeneratedPackage, rendered: dict[str, RenderedDocument]
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    expected_ids = [document.document_id for document in scenario.documents]
    generated_ids = [document.document_id for document in package.documents]
    missing = [document_id for document_id in expected_ids if document_id not in generated_ids]
    extra = [document_id for document_id in generated_ids if document_id not in expected_ids]
    duplicates = sorted(
        {document_id for document_id in generated_ids if generated_ids.count(document_id) > 1}
    )
    if missing or extra or duplicates:
        checks.append(
            _check(
                "package.structure.documents",
                CheckStatus.FAIL,
                (
                    f"dokumentpaketet stämmer inte: saknas {missing}, oväntade {extra}, "
                    f"dubbletter {duplicates}. Förväntade dokument: {expected_ids}."
                ),
                details={"missing": missing, "extra": extra, "duplicates": duplicates},
            )
        )
    else:
        checks.append(
            _check(
                "package.structure.documents",
                CheckStatus.PASS,
                f"dokumentpaketet innehåller exakt de förväntade dokumenten {expected_ids}.",
                details={"documents": expected_ids},
            )
        )
    if len(expected_ids) == len(generated_ids):
        checks.append(
            _check(
                "package.structure.document_count",
                CheckStatus.PASS,
                f"antal dokument {len(expected_ids)} stämmer.",
            )
        )
    else:
        checks.append(
            _check(
                "package.structure.document_count",
                CheckStatus.FAIL,
                (
                    f"antal dokument {len(generated_ids)} stämmer inte; "
                    f"{len(expected_ids)} förväntades."
                ),
            )
        )
    if duplicates:
        checks.append(
            _check(
                "package.structure.unique_document_ids",
                CheckStatus.FAIL,
                f"dubblerade dokument-id:n: {duplicates}.",
            )
        )
    else:
        checks.append(
            _check(
                "package.structure.unique_document_ids",
                CheckStatus.PASS,
                "alla dokument-id:n är unika.",
            )
        )
    unexpected_rendered = [
        document_id for document_id in rendered if document_id not in generated_ids
    ]
    if unexpected_rendered:
        checks.append(
            _check(
                "package.rendered.unexpected_documents",
                CheckStatus.WARN,
                f"renderade dokument utan motsvarighet i paketet: {unexpected_rendered}.",
            )
        )
    else:
        checks.append(
            _check(
                "package.rendered.unexpected_documents",
                CheckStatus.PASS,
                "inga oväntade renderade dokument.",
            )
        )
    expected_clauses = [clause for document in scenario.documents for clause in document.clauses]
    generated_clauses = [
        clause for document in package.documents for clause in document.clauses
    ]
    if len(expected_clauses) == len(generated_clauses):
        checks.append(
            _check(
                "package.expected_counts.clauses",
                CheckStatus.PASS,
                f"antal klausuler {len(expected_clauses)} stämmer.",
            )
        )
    else:
        checks.append(
            _check(
                "package.expected_counts.clauses",
                CheckStatus.FAIL,
                (
                    f"{len(generated_clauses)} klausuler genererades, "
                    f"{len(expected_clauses)} förväntades."
                ),
                details={
                    "expected": len(expected_clauses),
                    "generated": len(generated_clauses),
                },
            )
        )
    return checks


def _entity_checks(
    scenario: Scenario, rendered: dict[str, RenderedDocument]
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    canonical_items = list(rendered.values())
    if not scenario.parties:
        checks.append(
            _check(
                "package.entity.none",
                CheckStatus.SKIP,
                "scenariot har inga parter att verifiera.",
            )
        )
        return checks
    for party in scenario.parties:
        found_document: str | None = None
        for document in canonical_items:
            if find_span(document.canonical_text, party.name) is not None:
                found_document = document.document_id
                break
        if found_document is None:
            checks.append(
                _check(
                    f"package.entity.{party.party_id}",
                    CheckStatus.FAIL,
                    (
                        f"parten {party.name!r} ({party.party_id}) saknas i dokumenttexten; "
                        "avtalsparternas namn måste framgå av avtalet."
                    ),
                    details={"party_id": party.party_id, "name": party.name},
                )
            )
            continue
        checks.append(
            _check(
                f"package.entity.{party.party_id}",
                CheckStatus.PASS,
                f"parten {party.name!r} finns i {found_document}.",
                document_id=found_document,
                details={"party_id": party.party_id, "name": party.name},
            )
        )
        if party.org_number:
            present = any(
                find_span(document.canonical_text, party.org_number) is not None
                for document in canonical_items
            )
            checks.append(
                _check(
                    f"package.entity.{party.party_id}.org_number",
                    CheckStatus.PASS if present else CheckStatus.WARN,
                    (
                        f"organisationsnummer {party.org_number!r} finns i texten."
                        if present
                        else (
                            f"organisationsnummer {party.org_number!r} för {party.name!r} "
                            "återfinns inte i texten; kontrollera om det ska anges."
                        )
                    ),
                    details={"org_number": party.org_number},
                )
            )
        if party.address:
            present = any(
                find_span(document.canonical_text, party.address) is not None
                for document in canonical_items
            )
            checks.append(
                _check(
                    f"package.entity.{party.party_id}.address",
                    CheckStatus.PASS if present else CheckStatus.WARN,
                    (
                        f"adressen {party.address!r} finns i texten."
                        if present
                        else (
                            f"adressen {party.address!r} för {party.name!r} återfinns inte i "
                            "texten; kontrollera om den ska anges."
                        )
                    ),
                    details={"address": party.address},
                )
            )
        role_present = any(
            bool(party.role and party.role.strip())
            and find_span(document.canonical_text, party.role) is not None
            for document in canonical_items
        )
        checks.append(
            _check(
                f"package.entity.{party.party_id}.role",
                CheckStatus.PASS if role_present else CheckStatus.WARN,
                (
                    f"rollen {party.role!r} finns i texten."
                    if role_present
                    else (
                        f"rollen {party.role!r} för {party.name!r} uttrycks inte ordagrant i "
                        "texten; verifiera att rätt part har rätt roll."
                    )
                ),
                details={"role": party.role},
            )
        )
    return checks


def _fact_coverage_checks(
    sites: list[_FactSite],
    support: dict[str, bool],
    intentionally_missing: set[str],
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    assigned: dict[str, list[str]] = {}
    for site in sites:
        assigned.setdefault(site.fact.fact_id, []).append(site.clause_id)
    assigned = {
        fact_id: clause_ids
        for fact_id, clause_ids in assigned.items()
        if fact_id not in intentionally_missing
    }
    unsupported = [
        fact_id for fact_id, _clause_ids in assigned.items() if not support.get(fact_id, False)
    ]
    total = len(assigned)
    supported = total - len(unsupported)
    if unsupported:
        locations = {fact_id: assigned[fact_id] for fact_id in unsupported}
        checks.append(
            _check(
                "package.expected_counts.facts",
                CheckStatus.FAIL,
                (
                    f"{len(unsupported)} av {total} förväntade fakta saknar stöd i texten: "
                    f"{locations}. Generera om de berörda klausulerna så att fakta uttrycks."
                ),
                details={"unsupported": locations, "supported": supported, "total": total},
            )
        )
    else:
        checks.append(
            _check(
                "package.expected_counts.facts",
                CheckStatus.PASS,
                f"alla {total} förväntade fakta har stöd i texten.",
                details={"total": total},
            )
        )
    return checks


def _relation_checks(scenario: Scenario) -> list[CheckResult]:
    if not scenario.relations:
        return [
            _check(
                "package.references.relations",
                CheckStatus.SKIP,
                "scenariot deklarerar inga relationer.",
            )
        ]
    checks: list[CheckResult] = []
    for relation in scenario.relations:
        source = _resolve_token(scenario, relation.source)
        target = _resolve_token(scenario, relation.target)
        problems: list[str] = []
        if not source.ok:
            problems.append(f"källan {relation.source!r} är okänd")
        if not target.ok:
            problems.append(f"målet {relation.target!r} är okänt")
        if problems:
            checks.append(
                _check(
                    f"package.references.relation.{relation.relation_id}",
                    CheckStatus.FAIL,
                    (
                        f"relation {relation.relation_id} ({relation.kind.value}): "
                        + "; ".join(problems)
                    ),
                    details={"source": relation.source, "target": relation.target},
                )
            )
        else:
            checks.append(
                _check(
                    f"package.references.relation.{relation.relation_id}",
                    CheckStatus.PASS,
                    (
                        f"relation {relation.relation_id} ({relation.kind.value}) pekar på "
                        "giltiga noder."
                    ),
                    details={"source": relation.source, "target": relation.target},
                )
            )
    return checks


def _anomaly_checks(scenario: Scenario, support: dict[str, bool]) -> list[CheckResult]:
    if not scenario.anomalies:
        return [
            _check(
                "package.anomalies.present",
                CheckStatus.SKIP,
                "scenariot deklarerar inga avsiktliga anomalies.",
            )
        ]
    checks: list[CheckResult] = []
    for anomaly in scenario.anomalies:
        check_id = f"package.anomaly.{anomaly.anomaly_id}"
        problems: list[str] = []
        unsupported = [
            fact_id for fact_id in anomaly.fact_ids if not support.get(fact_id, False)
        ]
        governing_missing = (
            anomaly.governing_fact_id is not None
            and not support.get(anomaly.governing_fact_id, False)
        )
        if anomaly.kind.value == "missing_information":
            if not scenario.missing_documents and anomaly.answerability not in (
                Answerability.INSUFFICIENT_EVIDENCE,
                Answerability.CONDITIONAL,
            ):
                problems.append(
                    "avsiktlig informationslucka saknar deklarerade missing_documents "
                    "och answerability är inte INSUFFICIENT_EVIDENCE/CONDITIONAL"
                )
            if unsupported and not scenario.missing_documents:
                problems.append(f"anomalins fakta saknar stöd: {unsupported}")
        elif anomaly.answerability is Answerability.CONFLICTING_EVIDENCE:
            supported_count = sum(
                1 for fact_id in anomaly.fact_ids if support.get(fact_id, False)
            )
            if unsupported:
                problems.append(f"motsägelsefakta saknar stöd i texten: {unsupported}")
            if supported_count < 2 and not (
                anomaly.governing_fact_id
                and support.get(anomaly.governing_fact_id, False)
                and supported_count >= 1
            ):
                problems.append(
                    "CONFLICTING_EVIDENCE kräver minst två uttryckta fakta "
                    "(eller ett uttryckt fakta plus governande fakta)"
                )
        else:
            if unsupported:
                problems.append(f"anomalins fakta saknar stöd i texten: {unsupported}")
            if governing_missing:
                problems.append(
                    f"governande faktum {anomaly.governing_fact_id!r} saknar stöd i texten"
                )
        if problems:
            checks.append(
                _check(
                    check_id,
                    CheckStatus.FAIL,
                    (
                        f"anomalin {anomaly.anomaly_id} ({anomaly.kind.value}) finns inte där "
                        "scenariot säger: " + "; ".join(problems) + "."
                    ),
                    details={"anomaly_id": anomaly.anomaly_id, "problems": problems},
                )
            )
        else:
            checks.append(
                _check(
                    check_id,
                    CheckStatus.PASS,
                    f"anomalin {anomaly.anomaly_id} ({anomaly.kind.value}) finns i dokumenten.",
                    details={"anomaly_id": anomaly.anomaly_id},
                )
            )
    return checks


# ---------------------------------------------------------------------------
# Gold verification
# ---------------------------------------------------------------------------


def _gold_value_problems(fact: Fact, gold_fact: GoldFact) -> list[str]:
    problems: list[str] = []
    canonical = semantic.canonical_quantity_for_fact(fact)
    if canonical is not None:
        gold_canonical = semantic.canonical_quantity_for_fact(gold_fact)  # type: ignore[arg-type]
        expected = semantic.display_expected(fact)
        if gold_canonical is None:
            problems.append(
                f"gold-faktum {fact.fact_id} har värdet {gold_fact.value!r} som inte kan "
                f"normaliseras; scenario-värdet är {expected}"
            )
        elif gold_canonical.value != canonical.value or gold_canonical.unit != canonical.unit:
            problems.append(
                f"gold-faktum {fact.fact_id} anger ett annat värde än scenariot "
                f"(gold {gold_fact.value!r} {gold_fact.unit!r}, scenario {expected})"
            )
    elif gold_fact.value != fact.value:
        problems.append(
            f"gold-faktum {fact.fact_id} anger {gold_fact.value!r}, scenariot anger {fact.value!r}"
        )
    if gold_fact.status != fact.status:
        problems.append(
            f"gold-faktum {fact.fact_id} har status {gold_fact.status.value}, "
            f"scenariot har {fact.status.value}"
        )
    if gold_fact.currency != fact.currency:
        problems.append(
            f"gold-faktum {fact.fact_id} har valuta {gold_fact.currency!r}, "
            f"scenariot har {fact.currency!r}"
        )
    if gold_fact.entity_id != fact.entity_id:
        problems.append(
            f"gold-faktum {fact.fact_id} har entity {gold_fact.entity_id!r}, "
            f"scenariot har {fact.entity_id!r}"
        )
    return problems


def _gold_precision_problems(
    fact: Fact, gold_fact: GoldFact, rendered: dict[str, RenderedDocument]
) -> list[str]:
    if gold_fact.precision is not EvidencePrecision.EXACT:
        return []
    surfaces = [surface for surface in fact.explicit_surfaces if surface.strip()]
    if not surfaces:
        return []
    for span in gold_fact.evidence:
        document = rendered.get(span.document_id)
        if document is None:
            continue
        slice_text = document.canonical_text[span.start : span.end]
        for surface in surfaces:
            if contains_span_text(slice_text, 0, len(slice_text), surface):
                return []
    return [
        f"gold-faktum {fact.fact_id} är markerat precision=exact men inget evidensspann "
        f"innehåller någon av de explicita formerna {surfaces}"
    ]


def _validate_gold(
    scenario: Scenario,
    rendered: dict[str, RenderedDocument],
    gold: GoldSet,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    if gold.contract_id != scenario.contract_id:
        checks.append(
            _check(
                "gold.contract_id",
                CheckStatus.FAIL,
                f"gold.contract_id {gold.contract_id!r} matchar inte scenariot "
                f"{scenario.contract_id!r}.",
            )
        )
    else:
        checks.append(
            _check(
                "gold.contract_id",
                CheckStatus.PASS,
                f"gold.contract_id matchar scenariot ({scenario.contract_id}).",
            )
        )
    gold_facts = {gold_fact.fact_id: gold_fact for gold_fact in gold.facts}
    scenario_fact_ids = {fact.fact_id for fact in scenario.facts}
    intentionally_missing = _intentionally_missing_facts(scenario)
    for fact in scenario.facts:
        prefix = f"gold.fact.{fact.fact_id}"
        gold_fact = gold_facts.get(fact.fact_id)
        if gold_fact is None:
            checks.append(
                _check(
                    f"{prefix}.present",
                    CheckStatus.FAIL,
                    f"gold saknar scenario-faktum {fact.fact_id}; facit får inte utelämna sanningen.",
                )
            )
            continue
        checks.append(
            _check(
                f"{prefix}.present",
                CheckStatus.PASS,
                f"gold innehåller scenario-faktum {fact.fact_id}.",
            )
        )
        value_problems = _gold_value_problems(fact, gold_fact)
        if value_problems:
            checks.append(
                _check(
                    f"{prefix}.value",
                    CheckStatus.FAIL,
                    "; ".join(value_problems) + ".",
                    details={"fact_id": fact.fact_id},
                )
            )
        else:
            checks.append(
                _check(
                    f"{prefix}.value",
                    CheckStatus.PASS,
                    f"gold-värdet för {fact.fact_id} matchar scenariot.",
                )
            )
        evidence_problems = _span_problems(
            gold_fact.evidence, rendered, f"gold.faktum {fact.fact_id}"
        )
        if not gold_fact.evidence and fact.fact_id not in intentionally_missing:
            evidence_problems.append(
                f"gold-faktum {fact.fact_id} saknar evidensspann; facit måste vara spårbart i texten"
            )
        precision_problems = _gold_precision_problems(fact, gold_fact, rendered)
        if evidence_problems or precision_problems:
            checks.append(
                _check(
                    f"{prefix}.evidence",
                    CheckStatus.FAIL,
                    " ".join(evidence_problems + precision_problems),
                    details={"fact_id": fact.fact_id},
                )
            )
        else:
            checks.append(
                _check(
                    f"{prefix}.evidence",
                    CheckStatus.PASS,
                    (
                        f"gold-faktum {fact.fact_id} har {len(gold_fact.evidence)} giltiga "
                        f"evidensspann (precision {gold_fact.precision.value})."
                    ),
                )
            )
    for gold_fact in gold.facts:
        if gold_fact.fact_id not in scenario_fact_ids:
            checks.append(
                _check(
                    f"gold.fact.{gold_fact.fact_id}.unknown",
                    CheckStatus.FAIL,
                    (
                        f"gold innehåller faktum {gold_fact.fact_id!r} som inte finns i scenariot; "
                        "facit får aldrig påstå något scenariot inte stöder."
                    ),
                )
            )
    checks.extend(_gold_entity_checks(scenario, rendered, gold))
    checks.extend(_gold_reference_checks(scenario, rendered, gold))
    checks.extend(_gold_anomaly_checks(scenario, gold))
    if set(gold.missing_documents) != set(scenario.missing_documents):
        checks.append(
            _check(
                "gold.missing_documents",
                CheckStatus.FAIL,
                (
                    f"gold.missing_documents {gold.missing_documents} matchar inte scenariot "
                    f"{scenario.missing_documents}."
                ),
            )
        )
    else:
        checks.append(
            _check(
                "gold.missing_documents",
                CheckStatus.PASS,
                f"gold.missing_documents matchar scenariot ({scenario.missing_documents}).",
            )
        )
    return checks


def _gold_entity_checks(
    scenario: Scenario, rendered: dict[str, RenderedDocument], gold: GoldSet
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    gold_entities = {entity.entity_id: entity for entity in gold.entities}
    scenario_party_ids = {party.party_id for party in scenario.parties}
    for party in scenario.parties:
        prefix = f"gold.entity.{party.party_id}"
        gold_entity: GoldEntity | None = gold_entities.get(party.party_id)
        if gold_entity is None:
            checks.append(
                _check(
                    f"{prefix}.present",
                    CheckStatus.FAIL,
                    f"gold saknar parten {party.party_id} ({party.name}).",
                )
            )
            continue
        problems: list[str] = []
        if gold_entity.name != party.name:
            problems.append(f"namnet {gold_entity.name!r} matchar inte {party.name!r}")
        if gold_entity.role != party.role:
            problems.append(f"rollen {gold_entity.role!r} matchar inte {party.role!r}")
        evidence_problems = _span_problems(
            gold_entity.evidence, rendered, f"gold.enhet {party.party_id}"
        )
        if not gold_entity.evidence:
            evidence_problems.append("enheten saknar evidensspann")
        else:
            supported = False
            for span in gold_entity.evidence:
                document = rendered.get(span.document_id)
                if document is None:
                    continue
                slice_text = document.canonical_text[span.start : span.end]
                if contains_span_text(slice_text, 0, len(slice_text), party.name):
                    supported = True
                    break
            if not supported:
                problems.append(f"evidensspannet innehåller inte partnamnet {party.name!r}")
        problems.extend(evidence_problems)
        if problems:
            checks.append(
                _check(
                    f"{prefix}.value",
                    CheckStatus.FAIL,
                    f"gold-entiteten {party.party_id} stöds inte: " + "; ".join(problems) + ".",
                )
            )
        else:
            checks.append(
                _check(
                    f"{prefix}.value",
                    CheckStatus.PASS,
                    f"gold-entiteten {party.party_id} har namn, roll och giltig evidens.",
                )
            )
    for gold_entity in gold.entities:
        if gold_entity.entity_id not in scenario_party_ids:
            checks.append(
                _check(
                    f"gold.entity.{gold_entity.entity_id}.unknown",
                    CheckStatus.FAIL,
                    (
                        f"gold innehåller enheten {gold_entity.entity_id!r} som inte finns i "
                        "scenariot."
                    ),
                )
            )
    return checks


def _gold_reference_checks(
    scenario: Scenario, rendered: dict[str, RenderedDocument], gold: GoldSet
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    if not gold.references:
        checks.append(
            _check(
                "gold.references",
                CheckStatus.SKIP,
                "gold innehåller inga referenser.",
            )
        )
        return checks
    known_documents = {document.document_id for document in scenario.documents}
    declared_missing = set(scenario.missing_documents)
    problems: list[str] = []
    for index, reference in enumerate(gold.references):
        label = f"gold.reference.{index}"
        if reference.source_document_id not in known_documents:
            problems.append(
                f"{label}: källdokument {reference.source_document_id!r} finns inte i scenariot"
            )
        if (
            reference.target_document_id not in known_documents
            and reference.target_document_id not in declared_missing
        ):
            problems.append(
                f"{label}: måldokument {reference.target_document_id!r} finns varken i paketet "
                "eller i missing_documents"
            )
        evidence_problems = _span_problems(reference.evidence, rendered, label)
        if not reference.evidence:
            evidence_problems.append(f"{label}: referensen saknar evidensspann")
        problems.extend(evidence_problems)
    if problems:
        checks.append(
            _check(
                "gold.references",
                CheckStatus.FAIL,
                "ogiltiga gold-referenser: " + "; ".join(problems) + ".",
            )
        )
    else:
        checks.append(
            _check(
                "gold.references",
                CheckStatus.PASS,
                f"alla {len(gold.references)} gold-referenser är giltiga.",
            )
        )
    return checks


def _gold_anomaly_checks(scenario: Scenario, gold: GoldSet) -> list[CheckResult]:
    checks: list[CheckResult] = []
    gold_anomalies = {anomaly.anomaly_id: anomaly for anomaly in gold.anomalies}
    scenario_ids = {anomaly.anomaly_id for anomaly in scenario.anomalies}
    for anomaly in scenario.anomalies:
        prefix = f"gold.anomaly.{anomaly.anomaly_id}"
        gold_anomaly: GoldAnomaly | None = gold_anomalies.get(anomaly.anomaly_id)
        if gold_anomaly is None:
            checks.append(
                _check(
                    f"{prefix}.present",
                    CheckStatus.FAIL,
                    f"gold saknar anomalin {anomaly.anomaly_id}.",
                )
            )
            continue
        problems: list[str] = []
        if gold_anomaly.kind != anomaly.kind:
            problems.append(
                f"kind {gold_anomaly.kind.value!r} matchar inte {anomaly.kind.value!r}"
            )
        if gold_anomaly.answerability is not anomaly.answerability:
            problems.append(
                f"answerability {gold_anomaly.answerability.value!r} matchar inte "
                f"{anomaly.answerability.value!r}"
            )
        if set(gold_anomaly.fact_ids) != set(anomaly.fact_ids):
            problems.append(
                f"fact_ids {gold_anomaly.fact_ids} matchar inte {anomaly.fact_ids}"
            )
        if gold_anomaly.governing_fact_id != anomaly.governing_fact_id:
            problems.append(
                f"governing_fact_id {gold_anomaly.governing_fact_id!r} matchar inte "
                f"{anomaly.governing_fact_id!r}"
            )
        evidence_optional = anomaly.kind.value in ("missing_information", "ambiguity") or (
            anomaly.answerability
            in (Answerability.INSUFFICIENT_EVIDENCE, Answerability.CONDITIONAL)
        )
        if not evidence_optional and not gold_anomaly.evidence:
            problems.append("anomalin saknar evidensspann")
        if (
            anomaly.answerability is Answerability.CONFLICTING_EVIDENCE
            and len({(span.document_id, span.start, span.end) for span in gold_anomaly.evidence})
            < 2
        ):
            problems.append("CONFLICTING_EVIDENCE kräver minst två distinkta evidensspann")
        if problems:
            checks.append(
                _check(
                    f"{prefix}.value",
                    CheckStatus.FAIL,
                    f"gold-anomalin {anomaly.anomaly_id} stöds inte: " + "; ".join(problems) + ".",
                )
            )
        else:
            checks.append(
                _check(
                    f"{prefix}.value",
                    CheckStatus.PASS,
                    f"gold-anomalin {anomaly.anomaly_id} matchar scenariot.",
                )
            )
    for gold_anomaly in gold.anomalies:
        if gold_anomaly.anomaly_id not in scenario_ids:
            checks.append(
                _check(
                    f"gold.anomaly.{gold_anomaly.anomaly_id}.unknown",
                    CheckStatus.FAIL,
                    (
                        f"gold innehåller anomalin {gold_anomaly.anomaly_id!r} som inte finns i "
                        "scenariot."
                    ),
                )
            )
    return checks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_package(
    scenario: Scenario,
    package: GeneratedPackage,
    rendered: dict[str, RenderedDocument],
    *,
    gold: GoldSet | None = None,
) -> PackageVerification:
    """Verify a generated package against the scenario truth.

    Deterministic only: the same inputs always produce the same checks and
    messages. Gold is verified when supplied so facit can never claim more than
    the canonical text supports.
    """

    package_checks: list[CheckResult] = []
    sites = _collect_fact_sites(scenario, package, rendered)
    support = _support_index(sites)
    intentionally_missing = _intentionally_missing_facts(scenario)

    if package.contract_id == scenario.contract_id:
        package_checks.append(
            _check(
                "package.contract_id",
                CheckStatus.PASS,
                f"paketets contract_id matchar scenariot ({scenario.contract_id}).",
            )
        )
    else:
        package_checks.append(
            _check(
                "package.contract_id",
                CheckStatus.FAIL,
                f"paketets contract_id {package.contract_id!r} matchar inte "
                f"scenariot {scenario.contract_id!r}.",
            )
        )

    package_checks.extend(_package_structure_checks(scenario, package, rendered))
    package_checks.extend(_entity_checks(scenario, rendered))
    package_checks.extend(
        _fact_coverage_checks(sites, support, intentionally_missing)
    )
    package_checks.extend(_relation_checks(scenario))
    package_checks.extend(_anomaly_checks(scenario, support))

    documents: list[DocumentVerification] = []
    for spec_document in scenario.documents:
        documents.append(
            _verify_document(
                scenario, package, rendered, spec_document, sites, intentionally_missing
            )
        )

    if gold is not None:
        package_checks.extend(_validate_gold(scenario, rendered, gold))

    verification = PackageVerification(
        contract_id=package.contract_id,
        documents=documents,
        checks=package_checks,
    )
    verification.recompute_stats()
    package_failed = any(
        check.status is CheckStatus.FAIL for check in package_checks
    )
    documents_failed = any(
        check.status is CheckStatus.FAIL for document in documents for check in document.checks
    )
    if package_failed or documents_failed:
        verification.status = DocumentStatus.REJECTED
    return verification
