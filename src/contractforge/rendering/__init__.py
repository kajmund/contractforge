"""Rendering subsystem: canonical text plus TXT/Markdown/DOCX/PDF artifacts."""

from __future__ import annotations

from .canonical import CANONICAL_ENCODING, SYNTHETIC_MARKER, render_document, render_package
from .formats import write_document

__all__ = [
    "CANONICAL_ENCODING",
    "SYNTHETIC_MARKER",
    "render_document",
    "render_package",
    "write_document",
]
