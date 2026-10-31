"""Dataset statistics computed from loaded contracts.

Honesty rules:

* cost is only reported when every contract carries a known ``cost_usd``; a
  partially unknown dataset reports ``cost_usd=None`` and
  ``cost_basis="unknown"`` rather than a partial sum masquerading as the total.
* token fields sum whatever the contracts report and flag ``tokens_known`` when
  any contract says its usage is unknown.
* timing/throughput fields are ``None`` when there is no basis (no
  ``total_seconds``, no documents or no latency samples) instead of a made-up
  zero. The three rate fields are non-optional in :class:`DatasetStats`, so they
  stay ``0.0`` when there is no basis.
* ``mean_document_latency_ms`` is a *weighted* mean over all LLM calls
  (``sum(mean_latency_ms * calls) / sum(calls)``), never the mean of per-contract
  means: contracts issue very different numbers of calls.
"""

from __future__ import annotations

from ..models import Answerability, DatasetStats, DocumentStatus, LoadedContract


def _clause_count(contract: LoadedContract) -> int:
    if contract.rendered:
        return sum(
            len(rendered.clause_spans) for rendered in contract.rendered.values()
        )
    return len(contract.scenario.clause_ids)


def compute_stats(
    contracts: list[LoadedContract], *, total_seconds: float | None = None
) -> DatasetStats:
    """Compute :class:`DatasetStats` for a loaded dataset.

    ``verification_pass_rate`` and ``repair_rate`` are document-level
    (accepted / repaired documents over total documents); ``rejection_rate`` is
    contract-level (rejected contracts over total contracts), matching the
    pipeline's "a rejected contract is not part of the dataset" rule.

    A repaired contract is still an accepted contract: it was exported and its
    gold/questions are valid. ``contracts_accepted`` therefore counts both
    ``ACCEPTED`` and ``REPAIRED`` contract statuses.
    """

    loaded = list(contracts)
    stats = DatasetStats(total_seconds=total_seconds)
    stats.contracts_total = len(loaded)

    verifications = [contract.manifest.verification for contract in loaded]
    stats.contracts_accepted = sum(
        1
        for summary in verifications
        if summary.status in (DocumentStatus.ACCEPTED, DocumentStatus.REPAIRED)
    )
    stats.contracts_rejected = sum(
        1 for summary in verifications if summary.status == DocumentStatus.REJECTED
    )
    stats.documents_total = sum(summary.documents_total for summary in verifications)
    stats.documents_accepted = sum(summary.documents_accepted for summary in verifications)
    stats.documents_repaired = sum(summary.documents_repaired for summary in verifications)
    stats.documents_rejected = sum(summary.documents_rejected for summary in verifications)

    stats.clauses_total = sum(_clause_count(contract) for contract in loaded)
    stats.facts_total = sum(
        len(contract.gold.facts) for contract in loaded if contract.gold is not None
    )

    questions = [question for contract in loaded for question in contract.questions]
    stats.questions_total = len(questions)
    stats.questions_validated = sum(1 for question in questions if question.validated)
    stats.questions_answerable = sum(
        1 for question in questions if question.answerability == Answerability.ANSWERABLE
    )
    stats.questions_insufficient_evidence = sum(
        1
        for question in questions
        if question.answerability == Answerability.INSUFFICIENT_EVIDENCE
    )
    stats.questions_conditional = sum(
        1 for question in questions if question.answerability == Answerability.CONDITIONAL
    )
    stats.questions_conflicting_evidence = sum(
        1
        for question in questions
        if question.answerability == Answerability.CONFLICTING_EVIDENCE
    )

    costs = [contract.manifest.cost for contract in loaded]
    stats.calls_total = sum(cost.calls for cost in costs)
    stats.input_tokens = sum(cost.input_tokens for cost in costs)
    stats.output_tokens = sum(cost.output_tokens for cost in costs)
    stats.total_tokens = sum(cost.total_tokens for cost in costs)
    stats.tokens_known = all(cost.tokens_known for cost in costs)

    if loaded and all(cost.cost_usd is not None for cost in costs):
        stats.cost_usd = sum(cost.cost_usd for cost in costs if cost.cost_usd is not None)
        if all(cost.cost_basis == "configured_price_table" for cost in costs):
            stats.cost_basis = "configured_price_table"
        else:
            stats.cost_basis = "unknown"
    else:
        stats.cost_usd = None
        stats.cost_basis = "unknown"

    # Weighted mean over *all* calls that reported a latency: sum(mean * calls)
    # / sum(calls). Averaging the per-contract means instead would let a
    # two-call contract weigh as much as a twenty-call contract.
    measured = [cost for cost in costs if cost.mean_latency_ms is not None]
    measured_calls = sum(cost.calls for cost in measured)
    if measured_calls:
        stats.mean_document_latency_ms = (
            sum(cost.mean_latency_ms * cost.calls for cost in measured) / measured_calls
        )

    if stats.documents_total:
        stats.verification_pass_rate = stats.documents_accepted / stats.documents_total
        stats.repair_rate = stats.documents_repaired / stats.documents_total
    if stats.contracts_total:
        stats.rejection_rate = stats.contracts_rejected / stats.contracts_total

    if total_seconds is not None and total_seconds > 0:
        minutes = total_seconds / 60.0
        if stats.documents_total:
            stats.documents_per_minute = stats.documents_total / minutes
        if stats.clauses_total:
            stats.clauses_per_minute = stats.clauses_total / minutes

    if stats.cost_usd is not None:
        if stats.documents_accepted:
            stats.cost_per_accepted_document_usd = stats.cost_usd / stats.documents_accepted
        if stats.questions_validated:
            stats.cost_per_validated_question_usd = stats.cost_usd / stats.questions_validated

    return stats
