"""Canonical document rendering.

The canonical text is the single source of truth for character offsets. Every
evidence span, every gold fact and every dataset check ultimately points into
this text, so the renderer is deterministic and *never* reflows clause text: a
clause's generated text appears verbatim between its span boundaries.

Metadata header (:data:`SYNTHETIC_MARKER` and the contract/document fields) makes
every exported artifact self-identifying as synthetic test data.
"""

from __future__ import annotations

from ..errors import GenerationError
from ..models import (
    DocumentQuality,
    DocumentSpec,
    GeneratedClause,
    GeneratedDocument,
    GeneratedPackage,
    RenderedDocument,
    Scenario,
    TextSpan,
)

CANONICAL_ENCODING = "utf-8"

SYNTHETIC_MARKER = "SYNTETISK TESTDATA – ContractForge"


def _document_title(scenario: Scenario, document: GeneratedDocument, spec: DocumentSpec) -> str:
    # The generated title is what the document is rendered with; the scenario
    # title is the fallback when generation produced an empty title.
    if document.title.strip():
        return document.title
    return spec.title(scenario.language)


def _header_lines(
    scenario: Scenario, document: GeneratedDocument, spec: DocumentSpec
) -> list[str]:
    return [
        SYNTHETIC_MARKER,
        f"contract_id: {scenario.contract_id}",
        f"document_id: {document.document_id}",
        f"dokumenttyp: {spec.kind.value}",
        f"språk: {scenario.language.value}",
        f"jurisdiktion: {scenario.jurisdiction}",
    ]


def _ordered_clauses(
    document: GeneratedDocument, spec: DocumentSpec
) -> list[GeneratedClause]:
    """Return generated clauses in the planned order, rejecting structural drift.

    Rendering owns the canonical *order* (it must match the scenario plan); the
    generated heading and text are used verbatim. Missing or unexpected clause
    ids are a generation error, not something the renderer may paper over.
    """

    generated = {clause.clause_id: clause for clause in document.clauses}
    planned = [clause.clause_id for clause in spec.clauses]
    planned_set = set(planned)
    missing = [clause_id for clause_id in planned if clause_id not in generated]
    unexpected = sorted(clause_id for clause_id in generated if clause_id not in planned_set)
    if missing or unexpected:
        raise GenerationError(
            "generated document does not match the scenario clause plan for "
            f"{document.document_id}: missing={missing}, unexpected={unexpected}"
        )
    return [generated[clause_id] for clause_id in planned]


def render_document(
    scenario: Scenario, document: GeneratedDocument, spec: DocumentSpec
) -> RenderedDocument:
    """Render one generated document to canonical text with exact clause spans."""

    if document.document_id != spec.document_id:
        raise GenerationError(
            f"generated document {document.document_id!r} rendered against spec "
            f"{spec.document_id!r}"
        )

    title = _document_title(scenario, document, spec)
    clauses = _ordered_clauses(document, spec)
    quality = scenario.variation.document_quality
    # One blank line separates clause blocks in the standard layout; the
    # ``line_breaks`` variation adds a second one. Offsets are always computed
    # from the text actually produced, so neither variation can break spans.
    separator = "\n\n" if quality == DocumentQuality.LINE_BREAKS else "\n"

    text = "\n".join(_header_lines(scenario, document, spec)) + "\n\n"
    text += title + "\n\n"

    spans: dict[str, TextSpan] = {}
    for index, clause in enumerate(clauses, start=1):
        if index > 1:
            text += separator
        start = len(text)
        heading_line = f"{index}. {clause.heading}"
        text += heading_line + "\n"
        text += clause.text
        end = len(text)
        text += "\n"
        spans[clause.clause_id] = TextSpan(
            document_id=document.document_id,
            clause_id=clause.clause_id,
            start=start,
            end=end,
        )

    return RenderedDocument(
        document_id=document.document_id,
        kind=spec.kind,
        title=title,
        language=scenario.language,
        canonical_text=text,
        clause_spans=spans,
    )


def render_package(
    scenario: Scenario, package: GeneratedPackage
) -> dict[str, RenderedDocument]:
    """Render every document in a generated package, keyed by document id."""

    if package.contract_id != scenario.contract_id:
        raise GenerationError(
            f"generated package belongs to {package.contract_id!r}, "
            f"not scenario {scenario.contract_id!r}"
        )

    rendered: dict[str, RenderedDocument] = {}
    for document in package.documents:
        try:
            spec = scenario.document(document.document_id)
        except KeyError as exc:
            raise GenerationError(
                f"generated document {document.document_id!r} has no scenario document spec"
            ) from exc
        rendered[document.document_id] = render_document(scenario, document, spec)
    return rendered
