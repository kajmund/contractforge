"""Document generation engine.

One provider call per planned document. The engine validates the structured
response strictly (exact planned clause ids, correct document id, non-empty
heading and text), then builds a :class:`~contractforge.models.GeneratedPackage`
in *spec* order, never in model output order. Every call is recorded as an
:class:`~contractforge.models.LLMCallRecord` with sanitized request metadata so
scenario/document payloads do not leak into exported call logs.

A call is recorded as soon as the provider *answers*, before the answer is
parsed: output that fails validation was still paid for. When a document fails
validation, the error carries the attempt's calls
(:attr:`~contractforge.errors.GenerationError.calls`) so the pipeline can book
them instead of silently reporting zero tokens and zero cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import GenerationError
from ..models import (
    DocumentSpec,
    GeneratedClause,
    GeneratedDocument,
    GeneratedPackage,
    LLMCallRecord,
    Scenario,
)
from .parsing import extract_json_object
from .prompts import build_contract_request

if TYPE_CHECKING:
    from ..config import Settings
    from ..providers.base import Provider

_CALL_PURPOSE = "contract_document"


@dataclass
class GenerationOutcome:
    """Generated package plus the sanitized call records that produced it."""

    package: GeneratedPackage
    calls: list[LLMCallRecord]


class DocumentGenerator:
    """Generate a full document package from a scenario."""

    def __init__(self, provider: Provider, settings: Settings) -> None:
        self.provider = provider
        self.settings = settings

    def generate(
        self,
        scenario: Scenario,
        *,
        feedback: list[str] | None = None,
        attempt: int = 1,
    ) -> GenerationOutcome:
        calls: list[LLMCallRecord] = []
        documents: list[GeneratedDocument] = []

        for spec in scenario.documents:
            request = build_contract_request(
                scenario,
                spec,
                self.settings,
                attempt=attempt,
                feedback=feedback,
            )
            try:
                response = self.provider.complete(request)
            except GenerationError as exc:
                # No response was produced, but earlier documents in this
                # attempt were paid for: they stay bookable through the error.
                raise exc.with_calls(calls) from None
            # Book the call before parsing: a response that fails validation was
            # still paid for, and losing it would under-report tokens and cost.
            calls.append(
                LLMCallRecord(
                    call_id=_call_id(scenario, spec.document_id, attempt),
                    purpose=_CALL_PURPOSE,
                    contract_id=scenario.contract_id,
                    document_id=spec.document_id,
                    attempt=attempt,
                    request=request.model_copy(
                        update={"metadata": _sanitized_metadata(scenario, spec.document_id)}
                    ),
                    response_text=response.text,
                    usage=response.usage,
                    latency_ms=response.latency_ms,
                    finish_reason=response.finish_reason,
                    provider=response.provider,
                    model=response.model,
                    raw=response.raw,
                )
            )
            try:
                documents.append(_build_document(scenario, spec, response.text))
            except GenerationError as exc:
                raise exc.with_calls(calls) from None

        return GenerationOutcome(
            package=GeneratedPackage(contract_id=scenario.contract_id, documents=documents),
            calls=calls,
        )


def _call_id(scenario: Scenario, document_id: str, attempt: int) -> str:
    return f"{scenario.contract_id}_{document_id}_a{attempt}"


def _sanitized_metadata(scenario: Scenario, document_id: str) -> dict[str, Any]:
    return {
        "contract_id": scenario.contract_id,
        "document_id": document_id,
        "purpose": _CALL_PURPOSE,
        "difficulty": scenario.difficulty,
    }


def _build_document(
    scenario: Scenario, spec: DocumentSpec, response_text: str
) -> GeneratedDocument:
    payload = extract_json_object(response_text)

    returned_document_id = payload.get("document_id")
    if returned_document_id != spec.document_id:
        raise GenerationError(
            f"document {spec.document_id}: provider returned document_id {returned_document_id!r}"
        )

    raw_clauses = payload.get("clauses")
    if not isinstance(raw_clauses, list):
        raise GenerationError(f"document {spec.document_id}: 'clauses' must be a JSON array")

    by_id: dict[str, dict[str, Any]] = {}
    for item in raw_clauses:
        if not isinstance(item, dict):
            raise GenerationError(
                f"document {spec.document_id}: every clause must be a JSON object"
            )
        clause_id = item.get("clause_id")
        if not isinstance(clause_id, str) or not clause_id.strip():
            raise GenerationError(f"document {spec.document_id}: a clause is missing a clause_id")
        if clause_id in by_id:
            raise GenerationError(f"document {spec.document_id}: duplicate clause_id {clause_id!r}")
        by_id[clause_id] = item

    planned = [clause.clause_id for clause in spec.clauses]
    missing = [clause_id for clause_id in planned if clause_id not in by_id]
    unexpected = [clause_id for clause_id in by_id if clause_id not in planned]
    if missing or unexpected:
        raise GenerationError(
            f"document {spec.document_id}: clause ids do not match the plan "
            f"(missing={missing}, unexpected={unexpected})",
            details={
                "document_id": spec.document_id,
                "missing": missing,
                "unexpected": unexpected,
            },
        )

    clauses: list[GeneratedClause] = []
    for clause_spec in spec.clauses:
        item = by_id[clause_spec.clause_id]
        heading = item.get("heading")
        text = item.get("text")
        if not isinstance(heading, str) or not heading.strip():
            raise GenerationError(
                f"document {spec.document_id}: clause {clause_spec.clause_id!r} "
                "has an empty heading"
            )
        if not isinstance(text, str) or not text.strip():
            raise GenerationError(
                f"document {spec.document_id}: clause {clause_spec.clause_id!r} has empty text"
            )
        clauses.append(
            GeneratedClause(
                clause_id=clause_spec.clause_id,
                document_id=spec.document_id,
                heading=heading.strip(),
                text=text.strip(),
            )
        )

    return GeneratedDocument(
        document_id=spec.document_id,
        kind=spec.kind,
        title=spec.title(scenario.language),
        clauses=clauses,
        references=list(spec.references_documents),
    )
