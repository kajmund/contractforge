"""Command line interface – CLI-first, automation-friendly.

All commands can emit a machine-readable JSON envelope with ``--json`` and
return stable exit codes (see :mod:`contractforge.errors`). Human progress and
logs go to stderr; data goes to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from .. import __version__
from ..benchmark import run_benchmark
from ..config import SUPPORTED_PROVIDERS, Settings, load_pricing_table, parse_formats
from ..errors import (
    ConfigError,
    ContractForgeError,
    DatasetNotFoundError,
    UsageError,
    ValidationError,
)
from ..export.bundle import export_jsonl, export_zip
from ..export.loader import load_dataset, load_dataset_manifest
from ..export.stats import compute_stats
from ..models import (
    ContractType,
    ExportFormat,
    Language,
    ScenarioRequest,
    utc_now,
)
from ..pipeline import generate_dataset, regenerate_questions
from ..scenarios.registry import load_registry
from ..utils import atomic_write_json
from ..validation.dataset import validate_dataset

EXIT_OK = 0


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that raises UsageError so failures stay machine-readable."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message, details={"command": self.prog})


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="contractforge",
        description=(
            "ContractForge – genererar syntetiska avtal, verifierat facit och "
            "golden-frågor."
        ),
    )
    parser.add_argument("--version", action="version", version=f"contractforge {__version__}")
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="maskinläsbar JSON på stdout",
    )
    sub = parser.add_subparsers(dest="command", metavar="kommando")

    def add_json_flag(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )

    def add_generation_options(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--type",
            dest="contract_type",
            required=True,
            choices=[contract.value for contract in ContractType],
            help="avtalstyp",
        )
        command_parser.add_argument("--count", type=_positive_int, default=1)
        command_parser.add_argument("--clauses", type=_positive_int, default=10)
        command_parser.add_argument(
            "--difficulty", type=int, choices=range(1, 7), default=1, metavar="1-6"
        )
        command_parser.add_argument("--language", choices=["sv", "en"], default="sv")
        command_parser.add_argument("--jurisdiction", default="SE")
        command_parser.add_argument(
            "--provider", choices=list(SUPPORTED_PROVIDERS), default=None,
            help="cerebras (standard) eller mock (offline)",
        )
        command_parser.add_argument("--model", default=None)
        command_parser.add_argument("--seed", type=int, default=None)
        command_parser.add_argument("--concurrency", type=_positive_int, default=None)
        command_parser.add_argument("--max-repairs", type=_non_negative_int, default=None)
        command_parser.add_argument("--temperature", type=float, default=None)
        command_parser.add_argument("--top-p", type=float, default=None)
        command_parser.add_argument("--max-tokens", type=_positive_int, default=None)
        command_parser.add_argument(
            "--formats",
            default=None,
            help="kommaseparerade dokumentformat: txt,markdown,docx,pdf",
        )
        command_parser.add_argument(
            "--pricing",
            default=None,
            help="fil med explicit PricingTable (JSON); utan den redovisas ingen kostnad",
        )
        command_parser.add_argument("--prefix", default=None, help="contract_id-prefix")
        command_parser.add_argument(
            "--package",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="tillåt dokumentpaket (bilagor/tillägg)",
        )
        command_parser.add_argument("--quiet", action="store_true")
        add_json_flag(command_parser)

    generate = sub.add_parser("generate", help="generera ett dataset")
    add_generation_options(generate)
    generate.add_argument("--output", required=True, help="datasetkatalog")

    benchmark = sub.add_parser("benchmark", help="reproducerbar benchmark")
    add_generation_options(benchmark)
    benchmark.add_argument(
        "--output", default="benchmarks/results", help="katalog för benchmarkrapporter"
    )
    benchmark.add_argument("--benchmark-id", default=None)

    validate = sub.add_parser("validate", help="verifiera ett exporterat dataset")
    validate.add_argument("dataset")
    validate.add_argument("--min-contracts", type=_non_negative_int, default=0)
    add_json_flag(validate)

    questions = sub.add_parser(
        "generate-questions", help="återgenerera golden-frågor för ett dataset"
    )
    questions.add_argument("dataset")
    add_json_flag(questions)

    export = sub.add_parser("export", help="exportera ett dataset")
    export.add_argument("dataset")
    export.add_argument(
        "--format", choices=[fmt.value for fmt in ExportFormat], default="zip"
    )
    export.add_argument("--output", default=None)
    add_json_flag(export)

    stats = sub.add_parser("stats", help="statistik för ett dataset")
    stats.add_argument("dataset")
    add_json_flag(stats)

    list_types = sub.add_parser("list-types", help="lista avtalstyper")
    add_json_flag(list_types)
    return parser


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"förväntade ett heltal, fick {raw!r}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("värdet måste vara >= 1")
    return value


def _non_negative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"förväntade ett heltal, fick {raw!r}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("värdet måste vara >= 0")
    return value


def _json_mode(args: argparse.Namespace | None) -> bool:
    return bool(getattr(args, "json", False))


def _log(message: str, *, quiet: bool = False) -> None:
    if not quiet:
        print(message, file=sys.stderr, flush=True)


def _emit(payload: dict, *, json_mode: bool) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _fmt_int(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,}".replace(",", " ")


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "okänd (ingen prisuppgift konfigurerad)"
    return f"{value:.4f} USD"


def _print_stats(stats, *, stream=sys.stdout) -> None:
    lines = [
        f"Avtal: {_fmt_int(stats.contracts_total)} "
        f"(godkända {_fmt_int(stats.contracts_accepted)}, "
        f"kasserade {_fmt_int(stats.contracts_rejected)})",
        f"Dokument: {_fmt_int(stats.documents_total)} "
        f"(reparerade {_fmt_int(stats.documents_repaired)}, "
        f"kasserade {_fmt_int(stats.documents_rejected)})",
        f"Klausuler: {_fmt_int(stats.clauses_total)}  Fakta: {_fmt_int(stats.facts_total)}",
        f"Frågor: {_fmt_int(stats.questions_total)} "
        f"(validerade {_fmt_int(stats.questions_validated)})",
        (
            f"Token: {_fmt_int(stats.total_tokens)} "
            f"(in {_fmt_int(stats.input_tokens)} / ut {_fmt_int(stats.output_tokens)})"
            if stats.tokens_known
            else "Token: okänd (providern rapporterade ingen usage – t.ex. mock)"
        ),
        (
            f"Kostnad: {_fmt_money(stats.cost_usd)}"
            if stats.cost_usd is not None
            else "Kostnad: okänd (ingen prisuppgift konfigurerad)"
        ),
    ]
    if stats.total_seconds is not None:
        lines.append(
            f"Tid: {stats.total_seconds:.1f} s, "
            f"dokument/min: {_fmt_money_metric(stats.documents_per_minute)}, "
            f"klausuler/min: {_fmt_money_metric(stats.clauses_per_minute)}"
        )
    lines.append(
        f"Verifiering: pass {stats.verification_pass_rate:.1%}, "
        f"reparation {stats.repair_rate:.1%}, kassation {stats.rejection_rate:.1%}"
    )
    print("\n".join(lines), file=stream)


def _fmt_money_metric(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    overrides: dict[str, object] = {}
    if args.provider:
        overrides["provider"] = args.provider
    if args.model:
        overrides["model"] = args.model
    if args.concurrency:
        overrides["concurrency"] = args.concurrency
    if args.max_repairs is not None:
        overrides["max_repair_attempts"] = args.max_repairs
    if args.temperature is not None:
        overrides["temperature"] = args.temperature
    if args.top_p is not None:
        overrides["top_p"] = args.top_p
    if args.max_tokens is not None:
        overrides["max_tokens"] = args.max_tokens
    if args.formats:
        overrides["formats"] = parse_formats(args.formats)
    if args.prefix:
        overrides["contract_id_prefix"] = args.prefix
    if args.pricing:
        overrides["pricing"] = load_pricing_table(args.pricing)
    settings = settings.with_overrides(**overrides)
    if settings.provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(
            f"okänd provider {settings.provider!r}; välj {', '.join(SUPPORTED_PROVIDERS)}"
        )
    return settings


def _scenario_request(args: argparse.Namespace, settings: Settings) -> ScenarioRequest:
    seed = args.seed if args.seed is not None else settings.seed
    return ScenarioRequest(
        contract_type=ContractType(args.contract_type),
        count=args.count,
        clauses=args.clauses,
        difficulty=args.difficulty,
        language=Language(args.language),
        jurisdiction=args.jurisdiction,
        seed=seed,
        contract_id_prefix=settings.contract_id_prefix,
        package=args.package,
    )


def _cmd_generate(args: argparse.Namespace) -> int:
    json_mode = _json_mode(args)
    settings = _settings_from_args(args)
    request = _scenario_request(args, settings)
    output = Path(args.output)
    outcome = generate_dataset(
        settings,
        request,
        output,
        progress=lambda message: _log(message, quiet=args.quiet),
    )
    manifest = load_dataset_manifest(outcome.dataset_root)
    payload = {
        "status": "ok",
        "command": "generate",
        "dataset": str(outcome.dataset_root),
        "stats": outcome.stats.model_dump(mode="json"),
        "config": manifest.config.model_dump(mode="json") if manifest else None,
        "rejected": [result.contract_id for result in outcome.rejected],
    }
    if json_mode:
        _emit(payload, json_mode=True)
    else:
        print(f"Dataset: {outcome.dataset_root}")
        _print_stats(outcome.stats)
        if outcome.rejected:
            print(f"Kasserade: {len(outcome.rejected)} (se rejected/ i datasetet)")
    return EXIT_OK


def _cmd_benchmark(args: argparse.Namespace) -> int:
    json_mode = _json_mode(args)
    settings = _settings_from_args(args)
    request = _scenario_request(args, settings)
    report = run_benchmark(
        settings,
        request,
        Path(args.output),
        benchmark_id=args.benchmark_id,
        progress=lambda message: _log(message, quiet=args.quiet),
    )
    payload = {
        "status": "ok",
        "command": "benchmark",
        "benchmark_id": report.benchmark_id,
        "report_path": str(Path(args.output) / f"{report.benchmark_id}.json"),
        "provider": report.provider,
        "model": report.model,
        "provider_is_mock": report.provider_is_mock,
        "stats": report.stats.model_dump(mode="json"),
        "notes": report.notes,
    }
    if json_mode:
        _emit(payload, json_mode=True)
    else:
        print(f"Benchmark: {report.benchmark_id} ({report.provider}/{report.model})")
        _print_stats(report.stats)
        print(f"Rapport: {Path(args.output) / f'{report.benchmark_id}.json'}")
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    root = Path(args.dataset)
    if not root.is_dir():
        raise DatasetNotFoundError(f"datasetkatalogen finns inte: {root}")
    report = validate_dataset(root, expect_min_contracts=args.min_contracts)
    payload = {
        "status": "ok" if report.status.value == "PASS" else "error",
        "command": "validate",
        "report": report.model_dump(mode="json"),
    }
    if _json_mode(args):
        _emit(payload, json_mode=True)
    else:
        failures = [check for check in report.checks if check.status.value == "FAIL"]
        print(
            f"Validerade {report.contracts_checked} avtal: "
            f"{report.contracts_passed} godkända, {report.contracts_failed} underkända"
        )
        for check in failures[:25]:
            location = check.document_id or check.details.get("contract_id", "")
            print(f"  FAIL {check.check_id} {location}: {check.message}")
        if len(failures) > 25:
            print(f"  … {len(failures) - 25} fler fel")
        print(f"Status: {report.status.value}")
    return EXIT_OK if report.status.value == "PASS" else 3


def _cmd_generate_questions(args: argparse.Namespace) -> int:
    root = Path(args.dataset)
    if not root.is_dir():
        raise DatasetNotFoundError(f"datasetkatalogen finns inte: {root}")
    stats = regenerate_questions(root)
    payload = {
        "status": "ok",
        "command": "generate-questions",
        "dataset": str(root),
        "stats": stats.model_dump(mode="json"),
    }
    if _json_mode(args):
        _emit(payload, json_mode=True)
    else:
        print(f"Golden-frågor uppdaterade för {stats.contracts_total} avtal")
        _print_stats(stats)
    return EXIT_OK


def _cmd_export(args: argparse.Namespace) -> int:
    root = Path(args.dataset)
    if not root.is_dir():
        raise DatasetNotFoundError(f"datasetkatalogen finns inte: {root}")
    export_format = ExportFormat(args.format)
    if args.output:
        output = Path(args.output)
    elif export_format == ExportFormat.ZIP:
        output = root.with_suffix(".zip")
    elif export_format == ExportFormat.JSONL:
        output = root.with_suffix(".jsonl")
    else:
        output = root.with_suffix(".json")

    if export_format == ExportFormat.ZIP:
        written = export_zip(root, output)
    elif export_format == ExportFormat.JSONL:
        written = export_jsonl(root, output)
    else:
        contracts = load_dataset(root)
        if not contracts:
            raise ValidationError(f"dataset saknar avtal: {root}")
        payload = {
            "schema_version": "1.0",
            "generated_at": utc_now().isoformat(),
            "contracts": [
                {
                    "scenario": contract.scenario.model_dump(mode="json"),
                    "gold": contract.gold.model_dump(mode="json") if contract.gold else None,
                    "questions": [
                        question.model_dump(mode="json") for question in contract.questions
                    ],
                    "manifest": contract.manifest.model_dump(mode="json"),
                }
                for contract in contracts
            ],
        }
        atomic_write_json(output, payload)
        written = output

    result = {
        "status": "ok",
        "command": "export",
        "format": export_format.value,
        "path": str(written),
    }
    if _json_mode(args):
        _emit(result, json_mode=True)
    else:
        print(f"Exporterade {export_format.value} -> {written}")
    return EXIT_OK


def _cmd_stats(args: argparse.Namespace) -> int:
    root = Path(args.dataset)
    if not root.is_dir():
        raise DatasetNotFoundError(f"datasetkatalogen finns inte: {root}")
    contracts = load_dataset(root)
    manifest = load_dataset_manifest(root)
    total_seconds = manifest.totals.total_seconds if manifest else None
    stats = compute_stats(contracts, total_seconds=total_seconds)
    if manifest is not None:
        stats.contracts_rejected = manifest.totals.contracts_rejected
        stats.contracts_total = stats.contracts_accepted + stats.contracts_rejected
        stats.rejection_rate = (
            stats.contracts_rejected / stats.contracts_total if stats.contracts_total else 0.0
        )
    payload = {
        "status": "ok",
        "command": "stats",
        "dataset": str(root),
        "stats": stats.model_dump(mode="json"),
    }
    if _json_mode(args):
        _emit(payload, json_mode=True)
    else:
        _print_stats(stats)
    return EXIT_OK


def _cmd_list_types(args: argparse.Namespace) -> int:
    registry = load_registry()
    types = registry.types()
    payload = {"status": "ok", "command": "list-types", "types": types, "count": len(types)}
    if _json_mode(args):
        _emit(payload, json_mode=True)
    else:
        for contract_type in types:
            print(contract_type)
    return EXIT_OK


def _dispatch(args: argparse.Namespace) -> int:
    handlers = {
        "generate": _cmd_generate,
        "benchmark": _cmd_benchmark,
        "validate": _cmd_validate,
        "generate-questions": _cmd_generate_questions,
        "export": _cmd_export,
        "stats": _cmd_stats,
        "list-types": _cmd_list_types,
    }
    if args.command not in handlers:
        raise UsageError("inget kommando angivet; kör 'contractforge --help'")
    return handlers[args.command](args)


def _emit_error(exc: ContractForgeError, args: argparse.Namespace | None) -> None:
    payload = {
        "status": "error",
        "command": getattr(args, "command", None),
        "error": exc.to_dict(),
    }
    if _json_mode(args):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Fel: {exc.message}", file=sys.stderr)
        if exc.details:
            print(f"Detaljer: {json.dumps(exc.details, ensure_ascii=False)}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args: argparse.Namespace | None = None
    try:
        args = parser.parse_args(argv)
        return _dispatch(args)
    except ContractForgeError as exc:
        _emit_error(exc, args)
        return exc.exit_code
    except KeyboardInterrupt:
        print("Avbruten.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - last-resort guard for automation
        traceback.print_exc()
        _emit_error(
            ContractForgeError(
                f"oväntat internt fel: {exc}", details={"exception": type(exc).__name__}
            ),
            args,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
