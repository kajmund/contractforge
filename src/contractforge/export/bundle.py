"""Dataset-level bundling: deterministic ZIP archives and JSONL exports.

Both formats are written from the same loaded dataset and sorted by relative
path / contract directory name, so two exports of an unchanged tree are
byte-identical (ZIP entries get fixed timestamps).
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

from ..errors import ExportError
from ..utils import atomic_write_text, sha256_file
from .loader import (
    MANIFEST_FILENAME,
    find_contract_dirs,
    load_contract_dir,
    load_dataset_manifest,
)

_FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def _dataset_files(root: Path, skipped: set[Path]) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.resolve() in skipped:
            continue
        files.append(path)
    files.sort(key=lambda path: path.relative_to(root).as_posix())
    return files


def export_zip(dataset_root: Path, output_zip: Path) -> Path:
    """ZIP the whole dataset tree (including ``dataset_manifest.json``)."""

    root = Path(dataset_root)
    if not root.is_dir():
        raise ExportError(f"dataset directory not found: {root}")
    if load_dataset_manifest(root) is None:
        raise ExportError(
            "dataset_manifest.json is missing; write the dataset manifest before "
            "exporting a ZIP so the archive carries a versioned manifest"
        )

    output = Path(output_zip)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    skipped = {output.resolve(), temporary.resolve()}
    files = _dataset_files(root, skipped)

    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                relative = path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(relative, date_time=_FIXED_ZIP_DATE)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, path.read_bytes())
        os.replace(temporary, output)
    except OSError as exc:
        raise ExportError(f"could not write ZIP archive {output}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return output


def export_jsonl(dataset_root: Path, output_jsonl: Path) -> Path:
    """Write one JSON line per contract with scenario, gold, questions and manifest."""

    root = Path(dataset_root)
    if not root.is_dir():
        raise ExportError(f"dataset directory not found: {root}")

    dataset_manifest = load_dataset_manifest(root)
    dataset_records = (
        {record.contract_id: record for record in dataset_manifest.contracts}
        if dataset_manifest is not None
        else {}
    )

    lines: list[str] = []
    for contract_dir in find_contract_dirs(root):
        contract = load_contract_dir(contract_dir)
        dataset_record = dataset_records.get(contract.contract_id)
        manifest_path = contract_dir / MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise ExportError(f"contract {contract.contract_id} has no {MANIFEST_FILENAME}")
        line = {
            "contract_id": contract.contract_id,
            "path": contract_dir.name,
            "status": contract.manifest.verification.status.value,
            "dataset_status": dataset_record.status.value if dataset_record else None,
            "manifest_sha256": sha256_file(manifest_path),
            "scenario": contract.scenario.model_dump(mode="json"),
            "gold": contract.gold.model_dump(mode="json") if contract.gold is not None else None,
            "questions": [question.model_dump(mode="json") for question in contract.questions],
            "manifest": contract.manifest.model_dump(mode="json"),
        }
        lines.append(json.dumps(line, ensure_ascii=False))

    output = Path(output_jsonl)
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output, "".join(f"{line}\n" for line in lines))
    return output
