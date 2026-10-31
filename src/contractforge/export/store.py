"""Export a contract result (or a whole dataset) to the on-disk layout.

The dataset layout is the contract between generation, gold, questions,
validation and the CLI; see ``docs/architecture.md``. Rules this module keeps:

* ``manifest.json`` is written **last** and hashes every other artifact.
* artifacts are sorted by relative path, so manifests diff cleanly.
* LLM calls are sanitized before they are written; raw provider responses go to
  ``raw/<call_id>.json`` only and never contain credentials.
* a rejected contract is never written as an accepted contract.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Literal

from .. import __version__
from ..errors import ExportError
from ..models import (
    SCHEMA_VERSION,
    ArtifactRecord,
    CheckStatus,
    ContractResult,
    DatasetContractRecord,
    DatasetManifest,
    DatasetStats,
    DocumentFormat,
    DocumentRecord,
    DocumentSpec,
    DocumentStatus,
    GenerationConfigSnapshot,
    LoadedContract,
    Manifest,
    RenderedDocument,
    RunRecord,
    VerificationSummary,
)
from ..rendering.formats import write_document
from ..sanitize import sanitize
from ..utils import atomic_write_json, sha256_file, sha256_text

MANIFEST_FILENAME = "manifest.json"
RENDERED_FILENAME = "rendered.json"
DATASET_MANIFEST_FILENAME = "dataset_manifest.json"
STATS_FILENAME = "stats.json"
REJECTED_DIRNAME = "rejected"
RAW_DIRNAME = "raw"

_SYNTHETIC_NOTES = [
    "All parties, organisationsnummer, addresses and amounts are synthetic.",
    "Synthetic test data – not legal advice and not reviewed by a lawyer.",
    "LLM-generated text is not guaranteed to be bit-identical across runs.",
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _validate_contract_id(contract_id: str) -> None:
    if (
        not contract_id
        or contract_id in {".", ".."}
        or "\\" in contract_id
        or Path(contract_id).name != contract_id
    ):
        raise ExportError(
            f"contract_id {contract_id!r} cannot be used as a dataset directory name"
        )


def _contract_dir(dataset_root: str | Path, contract_id: str) -> Path:
    _validate_contract_id(contract_id)
    return Path(dataset_root) / contract_id


def _prepare_contract_dir(contract_dir: Path) -> None:
    """Create an empty contract directory, replacing any previous export."""

    contract_dir.parent.mkdir(parents=True, exist_ok=True)
    if contract_dir.is_symlink():
        contract_dir.unlink()
    elif contract_dir.exists():
        if not contract_dir.is_dir():
            raise ExportError(f"contract path exists and is not a directory: {contract_dir}")
        shutil.rmtree(contract_dir)
    contract_dir.mkdir(parents=True, exist_ok=True)


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return cleaned or "call"


def _sanitized_call(call) -> dict[str, Any]:  # noqa: ANN001 - LLMCallRecord
    return sanitize(call.model_dump(mode="json", exclude={"raw"}))


ArtifactKind = Literal[
    "document",
    "scenario",
    "gold_facts",
    "golden_questions",
    "verification",
    "llm_calls",
    "raw_response",
    "manifest",
    "other",
]


def _artifact(path: Path, contract_dir: Path, kind: ArtifactKind) -> ArtifactRecord:
    return ArtifactRecord(
        path=path.relative_to(contract_dir).as_posix(),
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
        kind=kind,
    )


def _write_json_artifact(path: Path, payload: Any) -> Path:
    atomic_write_json(path, payload)
    return path


def _effective_formats(formats: list[DocumentFormat]) -> list[DocumentFormat]:
    """Requested formats, always including TXT.

    ``.txt`` is the canonical artifact every validator and evidence span is
    checked against, so it is never optional, regardless of the requested set.
    """

    try:
        requested = [DocumentFormat(fmt) for fmt in formats]
    except ValueError as exc:
        raise ExportError(f"unknown document format: {exc}") from exc
    ordered: list[DocumentFormat] = [DocumentFormat.TXT]
    for fmt in requested:
        if fmt not in ordered:
            ordered.append(fmt)
    return ordered


def _ordered_documents(
    result: ContractResult,
) -> list[tuple[DocumentSpec, RenderedDocument]]:
    ordered: list[tuple[DocumentSpec, RenderedDocument]] = []
    scenario_ids: set[str] = set()
    for spec in result.scenario.documents:
        scenario_ids.add(spec.document_id)
        rendered = result.rendered.get(spec.document_id)
        if rendered is None:
            raise ExportError(
                f"contract {result.contract_id}: scenario document "
                f"{spec.document_id!r} has no rendered document"
            )
        ordered.append((spec, rendered))
    unexpected = sorted(set(result.rendered) - scenario_ids)
    if unexpected:
        raise ExportError(
            f"contract {result.contract_id}: rendered documents without a scenario "
            f"document: {unexpected}"
        )
    return ordered


def _verification_summary(result: ContractResult) -> VerificationSummary:
    verification = result.verification
    checks = [check for document in verification.documents for check in document.checks]
    checks.extend(verification.checks)
    return VerificationSummary(
        status=verification.status,
        documents_total=len(verification.documents),
        documents_accepted=sum(
            1
            for document in verification.documents
            if document.status == DocumentStatus.ACCEPTED
        ),
        documents_repaired=sum(
            1
            for document in verification.documents
            if document.status == DocumentStatus.REPAIRED
        ),
        documents_rejected=sum(
            1
            for document in verification.documents
            if document.status == DocumentStatus.REJECTED
        ),
        checks_failed=sum(1 for check in checks if check.status == CheckStatus.FAIL),
        checks_warned=sum(1 for check in checks if check.status == CheckStatus.WARN),
    )


# ---------------------------------------------------------------------------
# Contract export
# ---------------------------------------------------------------------------


def export_contract(
    result: ContractResult,
    dataset_root: Path,
    *,
    formats: list[DocumentFormat],
) -> Path:
    """Write one contract result to ``dataset_root/<contract_id>/`` and return it."""

    if (
        result.status == DocumentStatus.REJECTED
        or result.verification.status == DocumentStatus.REJECTED
    ):
        raise ExportError(
            f"contract {result.contract_id} was rejected; use write_rejected_contract "
            "instead of exporting it as an accepted contract"
        )

    contracts = _ordered_documents(result)
    contract_dir = _contract_dir(dataset_root, result.contract_id)
    _prepare_contract_dir(contract_dir)

    quality = result.scenario.variation.document_quality
    effective_formats = _effective_formats(formats)
    artifacts: list[ArtifactRecord] = []
    document_records: list[DocumentRecord] = []

    for spec, rendered in contracts:
        document_artifacts: list[ArtifactRecord] = []
        for fmt in effective_formats:
            path = write_document(
                rendered,
                target_without_suffix=contract_dir / spec.filename_stem,
                fmt=fmt,
                quality=quality,
            )
            record = _artifact(path, contract_dir, "document")
            document_artifacts.append(record)
            artifacts.append(record)
        document_records.append(
            DocumentRecord(
                document_id=spec.document_id,
                kind=spec.kind,
                title=rendered.title,
                canonical_sha256=sha256_text(rendered.canonical_text),
                files=document_artifacts,
            )
        )

    rendered_payload = {
        rendered.document_id: rendered.model_dump(mode="json") for _, rendered in contracts
    }
    json_artifacts: list[tuple[Path, Any, ArtifactKind]] = [
        (contract_dir / RENDERED_FILENAME, rendered_payload, "other"),
        (
            contract_dir / "scenario.json",
            result.scenario.model_dump(mode="json"),
            "scenario",
        ),
        (contract_dir / "gold_facts.json", result.gold.model_dump(mode="json"), "gold_facts"),
        (
            contract_dir / "golden_questions.json",
            [question.model_dump(mode="json") for question in result.questions],
            "golden_questions",
        ),
        (
            contract_dir / "verification.json",
            result.verification.model_dump(mode="json"),
            "verification",
        ),
        (
            contract_dir / "llm_calls.json",
            [_sanitized_call(call) for call in result.llm_calls],
            "llm_calls",
        ),
    ]
    for path, payload, kind in json_artifacts:
        _write_json_artifact(path, payload)
        artifacts.append(_artifact(path, contract_dir, kind))

    for call in result.llm_calls:
        if not call.raw:
            continue
        raw_path = contract_dir / RAW_DIRNAME / f"{_safe_filename(call.call_id)}.json"
        _write_json_artifact(raw_path, sanitize(call.raw))
        artifacts.append(_artifact(raw_path, contract_dir, "raw_response"))

    # One manifest entry per file: a duplicated raw call id must not create
    # duplicate artifact records.
    unique_artifacts = {artifact.path: artifact for artifact in artifacts}
    artifacts = sorted(unique_artifacts.values(), key=lambda artifact: artifact.path)

    # The manifest must describe the formats actually written, not just the
    # formats requested at generation time.
    generation_config = result.config.model_copy(
        update={"formats": list(effective_formats)}
    )
    manifest = Manifest(
        contractforge_version=__version__,
        generated_at=result.created_at,
        synthetic=True,
        contract_id=result.contract_id,
        contract_type=result.scenario.contract_type,
        difficulty=result.scenario.difficulty,
        language=result.scenario.language,
        jurisdiction=result.scenario.jurisdiction,
        provider=result.config.provider,
        model=result.config.model,
        prompt_version=result.config.prompt_version,
        prompt_sha256=result.config.prompt_sha256,
        seed=result.scenario.seed,
        generation_config=generation_config,
        documents=document_records,
        artifacts=artifacts,
        verification=_verification_summary(result),
        cost=result.cost,
        timings=dict(result.timings),
        notes=list(_SYNTHETIC_NOTES),
    )
    # The manifest is written last: it describes every artifact above.
    _write_json_artifact(contract_dir / MANIFEST_FILENAME, manifest.model_dump(mode="json"))
    return contract_dir


def write_rejected_contract(result: ContractResult, dataset_root: Path) -> Path:
    """Write a rejected contract to ``rejected/<contract_id>.json``.

    Rejections keep their scenario and verification so failures stay auditable,
    but are written outside the contract directories and are marked
    ``REJECTED`` no matter what the in-memory status said.
    """

    _validate_contract_id(result.contract_id)
    rejected_dir = Path(dataset_root) / REJECTED_DIRNAME
    rejected_dir.mkdir(parents=True, exist_ok=True)
    path = rejected_dir / f"{result.contract_id}.json"
    # A rejected attempt still cost money and tokens: keep the sanitized calls so
    # cost and latency stay auditable. Raw provider bodies are scrubbed and
    # written next to the record, exactly like for accepted contracts.
    calls = [_sanitized_call(call) for call in result.llm_calls]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "synthetic": True,
        "contract_id": result.contract_id,
        "status": DocumentStatus.REJECTED.value,
        "source_status": result.status.value,
        "attempts": result.attempts,
        "created_at": result.created_at.isoformat(),
        "scenario": result.scenario.model_dump(mode="json"),
        "verification": result.verification.model_dump(mode="json"),
        "llm_calls": calls,
        "cost": sanitize(result.cost.model_dump(mode="json")),
        "config": sanitize(result.config.model_dump(mode="json")),
        "timings": result.timings,
        "notes": [
            "Rejected contract: kept for auditing and never exported as an accepted "
            "agreement or part of gold/questions.",
        ],
    }
    _write_json_artifact(path, payload)
    for call in result.llm_calls:
        if not call.raw:
            continue
        raw_path = rejected_dir / RAW_DIRNAME / f"{_safe_filename(call.call_id)}.json"
        _write_json_artifact(raw_path, sanitize(call.raw))
    return path


def write_dataset_manifest(
    dataset_root: Path,
    config: GenerationConfigSnapshot,
    contracts: list[LoadedContract],
    stats: DatasetStats,
    *,
    run: RunRecord | None = None,
) -> Path:
    """Write ``dataset_manifest.json`` and ``stats.json`` for a dataset root.

    ``run`` records what the run owns (prefix, contract ids and generated data
    files) so a later run can clean up exactly its own artifacts and never
    delete user files.
    """

    root = Path(dataset_root)
    root.mkdir(parents=True, exist_ok=True)

    records: list[DatasetContractRecord] = []
    for contract in sorted(contracts, key=lambda loaded: loaded.root.name):
        manifest_path = contract.root / MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise ExportError(
                f"contract {contract.contract_id} at {contract.root} has no "
                f"{MANIFEST_FILENAME}; cannot compute the dataset manifest hash"
            )
        records.append(
            DatasetContractRecord(
                contract_id=contract.contract_id,
                path=contract.root.name,
                status=contract.manifest.verification.status,
                manifest_sha256=sha256_file(manifest_path),
            )
        )

    rejected = sorted(
        path.stem
        for path in (root / REJECTED_DIRNAME).glob("*.json")
    ) if (root / REJECTED_DIRNAME).is_dir() else []
    manifest = DatasetManifest(
        contractforge_version=__version__,
        generated_at=stats.generated_at,
        synthetic=True,
        config=config,
        contracts=records,
        rejected=rejected,
        totals=stats,
        run=run,
        notes=list(_SYNTHETIC_NOTES),
    )
    manifest_path = _write_json_artifact(
        root / DATASET_MANIFEST_FILENAME, manifest.model_dump(mode="json")
    )
    _write_json_artifact(root / STATS_FILENAME, stats.model_dump(mode="json"))
    return manifest_path
