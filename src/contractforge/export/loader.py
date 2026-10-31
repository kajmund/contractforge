"""Read an exported dataset back from disk.

The loader is deliberately tolerant about *optional* files (gold, questions,
verification, rendered text, canonical ``.txt``) and records every gap in
``LoadedContract.load_errors`` instead of guessing a default. Mandatory files
(``scenario.json`` and ``manifest.json``) are different: a contract without them
cannot be represented honestly, so :class:`ExportError` is raised and
``validation.dataset`` can attribute the failure to that contract directory.

The loader never verifies hashes or evidence spans; that is
``validation.dataset``'s job. It only reads and validates the persisted shapes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ..errors import ExportError
from ..models import (
    DatasetManifest,
    GoldenQuestion,
    GoldSet,
    LoadedContract,
    Manifest,
    PackageVerification,
    RenderedDocument,
    Scenario,
)

SCENARIO_FILENAME = "scenario.json"
MANIFEST_FILENAME = "manifest.json"
RENDERED_FILENAME = "rendered.json"
GOLD_FILENAME = "gold_facts.json"
QUESTIONS_FILENAME = "golden_questions.json"
VERIFICATION_FILENAME = "verification.json"
DATASET_MANIFEST_FILENAME = "dataset_manifest.json"
REJECTED_DIRNAME = "rejected"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ExportError(f"could not read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ExportError(f"invalid JSON in {path}: {exc}") from exc


def _require_model(path: Path, model: type[BaseModel], label: str) -> Any:
    if not path.is_file():
        raise ExportError(f"missing required {label}: {path}")
    payload = _read_json(path)
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ExportError(f"invalid {label} in {path}: {exc}") from exc


def _optional_model(
    path: Path,
    model: type[BaseModel],
    label: str,
    load_errors: list[str],
) -> Any | None:
    if not path.is_file():
        load_errors.append(f"missing optional {label}: {path.name}")
        return None
    try:
        payload = _read_json(path)
    except ExportError as exc:
        load_errors.append(str(exc))
        return None
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        load_errors.append(f"invalid {label} in {path.name}: {exc}")
        return None


def _load_rendered(root: Path, load_errors: list[str]) -> dict[str, RenderedDocument]:
    path = root / RENDERED_FILENAME
    if not path.is_file():
        load_errors.append(f"missing optional {RENDERED_FILENAME}")
        return {}
    try:
        payload = _read_json(path)
    except ExportError as exc:
        load_errors.append(str(exc))
        return {}
    if not isinstance(payload, dict):
        load_errors.append(f"invalid {RENDERED_FILENAME}: expected an object per document")
        return {}
    rendered: dict[str, RenderedDocument] = {}
    for document_id, item in payload.items():
        try:
            rendered[str(document_id)] = RenderedDocument.model_validate(item)
        except ValidationError as exc:
            load_errors.append(f"invalid {RENDERED_FILENAME} entry {document_id!r}: {exc}")
    return rendered


def _load_questions(path: Path, load_errors: list[str]) -> list[GoldenQuestion]:
    if not path.is_file():
        load_errors.append(f"missing optional {QUESTIONS_FILENAME}")
        return []
    try:
        payload = _read_json(path)
    except ExportError as exc:
        load_errors.append(str(exc))
        return []
    if not isinstance(payload, list):
        load_errors.append(f"invalid {QUESTIONS_FILENAME}: expected a list")
        return []
    questions: list[GoldenQuestion] = []
    for index, item in enumerate(payload):
        try:
            questions.append(GoldenQuestion.model_validate(item))
        except ValidationError as exc:
            load_errors.append(f"invalid {QUESTIONS_FILENAME} entry #{index}: {exc}")
    return questions


def _document_ids(
    rendered: dict[str, RenderedDocument],
    manifest: Manifest,
    scenario: Scenario,
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for document_id in rendered:
        if document_id not in seen:
            seen.add(document_id)
            ordered.append(document_id)
    for record in manifest.documents:
        if record.document_id not in seen:
            seen.add(record.document_id)
            ordered.append(record.document_id)
    for spec in scenario.documents:
        if spec.document_id not in seen:
            seen.add(spec.document_id)
            ordered.append(spec.document_id)
    return ordered


def _text_paths(
    manifest: Manifest, scenario: Scenario, document_ids: list[str]
) -> dict[str, str]:
    """Map document id -> relative ``.txt`` path using the manifest, then the scenario."""

    paths: dict[str, str] = {}
    for record in manifest.documents:
        for artifact in record.files:
            if artifact.kind == "document" and artifact.path.lower().endswith(".txt"):
                paths[record.document_id] = artifact.path
                break

    specs = {spec.document_id: spec for spec in scenario.documents}
    for document_id in document_ids:
        if document_id in paths:
            continue
        spec = specs.get(document_id)
        if spec is not None:
            paths[document_id] = f"{spec.filename_stem}.txt"
        else:
            paths[document_id] = f"{document_id}.txt"
    return paths


def _load_texts(root: Path, loaded: LoadedContract) -> None:
    document_ids = _document_ids(loaded.rendered, loaded.manifest, loaded.scenario)
    paths = _text_paths(loaded.manifest, loaded.scenario, document_ids)
    for document_id in document_ids:
        relative = paths.get(document_id)
        if relative is None:  # pragma: no cover - _text_paths always fills an entry
            loaded.load_errors.append(f"missing canonical .txt for document {document_id!r}")
            continue
        path = root / relative
        if not path.is_file():
            loaded.load_errors.append(
                f"missing canonical .txt for document {document_id!r}: {relative}"
            )
            continue
        try:
            loaded.texts[document_id] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            loaded.load_errors.append(f"could not read canonical text {relative}: {exc}")


def find_contract_dirs(dataset_root: Path) -> list[Path]:
    """Return contract directories under ``dataset_root``, sorted by name.

    ``rejected/`` and hidden directories are not contracts.
    """

    root = Path(dataset_root)
    if not root.is_dir():
        raise ExportError(f"dataset directory not found: {root}")
    directories: list[Path] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name == REJECTED_DIRNAME:
            continue
        directories.append(child)
    return directories


def load_contract_dir(contract_dir: Path) -> LoadedContract:
    """Load one contract directory without verifying its contents."""

    root = Path(contract_dir)
    if not root.is_dir():
        raise ExportError(f"contract directory not found: {root}")

    scenario = _require_model(root / SCENARIO_FILENAME, Scenario, SCENARIO_FILENAME)
    manifest = _require_model(root / MANIFEST_FILENAME, Manifest, MANIFEST_FILENAME)

    load_errors: list[str] = []
    loaded = LoadedContract(
        root=root,
        scenario=scenario,
        manifest=manifest,
        rendered=_load_rendered(root, load_errors),
        load_errors=load_errors,
    )
    _load_texts(root, loaded)
    loaded.gold = _optional_model(root / GOLD_FILENAME, GoldSet, GOLD_FILENAME, load_errors)
    loaded.questions = _load_questions(root / QUESTIONS_FILENAME, load_errors)
    loaded.verification = _optional_model(
        root / VERIFICATION_FILENAME, PackageVerification, VERIFICATION_FILENAME, load_errors
    )
    return loaded


def load_dataset(dataset_root: Path) -> list[LoadedContract]:
    """Load every contract directory under ``dataset_root``.

    A contract missing its mandatory files raises :class:`ExportError`; callers
    that want per-contract reporting (``validation.dataset``) can iterate
    :func:`find_contract_dirs` and :func:`load_contract_dir` themselves.
    """

    return [load_contract_dir(directory) for directory in find_contract_dirs(dataset_root)]


def load_dataset_manifest(dataset_root: Path) -> DatasetManifest | None:
    """Read the root dataset manifest; ``None`` when it has not been written yet."""

    root = Path(dataset_root)
    if not root.is_dir():
        raise ExportError(f"dataset directory not found: {root}")
    path = root / DATASET_MANIFEST_FILENAME
    if not path.is_file():
        return None
    payload = _read_json(path)
    try:
        return DatasetManifest.model_validate(payload)
    except ValidationError as exc:
        raise ExportError(f"invalid {DATASET_MANIFEST_FILENAME} in {path}: {exc}") from exc
