"""Small shared utilities: hashing, atomic JSON IO, whitespace-stable search.

Search is deliberately explicit about mapping normalized text offsets back to
canonical text offsets, because every evidence span in the gold data must be
verifiable against the exported canonical document text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .errors import ExportError

_WHITESPACE_RE = re.compile(r"\s+")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_for_search(text: str) -> tuple[str, list[int]]:
    """Normalize text for tolerant matching and keep an offset map.

    Case is folded with ``lower()`` (stable for Swedish), whitespace runs are
    collapsed to a single space and stripped at both ends. ``offsets[i]`` is the
    index in ``text`` of normalized character ``i``. The returned mapping lets
    callers translate a normalized match back into an exact canonical span.
    """

    normalized_chars: list[str] = []
    offsets: list[int] = []
    last_was_space = True
    for index, char in enumerate(text):
        if char.isspace():
            if last_was_space:
                continue
            normalized_chars.append(" ")
            offsets.append(index)
            last_was_space = True
        else:
            normalized_chars.append(char.lower())
            offsets.append(index)
            last_was_space = False
    while normalized_chars and normalized_chars[-1] == " ":
        normalized_chars.pop()
        offsets.pop()
    return "".join(normalized_chars), offsets


def find_span(text: str, needle: str, start: int = 0, end: int | None = None) -> tuple[int, int] | None:
    """Find ``needle`` in ``text`` tolerating case and whitespace differences.

    Returns an exact ``(start, end)`` character span in ``text`` or ``None``.
    ``start``/``end`` bound the search to a region (e.g. one clause span).
    """

    if not needle:
        return None
    normalized_needle = _WHITESPACE_RE.sub(" ", needle.strip()).lower()
    if not normalized_needle:
        return None
    region_end = len(text) if end is None else end
    region = text[start:region_end]
    normalized_region, offsets = normalize_for_search(region)
    position = normalized_region.find(normalized_needle)
    if position < 0:
        return None
    first = offsets[position]
    last_index = position + len(normalized_needle) - 1
    # The last normalized character may be a collapsed space; clamp to region.
    last = offsets[min(last_index, len(offsets) - 1)]
    span_end = last + 1
    if span_end > len(region):
        span_end = len(region)
    return start + first, start + span_end


def contains_span_text(text: str, start: int, end: int, needle: str) -> bool:
    if not 0 <= start < end <= len(text):
        return False
    target = _WHITESPACE_RE.sub(" ", needle.strip()).lower()
    candidate = _WHITESPACE_RE.sub(" ", text[start:end].strip()).lower()
    return bool(target) and target in candidate


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> None:
    _atomic_write(path, text.encode(encoding))


def atomic_write_json(path: str | Path, payload: Any, indent: int = 2) -> None:
    _atomic_write(path, (json.dumps(payload, ensure_ascii=False, indent=indent) + "\n").encode("utf-8"))


def atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    _atomic_write(path, payload)


def _atomic_write(path: str | Path, payload: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=f".{target.name}.", delete=False
        ) as handle:
            handle.write(payload)
            temp_path = Path(handle.name)
        os.replace(temp_path, target)
    except OSError as exc:
        raise ExportError(f"could not write {target}: {exc}") from exc


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
    return slug.strip("_")
