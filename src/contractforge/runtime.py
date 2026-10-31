"""Runtime identity recorded in manifests and benchmark reports.

A dataset must be traceable to the code, interpreter and dependencies that
produced it. The identity is best-effort but never guessed: fields that cannot
be determined are left empty/``None`` instead of being filled with a default.
"""

from __future__ import annotations

import importlib.metadata
import os
import platform
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import __version__
from .models import RuntimeIdentity

#: Runtime dependencies pinned in ``constraints.txt`` / reported per run.
TRACKED_DEPENDENCIES = ("pydantic", "httpx", "python-docx", "reportlab")

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _dependency_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in TRACKED_DEPENDENCIES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover - minimal envs
            continue
    return versions


def _git(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git binary
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


@lru_cache(maxsize=1)
def runtime_identity() -> RuntimeIdentity:
    """Return the identity of the current process (cached per process)."""

    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    dirty = None if status is None else bool(status)
    return RuntimeIdentity(
        contractforge_version=__version__,
        python_version=platform.python_version(),
        python_implementation=platform.python_implementation(),
        platform=platform.platform(),
        git_commit=commit,
        git_dirty=dirty,
        dependencies=_dependency_versions(),
    )


def reset_runtime_identity_cache() -> None:
    """Tests may switch repositories; drop the cached identity."""

    runtime_identity.cache_clear()


def environment_summary() -> dict[str, Any]:
    """A coarse, non-secret summary of the environment (for benchmark reports)."""

    return {
        "cpu_count": os.cpu_count(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
