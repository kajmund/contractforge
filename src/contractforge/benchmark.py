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

    report = BenchmarkReport(
        benchmark_id=benchmark_id,
        provider=config.provider,
        model=config.model,
        provider_is_mock=config.provider == "mock",
        seed=request.seed,
        config=config,
        stats=stats,
        notes=notes,
    )
    payload = report.model_dump(mode="json")
    payload["contractforge_version"] = __version__
    payload["dataset_path"] = str(dataset_root)
    atomic_write_json(output_dir / f"{benchmark_id}.json", payload)
    return report
