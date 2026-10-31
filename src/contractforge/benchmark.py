"""Reproducible benchmark: generate a corpus, verify it and record the metrics.

The benchmark answers the questions in the specification: documents per minute,
clauses per minute, mean latency, token usage, actual provider cost (only when a
pricing table is configured), verification pass rate, repair rate, cost per
accepted document and cost per validated question.

A ``mock`` run is an offline pipeline benchmark, not a model-quality
measurement; the report records ``provider_is_mock`` so the two are never
confused.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import __version__
from .config import Settings
from .models import BenchmarkReport, GenerationConfigSnapshot, ScenarioRequest, utc_now
from .pipeline import generate_dataset
from .utils import atomic_write_json


def run_benchmark(
    settings: Settings,
    request: ScenarioRequest,
    output_dir: Path,
    *,
    benchmark_id: str | None = None,
    progress=None,
    force: bool = False,
) -> BenchmarkReport:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    benchmark_id = benchmark_id or f"bench_{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    dataset_root = output_dir / benchmark_id / "dataset"
    started = time.perf_counter()
    outcome = generate_dataset(
        settings,
        request,
        dataset_root,
        progress=progress or (lambda _: None),
        force=force,
    )
    elapsed = time.perf_counter() - started

    config = GenerationConfigSnapshot.model_validate(
        json.loads((dataset_root / "dataset_manifest.json").read_text(encoding="utf-8"))["config"]
    )
    notes = [
        "Benchmarken mäter pipeline, verifiering och export – inte språklig kvalitet.",
        "Kostnad redovisas endast om en explicit PricingTable konfigurerats "
        "(CONTRACTFORGE_PRICING).",
        "En seed reproducerar scenarier och konfiguration; LLM-text är inte garanterat "
        "bit-identisk.",
    ]
    if config.provider == "mock":
        notes.insert(
            0,
            "provider=mock: deterministisk offline-provider. Siffrorna säger inget om "
            "Cerebras-modellens kvalitet eller latens.",
        )

    # The benchmark owns total wall-clock time (including export and verification).
    stats = outcome.stats
    stats.total_seconds = elapsed
    stats.documents_per_minute = (
        stats.documents_total / (elapsed / 60.0) if stats.documents_total and elapsed > 0 else None
    )
    stats.clauses_per_minute = (
        stats.clauses_total / (elapsed / 60.0) if stats.clauses_total and elapsed > 0 else None
    )

    rejected_questions = list(getattr(outcome, "rejected_questions", []) or [])
    details = _benchmark_details(outcome, dataset_root, elapsed, rejected_questions)
    throughput = (
        outcome.throughput_breakdown()
        if hasattr(outcome, "throughput_breakdown")
        else {}
    )
    if throughput:
        details["throughput"] = throughput
        if throughput.get("documents_per_minute_excluding_llm") is not None:
            notes.append(
                "Genomströmning redovisas både totalt och exklusive LLM-väntan "
                "(details.throughput); en hastighetssiffra utan detta är inte jämförbar."
            )
    report = BenchmarkReport(
        benchmark_id=benchmark_id,
        provider=config.provider,
        model=config.model,
        provider_is_mock=config.provider == "mock",
        seed=request.seed,
        config=config,
        stats=stats,
        details=details,
        notes=notes,
    )
    payload = report.model_dump(mode="json")
    payload["contractforge_version"] = __version__
    payload["dataset_path"] = str(dataset_root)
    atomic_write_json(output_dir / f"{benchmark_id}.json", payload)
    return report


def _benchmark_details(
    outcome,
    dataset_root: Path,
    elapsed: float,
    rejected_questions: list,
) -> dict[str, object]:
    """Verifiability data: hashes, runtime, validation and question accounting.

    Without this a benchmark report is only a set of numbers: it cannot be tied
    to the corpus it describes nor re-verified later.
    """

    from .runtime import environment_summary, runtime_identity
    from .utils import read_json, sha256_file

    manifest_path = dataset_root / "dataset_manifest.json"
    manifest = read_json(manifest_path) if manifest_path.is_file() else {}
    contracts = manifest.get("contracts") or []
    validation = getattr(outcome, "validation_report", None)
    contract_hashes = {
        record.get("contract_id", ""): record.get("manifest_sha256", "")
        for record in contracts
        if isinstance(record, dict)
    }
    questions_accepted = sum(len(result.questions) for result in outcome.results)
    calls = [call for result in list(outcome.results) + list(outcome.rejected) for call in result.llm_calls]
    total_latency = sum(call.latency_ms for call in calls)
    identity = runtime_identity().model_dump(mode="json")
    identity["environment"] = environment_summary()
    return {
        "dataset_path": str(dataset_root),
        "dataset_manifest_sha256": sha256_file(manifest_path) if manifest_path.is_file() else None,
        "dataset_contract_count": len(contracts),
        "dataset_contracts_hashes": contract_hashes,
        "validation": (
            validation.model_dump(mode="json") if validation is not None else None
        ),
        "questions_accepted": questions_accepted,
        "questions_rejected": len(rejected_questions),
        "rejected_question_ids": [q.question_id for q in rejected_questions],
        "latency": {
            "basis": "weighted_per_call",
            "calls": len(calls),
            "total_latency_ms": total_latency,
            "mean_latency_ms": (total_latency / len(calls)) if calls else None,
        },
        "wall_clock_seconds": elapsed,
        "runtime": identity,
    }
