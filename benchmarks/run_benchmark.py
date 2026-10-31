#!/usr/bin/env python3
"""Reproducerbar benchmark för ContractForge.

Kör hela kedjan (scenario → generering → rendering → verifiering → facit →
frågor → export) och skriver en JSON-rapport med prestanda- och kvalitetsmätetal.

Standard är den deterministiska mock-providern: den mäter pipeline, verifiering
och export – inte språklig kvalitet. Använd ``--provider cerebras`` för en
live-mätning (kräver CEREBRAS_API_KEY).

Exempel:
    python benchmarks/run_benchmark.py --count 100 --seed 1234
    python benchmarks/run_benchmark.py --provider cerebras --count 20 \
        --pricing pricing.json --output benchmarks/results
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from contractforge.benchmark import run_benchmark
from contractforge.config import Settings, load_pricing_table
from contractforge.models import ContractType, Language, ScenarioRequest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--type", dest="contract_type", default="consulting",
        choices=[contract.value for contract in ContractType],
    )
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--clauses", type=int, default=10)
    parser.add_argument("--difficulty", type=int, choices=range(1, 7), default=3)
    parser.add_argument("--language", choices=["sv", "en"], default="sv")
    parser.add_argument("--provider", choices=["mock", "cerebras"], default="mock")
    parser.add_argument("--model", default=None)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--pricing", default=None)
    parser.add_argument("--output", default="benchmarks/results")
    parser.add_argument("--benchmark-id", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides: dict[str, object] = {"provider": args.provider}
    if args.model:
        overrides["model"] = args.model
    if args.concurrency:
        overrides["concurrency"] = args.concurrency
    if args.pricing:
        overrides["pricing"] = load_pricing_table(args.pricing)
    settings = Settings.from_env().with_overrides(**overrides)
    request = ScenarioRequest(
        contract_type=ContractType(args.contract_type),
        count=args.count,
        clauses=args.clauses,
        difficulty=args.difficulty,
        language=Language(args.language),
        seed=args.seed,
    )
    print(
        f"Benchmark: {args.provider}/{settings.model} – {args.count} avtal, "
        f"nivå {args.difficulty}, {args.language}",
        file=sys.stderr,
    )
    report = run_benchmark(
        settings,
        request,
        Path(args.output),
        benchmark_id=args.benchmark_id,
        progress=lambda message: print(message, file=sys.stderr),
    )
    path = Path(args.output) / f"{report.benchmark_id}.json"
    print(f"\nRapport: {path}")
    stats = report.stats
    print(
        f"Dokument: {stats.documents_total}  "
        f"Godkända avtal: {stats.contracts_accepted}/{stats.contracts_total}  "
        f"Reparerade dokument: {stats.documents_repaired}  "
        f"Kasserade avtal: {stats.contracts_rejected}"
    )
    print(
        f"Frågor: {stats.questions_total} (validerade {stats.questions_validated})  "
        f"Token: {stats.total_tokens}"
    )
    cost = "okänd" if stats.cost_usd is None else f"{stats.cost_usd:.4f} USD"
    print(
        f"Tid: {stats.total_seconds:.1f}s  "
        f"Dokument/min: {_fmt(stats.documents_per_minute)}  "
        f"Klausuler/min: {_fmt(stats.clauses_per_minute)}  Kostnad: {cost}"
    )
    print(
        f"Pass: {stats.verification_pass_rate:.1%}  "
        f"Reparation: {stats.repair_rate:.1%}  "
        f"Kassation: {stats.rejection_rate:.1%}"
    )
    if report.provider_is_mock:
        print("\nOBS: provider=mock – siffrorna mäter pipeline, inte LLM-kvalitet.")
    return 0


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


if __name__ == "__main__":
    raise SystemExit(main())
