"""End-to-end pipeline: scenario -> generation -> rendering -> validation -> gold -> export.

This module is the only place that knows about every subsystem. It owns the
repair loop and the aggregate accounting (tokens, latency, cost, statuses).
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .errors import GenerationError, ProviderError, ScenarioError, UsageError, ValidationError
from .export.loader import load_dataset, load_dataset_manifest
from .export.stats import compute_stats
from .export.store import export_contract, write_dataset_manifest, write_rejected_contract
from .generation.engine import DocumentGenerator
from .models import (
    CheckResult,
    CheckStatus,
    ContractResult,
    CostSummary,
    DatasetStats,
    DatasetValidationReport,
    DocumentStatus,
    DocumentVerification,
    GeneratedPackage,
    GenerationConfigSnapshot,
    GoldenQuestion,
    GoldSet,
    LLMCallRecord,
    LoadedContract,
    Manifest,
    PackageVerification,
    RunRecord,
    Scenario,
    ScenarioDefinitionRecord,
    ScenarioRequest,
)
from .providers import create_provider
from .providers.base import Provider
from .questions import generate_and_verify_questions
from .rendering.canonical import render_package
from .runtime import runtime_identity
from .scenarios.generator import ScenarioGenerator
from .scenarios.registry import DEFAULT_DEFINITIONS_DIR, load_registry
from .utils import atomic_write_json, read_json, sha256_file
from .validation import validate_package
from .validation.dataset import validate_dataset

ProgressCallback = Callable[[str], None]


@dataclass
class RunOutcome:
    dataset_root: Path
    results: list[ContractResult] = field(default_factory=list)
    rejected: list[ContractResult] = field(default_factory=list)
    stats: DatasetStats = field(default_factory=DatasetStats)
    rejected_questions: list[GoldenQuestion] = field(default_factory=list)
    validation_report: DatasetValidationReport | None = None
    total_seconds: float = 0.0


def _noop(_: str) -> None:
    return None


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


def summarize_cost(calls: list[LLMCallRecord], settings: Settings) -> CostSummary:
    """Aggregate tokens/latency; price only from an explicit pricing table.

    A price is only reported when *every* call has known token usage. An unknown
    ``Usage()`` must never be reported as a real ``0.0`` cost: ``cost_usd`` stays
    ``None`` with ``cost_basis="unknown"`` until the usage is complete.
    """

    if not calls:
        return CostSummary()
    input_tokens = sum(call.usage.input_tokens or 0 for call in calls)
    output_tokens = sum(call.usage.output_tokens or 0 for call in calls)
    total_tokens = sum(call.usage.total_tokens or 0 for call in calls)
    tokens_known = all(
        call.usage.input_tokens is not None and call.usage.output_tokens is not None
        for call in calls
    )
    mean_latency = sum(call.latency_ms for call in calls) / len(calls)

    price = None
    if settings.pricing is not None:
        price = settings.pricing.price_for(calls[0].provider, calls[0].model)
    cost_usd: float | None = None
    cost_basis = "unknown"
    if price is not None and tokens_known:
        cost_usd = (
            input_tokens / 1_000_000.0 * price.input_usd_per_million
            + output_tokens / 1_000_000.0 * price.output_usd_per_million
        )
        cost_basis = "configured_price_table"
    return CostSummary(
        calls=len(calls),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        tokens_known=tokens_known,
        cost_usd=cost_usd,
        cost_basis=cost_basis,
        mean_latency_ms=mean_latency,
    )


def scenario_definition_records(
    registry_types: list[str] | None = None,
    definitions_dir: Path | None = None,
) -> list[ScenarioDefinitionRecord]:
    """SHA-256 of every definition file that shapes the generated scenarios."""

    directory = DEFAULT_DEFINITIONS_DIR if definitions_dir is None else Path(definitions_dir)
    wanted = set(registry_types) if registry_types is not None else None
    records: list[ScenarioDefinitionRecord] = []
    for path in sorted(directory.glob("*.json")):
        if wanted is not None and path.stem not in wanted:
            continue
        records.append(
            ScenarioDefinitionRecord(
                contract_type=path.stem,
                path=f"scenarios/definitions/{path.name}",
                sha256=sha256_file(path),
            )
        )
    return records


def build_config_snapshot(
    settings: Settings,
    request: ScenarioRequest,
    provider: Provider,
    prompt_version: str,
    prompt_sha256: str,
) -> GenerationConfigSnapshot:
    return GenerationConfigSnapshot(
        contract_type=request.contract_type,
        count=request.count,
        clauses=request.clauses,
        difficulty=request.difficulty,
        language=request.language,
        jurisdiction=request.jurisdiction,
        provider=provider.name,
        model=provider.model,
        temperature=settings.temperature,
        top_p=settings.top_p,
        max_tokens=settings.max_tokens,
        seed=request.seed,
        concurrency=settings.concurrency,
        max_repair_attempts=settings.max_repair_attempts,
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha256,
        formats=list(settings.formats),
        contract_id_prefix=request.contract_id_prefix,
        package=request.package,
        variation_overrides=dict(request.variation_overrides),
        scenario_definitions=scenario_definition_records([request.contract_type.value]),
        runtime=runtime_identity(),
    )


def _prompt_version() -> str:
    from .generation.prompts import PROMPT_VERSION

    return PROMPT_VERSION


def _prompt_sha256() -> str:
    from .generation.prompts import prompt_sha256

    return prompt_sha256()


def _feedback_lines(verification: PackageVerification, limit: int = 16) -> list[str]:
    lines: list[str] = []
    for document in verification.documents:
        for check in document.checks:
            if check.status == CheckStatus.FAIL:
                lines.append(f"{check.check_id} ({document.document_id}): {check.message}")
    for check in verification.checks:
        if check.status == CheckStatus.FAIL:
            lines.append(f"{check.check_id}: {check.message}")
    return lines[:limit]


def _rejected_verification(scenario: Scenario, reason: str) -> PackageVerification:
    verification = PackageVerification(
        contract_id=scenario.contract_id,
        status=DocumentStatus.REJECTED,
        documents=[
            DocumentVerification(
                document_id=document.document_id,
                status=DocumentStatus.REJECTED,
                checks=[
                    CheckResult(
                        check_id="generation_valid",
                        status=CheckStatus.FAIL,
                        message=reason,
                        document_id=document.document_id,
                    )
                ],
                reasons=[reason],
            )
            for document in scenario.documents
        ],
    )
    verification.recompute_stats()
    return verification


def _request_from_scenario(scenario: Scenario) -> ScenarioRequest:
    prefix = scenario.contract_id.rsplit("_", 1)[0] or "contract"
    return ScenarioRequest(
        contract_type=scenario.contract_type,
        count=1,
        clauses=max(1, sum(len(document.clauses) for document in scenario.documents)),
        difficulty=scenario.difficulty,
        language=scenario.language,
        jurisdiction=scenario.jurisdiction,
        seed=scenario.seed,
        contract_id_prefix=prefix,
        package=len(scenario.documents) > 1,
    )


# ---------------------------------------------------------------------------
# One contract
# ---------------------------------------------------------------------------


def run_contract(
    scenario: Scenario,
    provider: Provider,
    settings: Settings,
    *,
    progress: ProgressCallback = _noop,
) -> ContractResult:
    """Generate, verify and enrich one contract, repairing failed attempts.

    A contract is only accepted when the deterministic and semantic verifier
    accepts every document. Rejected contracts never receive gold/questions and
    are stored under ``rejected/`` by the exporter.
    """

    started = time.perf_counter()
    document_generator = DocumentGenerator(provider, settings)
    all_calls: list[LLMCallRecord] = []
    feedback: list[str] | None = None
    last_package: GeneratedPackage | None = None
    last_rendered = None
    last_verification: PackageVerification | None = None
    rejection_reason = ""
    attempt = 0
    accepted = False

    total_attempts = settings.max_repair_attempts + 1
    for attempt in range(1, total_attempts + 1):
        try:
            outcome = document_generator.generate(scenario, feedback=feedback, attempt=attempt)
        except (GenerationError, ProviderError) as exc:
            # A failed attempt may still have paid provider calls: book every one.
            all_calls.extend(getattr(exc, "calls", None) or [])
            rejection_reason = f"generering misslyckades (försök {attempt}): {exc.message}"
            feedback = [f"Föregående försök kasserades: {exc.message}"]
            progress(f"{scenario.contract_id}: försök {attempt} misslyckades: {exc.message}")
            continue

        all_calls.extend(outcome.calls)
        rendered = render_package(scenario, outcome.package)
        verification = validate_package(scenario, outcome.package, rendered)
        last_package = outcome.package
        last_rendered = rendered
        last_verification = verification

        if verification.status in (DocumentStatus.ACCEPTED, DocumentStatus.REPAIRED):
            accepted = True
            break

        feedback = _feedback_lines(verification)
        rejection_reason = (
            f"verifieringen underkände försök {attempt}"
            + (f": {feedback[0]}" if feedback else "")
        )
        progress(f"{scenario.contract_id}: försök {attempt} underkänt, reparerar")

    cost = summarize_cost(all_calls, settings)
    config = build_config_snapshot(
        settings,
        _request_from_scenario(scenario),
        provider,
        _prompt_version(),
        _prompt_sha256(),
    )

    if not accepted or last_package is None or last_rendered is None or last_verification is None:
        progress(f"{scenario.contract_id}: KASSERAT – {rejection_reason}")
        return ContractResult(
            contract_id=scenario.contract_id,
            scenario=scenario,
            package=GeneratedPackage(contract_id=scenario.contract_id),
            rendered={},
            verification=_rejected_verification(
                scenario, rejection_reason or "avtalet kunde inte genereras"
            ),
            gold=GoldSet(contract_id=scenario.contract_id),
            questions=[],
            llm_calls=all_calls,
            cost=cost,
            config=config,
            status=DocumentStatus.REJECTED,
            attempts=max(attempt, 1),
            timings={"total_seconds": time.perf_counter() - started},
        )

    if attempt > 1:
        for document in last_verification.documents:
            if document.status == DocumentStatus.ACCEPTED:
                document.status = DocumentStatus.REPAIRED
            document.attempts = attempt
        last_verification.attempts = attempt
        last_verification.recompute_stats()

    gold = build_gold_set(scenario, last_package, last_rendered)
    question_outcome = generate_and_verify_questions(scenario, gold, last_rendered)
    for fact in gold.facts:
        fact.question_ids = [
            question.question_id
            for question in question_outcome.accepted
            if fact.fact_id in question.required_facts
        ]
    if question_outcome.rejected:
        progress(
            f"{scenario.contract_id}: {len(question_outcome.rejected)} frågor kasserades "
            "av frågeverifieringen"
        )

    # Final package-level gate: gold and questions must not introduce a
    # verification failure the document checks cannot see (e.g. an evidence span
    # in the gold set that does not match the canonical text).
    if gold.contract_id != scenario.contract_id:  # pragma: no cover - defensive
        raise ValidationError(f"gold set {gold.contract_id} does not match {scenario.contract_id}")
    gold_verification = validate_package(
        scenario, last_package, last_rendered, gold=gold
    )
    if gold_verification.status == DocumentStatus.REJECTED:
        failures = "; ".join(check.message for check in gold_verification.failed_checks[:3])
        progress(f"{scenario.contract_id}: KASSERAT – gold/verifiering: {failures}")
        return ContractResult(
            contract_id=scenario.contract_id,
            scenario=scenario,
            package=last_package,
            rendered=last_rendered,
            verification=gold_verification,
            gold=gold,
            questions=[],
            rejected_questions=question_outcome.accepted + question_outcome.rejected,
            llm_calls=all_calls,
            cost=cost,
            config=config,
            status=DocumentStatus.REJECTED,
            attempts=attempt,
            timings={"total_seconds": time.perf_counter() - started},
        )

    return ContractResult(
        contract_id=scenario.contract_id,
        scenario=scenario,
        package=last_package,
        rendered=last_rendered,
        verification=last_verification,
        gold=gold,
        questions=question_outcome.accepted,
        rejected_questions=question_outcome.rejected,
        llm_calls=all_calls,
        cost=cost,
        config=config,
        status=last_verification.status,
        attempts=attempt,
        timings={"total_seconds": time.perf_counter() - started},
    )


def build_gold_set(scenario: Scenario, package: GeneratedPackage, rendered):
    # Imported lazily so scenario-only tooling does not need the full stack.
    from .gold import build_gold_set as _build_gold_set

    return _build_gold_set(scenario, package, rendered)


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


OUTPUT_MARKER_FILENAME = ".contractforge"
OUTPUT_MARKER_CONTENT = "ContractForge dataset directory\n"
MANAGED_DIR_PREFIX = "contract_"
MANAGED_DIR_NAMES = ("rejected",)
RUN_MARKER_FILENAME = "run.json"
MANAGED_FILENAMES = ("dataset_manifest.json", "stats.json", "validation_report.json")


def _has_manifest(output: Path) -> bool:
    return (output / "dataset_manifest.json").is_file()


def _output_ownership(output: Path) -> str:
    """Classify an output directory: ``absent``, ``empty``, ``managed`` or ``foreign``.

    A directory counts as a managed dataset only when it carries the
    ContractForge marker file or a ContractForge dataset manifest. Everything
    else is user data and is never deleted.
    """

    if not output.exists():
        return "absent"
    if not output.is_dir():
        raise UsageError(f"output finns men är inte en katalog: {output}")
    if (output / OUTPUT_MARKER_FILENAME).is_file() or _has_manifest(output):
        return "managed"
    if any(output.iterdir()):
        return "foreign"
    return "empty"


def _refusal(output: Path) -> UsageError:
    return UsageError(
        f"katalogen {output} är inte tom och är inte ett ContractForge-dataset. "
        "Använd en ny --output-katalog (rekommenderat) eller --force om du är säker "
        "på att den tidigare körningens data får ersättas. Ingenting raderades."
    )


def _prepare_dataset_output(
    output: Path, *, request: ScenarioRequest | None = None, force: bool = False
) -> None:
    """Prepare the output directory without ever deleting foreign files.

    * absent/empty directory -> use it;
    * ContractForge-managed directory -> remove only its own generated artifacts
      (contract directories, ``rejected/``, dataset manifest, stats and
      validation report), verified against the previous dataset manifest where
      possible; unknown files are left untouched;
    * anything else -> refuse with exit code 2 unless ``force`` is given. With
      ``--force`` the user explicitly accepted replacing the directory.
    """

    ownership = _output_ownership(output)
    if ownership == "foreign" and not force:
        raise _refusal(output)
    if ownership == "managed" and request is not None:
        # Verify the run identity before anything is removed.
        _refuse_changed_run_identity(output, request)
    if ownership in ("absent", "empty"):
        output.mkdir(parents=True, exist_ok=True)
        (output / OUTPUT_MARKER_FILENAME).write_text(OUTPUT_MARKER_CONTENT, encoding="utf-8")
        return

    previous = read_json(output / "dataset_manifest.json") if _has_manifest(output) else None
    owned: set[str] = set()
    if isinstance(previous, dict):
        run = previous.get("run")
        if isinstance(run, dict):
            for path in run.get("data_files") or []:
                if isinstance(path, str) and path:
                    owned.add(path)
        for record in previous.get("contracts") or []:
            path = record.get("path") if isinstance(record, dict) else None
            if isinstance(path, str) and path:
                owned.add(path)

    for entry in list(output.iterdir()):
        name = entry.name
        if name == OUTPUT_MARKER_FILENAME:
            continue
        if ownership == "managed":
            managed = (
                (entry.is_dir() and (name in MANAGED_DIR_NAMES or name in owned))
                or (entry.is_file() and (name in MANAGED_FILENAMES or name in owned))
                or name in owned
            )
        else:  # foreign + force: the user accepted replacing the directory
            managed = True
        if not managed:
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    (output / OUTPUT_MARKER_FILENAME).write_text(OUTPUT_MARKER_CONTENT, encoding="utf-8")


def _refuse_foreign_contract_dirs(output: Path, expected_ids: set[str]) -> None:
    """Fail if the output dir already holds contract directories from another run.

    A fresh run starts from an empty directory (``_prepare_dataset_output``
    wipes a managed dataset), so any contract-style directory present now was
    created outside this run: a different prefix or another tool entirely. It is
    never deleted or mixed in.
    """

    leftovers = sorted(
        entry.name
        for entry in output.iterdir()
        if entry.is_dir()
        and entry.name.startswith(MANAGED_DIR_PREFIX)
        and entry.name not in expected_ids
    )
    if leftovers:
        raise UsageError(
            f"katalogen {output} innehåller redan avtal från en annan körning "
            f"({', '.join(leftovers[:5])}{' …' if len(leftovers) > 5 else ''}). "
            "Använd en ny --output-katalog eller samma prefix/count som tidigare."
        )


def _previous_run(output: Path) -> RunRecord | None:
    manifest_path = output / "dataset_manifest.json"
    if not manifest_path.is_file():
        return None
    previous = read_json(manifest_path)
    run = previous.get("run") if isinstance(previous, dict) else None
    if not isinstance(run, dict):
        return None
    try:
        return RunRecord.model_validate(run)
    except ValueError:  # pragma: no cover - hand-edited manifest
        return None


def _refuse_changed_run_identity(output: Path, request: ScenarioRequest) -> None:
    """Refuse reusing a dataset directory with a different contract-id prefix.

    The prefix determines the contract directory names; switching it in the same
    directory would silently mix two naming schemes. Aborting (before touching
    anything, since a managed dataset is already cleaned) would be too late, so
    the check runs during preparation.
    """

    previous = _previous_run(output)
    if previous is None:
        return
    if previous.contract_id_prefix != request.contract_id_prefix:
        raise UsageError(
            f"katalogen {output} skapades med prefixet {previous.contract_id_prefix!r} "
            f"men körningen använder {request.contract_id_prefix!r}. Använd en ny "
            "--output-katalog för ett annat prefix."
        )


def _run_record(
    request: ScenarioRequest, contract_ids: set[str], data_files: list[str]
) -> RunRecord:
    """Describe which paths a run owns, so a later run can clean up safely."""

    return RunRecord(
        contract_id_prefix=request.contract_id_prefix,
        count=request.count,
        contract_ids=sorted(contract_ids),
        data_files=sorted(data_files),
    )


def generate_dataset(
    settings: Settings,
    request: ScenarioRequest,
    output: Path,
    *,
    progress: ProgressCallback = _noop,
    force: bool = False,
) -> RunOutcome:
    """Generate ``request.count`` contracts, verify them and export a dataset.

    The run is only reported as successful when the exported dataset passes the
    same validation that ``contractforge validate`` runs (see
    :func:`validate_dataset`). A dataset that fails validation raises
    :class:`ValidationError`, so automation can never mistake an inconsistent
    export for a good one.
    """

    output = Path(output)
    _prepare_dataset_output(output, request=request, force=force)
    provider = create_provider(settings)
    try:
        return _generate_dataset_inner(
            settings, request, output, provider=provider, progress=progress
        )
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()


def _generate_dataset_inner(
    settings: Settings,
    request: ScenarioRequest,
    output: Path,
    *,
    provider: Provider,
    progress: ProgressCallback,
) -> RunOutcome:
    registry = load_registry()
    scenario_generator = ScenarioGenerator(registry)

    started = time.perf_counter()
    scenarios: list[Scenario] = []
    for index in range(request.count):
        try:
            scenarios.append(scenario_generator.generate(request, index))
        except ValueError as exc:  # pydantic scenario validation
            raise ScenarioError(f"kunde inte generera scenario {index + 1}: {exc}") from exc
    expected_ids = {scenario.contract_id for scenario in scenarios}
    _refuse_foreign_contract_dirs(output, expected_ids)
    progress(f"genererade {len(scenarios)} scenarier")

    results: list[ContractResult] = []
    rejected: list[ContractResult] = []

    def _run(scenario: Scenario) -> ContractResult:
        return run_contract(scenario, provider, settings, progress=progress)

    if settings.concurrency > 1 and len(scenarios) > 1:
        workers = min(settings.concurrency, len(scenarios))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_run, scenario): scenario for scenario in scenarios}
            for future in as_completed(futures):
                result = future.result()
                (rejected if result.status == DocumentStatus.REJECTED else results).append(result)
    else:
        for scenario in scenarios:
            result = _run(scenario)
            (rejected if result.status == DocumentStatus.REJECTED else results).append(result)

    results.sort(key=lambda result: result.contract_id)
    rejected.sort(key=lambda result: result.contract_id)

    for result in results:
        export_contract(result, output, formats=list(settings.formats))
    for result in rejected:
        write_rejected_contract(result, output)

    total_seconds = time.perf_counter() - started
    contracts = load_dataset(output)
    stats = compute_stats(contracts, total_seconds=total_seconds)
    stats = _adjust_rejections(
        stats,
        rejected,
        total=len(scenarios),
        rejected_questions=[question for result in results for question in result.rejected_questions],
    )
    config = build_config_snapshot(
        settings, request, provider, _prompt_version(), _prompt_sha256()
    )
    data_files = _data_file_paths(output)
    atomic_write_json(output / RUN_MARKER_FILENAME, _run_record(request, expected_ids, data_files).model_dump(mode="json"))
    write_dataset_manifest(output, config, contracts, stats, run=_run_record(request, expected_ids, data_files))

    report = validate_dataset(output, expect_min_contracts=len(scenarios))
    atomic_write_json(output / "validation_report.json", report.model_dump(mode="json"))
    if report.status != CheckStatus.PASS:
        failures = [
            f"{check.check_id}: {check.message}"
            for check in report.checks
            if check.status == CheckStatus.FAIL
        ]
        rejection_reasons: dict[str, list[str]] = {}
        for result in rejected:
            reasons = [check.message for check in result.verification.failed_checks]
            if not reasons:
                reasons = [document.reasons[0] for document in result.verification.documents
                           for _ in document.reasons[:1]] or ["okänd kasseringsorsak"]
            rejection_reasons[result.contract_id] = reasons[:3]
        reason_text = ""
        if rejection_reasons:
            first_id, first_reasons = next(iter(rejection_reasons.items()))
            reason_text = f". Kasserade avtal: {len(rejected)} (första: {first_id}: {first_reasons[0]})"
        raise ValidationError(
            "det exporterade datasetet klarade inte valideringen: "
            + "; ".join(failures[:5])
            + reason_text,
            details={
                "dataset": str(output),
                "contracts_checked": report.contracts_checked,
                "contracts_accepted": stats.contracts_accepted,
                "contracts_rejected": stats.contracts_rejected,
                "failures": len(failures),
                "rejection_reasons": rejection_reasons,
            },
        )

    progress(
        f"klart: {stats.contracts_accepted} godkända, {stats.contracts_rejected} kasserade "
        f"på {total_seconds:.1f}s (datasetvalidering: PASS, {report.contracts_checked} avtal)"
    )
    return RunOutcome(
        dataset_root=output,
        results=results,
        rejected=rejected,
        stats=stats,
        rejected_questions=[question for result in results for question in result.rejected_questions],
        validation_report=report,
        total_seconds=total_seconds,
    )


def _data_file_paths(output: Path) -> list[str]:
    """Relative paths of the files a run generated (contract dirs + rejected/)."""

    paths: list[str] = []
    for entry in sorted(output.iterdir()):
        if entry.is_dir() and (entry.name.startswith(MANAGED_DIR_PREFIX) or entry.name == "rejected"):
            for path in sorted(entry.rglob("*")):
                if path.is_file():
                    paths.append(path.relative_to(output).as_posix())
    return paths


def _adjust_rejections(
    stats: DatasetStats,
    rejected: list[ContractResult],
    total: int,
    *,
    rejected_questions: list[GoldenQuestion] | None = None,
) -> DatasetStats:
    if rejected_questions is not None:
        stats.questions_rejected = len(rejected_questions)
    stats.contracts_rejected = len(rejected)
    stats.contracts_total = stats.contracts_accepted + len(rejected)
    stats.documents_rejected = sum(len(result.scenario.documents) for result in rejected)
    denom = max(1, total)
    stats.rejection_rate = len(rejected) / denom
    if stats.cost_usd is not None and stats.documents_accepted:
        stats.cost_per_accepted_document_usd = stats.cost_usd / stats.documents_accepted
    if stats.cost_usd is not None and stats.questions_validated:
        stats.cost_per_validated_question_usd = stats.cost_usd / stats.questions_validated
    return stats


# ---------------------------------------------------------------------------
# Question regeneration (deterministic, no provider call)
# ---------------------------------------------------------------------------


def regenerate_questions(dataset_root: Path) -> DatasetStats:
    """Regenerate golden questions from the stored gold + canonical text.

    The scenario and canonical texts are authoritative. Questions are rebuilt
    deterministically and verified; affected hashes and the dataset manifest are
    refreshed. No provider call is made.
    """

    dataset_root = Path(dataset_root)
    contracts = load_dataset(dataset_root)
    if not contracts:
        raise ValidationError(f"dataset saknar avtal: {dataset_root}")

    for contract in contracts:
        if contract.gold is None or not contract.rendered:
            continue
        outcome = generate_and_verify_questions(contract.scenario, contract.gold, contract.rendered)
        for fact in contract.gold.facts:
            fact.question_ids = [
                question.question_id
                for question in outcome.accepted
                if fact.fact_id in question.required_facts
            ]
        _write_questions(contract, outcome.accepted)

    refreshed = load_dataset(dataset_root)
    previous = load_dataset_manifest(dataset_root)
    total_seconds = previous.totals.total_seconds if previous else None
    stats = compute_stats(refreshed, total_seconds=total_seconds)
    if previous is not None:
        stats.contracts_rejected = previous.totals.contracts_rejected
        stats.contracts_total = stats.contracts_accepted + previous.totals.contracts_rejected
        stats.rejection_rate = (
            stats.contracts_rejected / stats.contracts_total if stats.contracts_total else 0.0
        )
        write_dataset_manifest(dataset_root, previous.config, refreshed, stats)
    return stats


def _write_questions(contract: LoadedContract, questions: list[GoldenQuestion]) -> None:
    contract_dir = Path(contract.root)
    atomic_write_json(
        contract_dir / "golden_questions.json",
        [question.model_dump(mode="json") for question in questions],
    )
    gold_path = contract_dir / "gold_facts.json"
    if contract.gold is not None and gold_path.exists():
        atomic_write_json(gold_path, contract.gold.model_dump(mode="json"))
    _refresh_contract_manifest(contract_dir)


def _refresh_contract_manifest(contract_dir: Path) -> None:
    manifest_path = contract_dir / "manifest.json"
    if not manifest_path.exists():
        raise ValidationError(f"manifest saknas i {contract_dir}")
    manifest = Manifest.model_validate(read_json(manifest_path))
    for artifact in manifest.artifacts:
        path = contract_dir / artifact.path
        if path.exists():
            artifact.sha256 = sha256_file(path)
            artifact.size_bytes = path.stat().st_size
    atomic_write_json(manifest_path, manifest.model_dump(mode="json"))
