"""Canonical text -> TXT, Markdown, DOCX and PDF artifacts.

All four formats are derived from the same :class:`RenderedDocument`, so they can
never disagree about clause content. TXT is byte-identical to
``canonical_text``; the formatted variants (Markdown/DOCX/PDF) are derived from
the exact clause spans, never from a re-flowed or re-parsed rendering.

The optional ``quality`` variation only affects the *formatted* artifacts:
``line_breaks`` is already part of the canonical text (the canonical renderer
owns offsets), ``page_breaks`` adds a page break before attachment documents and
``tables`` renders the clauses as a simple terms table. None of them may change
canonical text, offsets or TXT output.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import docx
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ..models import (
    FORMAT_EXTENSIONS,
    DocKind,
    DocumentFormat,
    DocumentQuality,
    RenderedDocument,
)
from ..utils import atomic_write_bytes, atomic_write_text
from .canonical import CANONICAL_ENCODING

# Fixed timestamps keep DOCX/PDF artifacts byte-reproducible for the same input.
_FIXED_TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)
_FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def canonical_header_and_title(rendered: RenderedDocument) -> tuple[list[str], str]:
    """Split canonical text into metadata header lines and the document title.

    Uses the clause spans to find where the document body starts, so it works
    for canonical text produced by this renderer as well as for the minimal test
    renderer in ``tests/factories.py``.
    """

    text = rendered.canonical_text
    body_start = min(
        (span.start for span in rendered.clause_spans.values()), default=len(text)
    )
    prefix_lines = [line for line in text[:body_start].split("\n") if line.strip()]
    if not prefix_lines:
        return [], rendered.title
    if len(prefix_lines) == 1:
        return [], prefix_lines[0]
    return prefix_lines[:-1], prefix_lines[-1]


def clause_blocks(rendered: RenderedDocument) -> list[tuple[str, str]]:
    """Return clause blocks as ``(heading_line, text)`` derived from the spans."""

    blocks: list[tuple[str, str]] = []
    ordered = sorted(rendered.clause_spans.values(), key=lambda span: span.start)
    for span in ordered:
        chunk = span.slice(rendered.canonical_text)
        heading_line, _, body = chunk.partition("\n")
        blocks.append((heading_line, body.rstrip("\n")))
    return blocks


def _target_path(target_without_suffix: Path, fmt: DocumentFormat) -> Path:
    return Path(f"{target_without_suffix}{FORMAT_EXTENSIONS[fmt]}")


def _markdown(rendered: RenderedDocument) -> str:
    header, title = canonical_header_and_title(rendered)
    lines = [f"# {title}", ""]
    for line in header:
        lines.append(f"> {line}")
    if header:
        lines.append("")
    for heading_line, body in clause_blocks(rendered):
        lines.append(f"## {heading_line}")
        lines.append("")
        lines.append(body)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _freeze_zip_timestamps(payload: bytes) -> bytes:
    """Rewrite a DOCX zip with fixed entry timestamps (order preserved)."""

    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(payload)) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            frozen = zipfile.ZipInfo(info.filename, date_time=_FIXED_ZIP_DATE)
            frozen.compress_type = zipfile.ZIP_DEFLATED
            frozen.external_attr = info.external_attr
            target.writestr(frozen, source.read(info.filename))
    return output.getvalue()


def _docx_bytes(rendered: RenderedDocument, quality: DocumentQuality) -> bytes:
    document = docx.Document()
    core = document.core_properties
    core.author = "ContractForge"
    core.last_modified_by = "ContractForge"
    core.title = rendered.title
    core.subject = "Syntetisk testdata"
    core.comments = "Syntetisk testdata – ContractForge"
    core.created = _FIXED_TIMESTAMP
    core.modified = _FIXED_TIMESTAMP

    if quality == DocumentQuality.PAGE_BREAKS and rendered.kind != DocKind.MAIN_AGREEMENT:
        document.add_page_break()

    document.add_heading(rendered.title, level=1)
    header, _ = canonical_header_and_title(rendered)
    for line in header:
        paragraph = document.add_paragraph()
        paragraph.add_run(line).italic = True

    blocks = clause_blocks(rendered)
    if quality == DocumentQuality.TABLES:
        table = document.add_table(rows=1, cols=2)
        table.style = "Table Grid"
        cells = table.rows[0].cells
        cells[0].text = "Klausul"
        cells[1].text = "Innehåll"
        for heading_line, body in blocks:
            row = table.add_row().cells
            row[0].text = heading_line
            row[1].text = body
    else:
        for heading_line, body in blocks:
            document.add_heading(heading_line, level=2)
            document.add_paragraph(body)

    buffer = io.BytesIO()
    document.save(buffer)
    return _freeze_zip_timestamps(buffer.getvalue())


def _pdf_paragraph(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(text).replace("\n", "<br/>"), style)


def _pdf_bytes(rendered: RenderedDocument, quality: DocumentQuality) -> bytes:
    buffer = io.BytesIO()
    styles = getSampleStyleSheet()
    body_style = styles["BodyText"]
    meta_style = ParagraphStyle(
        "Meta",
        parent=body_style,
        fontSize=8,
        leading=10,
        textColor=colors.grey,
    )
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        invariant=1,
        title=rendered.title,
        author="ContractForge",
        subject="Syntetisk testdata",
        creator="ContractForge",
    )

    story: list[object] = []
    if quality == DocumentQuality.PAGE_BREAKS and rendered.kind != DocKind.MAIN_AGREEMENT:
        story.append(PageBreak())

    story.append(_pdf_paragraph(rendered.title, styles["Title"]))
    story.append(Spacer(1, 6))
    header, _ = canonical_header_and_title(rendered)
    for line in header:
        story.append(_pdf_paragraph(line, meta_style))
    story.append(Spacer(1, 10))

    blocks = clause_blocks(rendered)
    if quality == DocumentQuality.TABLES:
        data: list[list[object]] = [
            [
                _pdf_paragraph("Klausul", styles["Heading3"]),
                _pdf_paragraph("Innehåll", styles["Heading3"]),
            ]
        ]
        for heading_line, body in blocks:
            data.append(
                [_pdf_paragraph(heading_line, body_style), _pdf_paragraph(body, body_style)]
            )
        table = Table(data, colWidths=[55 * mm, 110 * mm], repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(table)
    else:
        for heading_line, body in blocks:
            story.append(_pdf_paragraph(heading_line, styles["Heading2"]))
            story.append(_pdf_paragraph(body, body_style))
            story.append(Spacer(1, 6))

    document.build(story)
    return buffer.getvalue()


def write_document(
    rendered: RenderedDocument,
    *,
    target_without_suffix: Path,
    fmt: DocumentFormat,
    quality: DocumentQuality | None = None,
) -> Path:
    """Write one rendered document in ``fmt`` and return the created path.

    ``target_without_suffix`` is a path without an extension; the caller chooses
    where the artifact lives (export uses the scenario filename stem). ``quality``
    is the scenario's document-quality variation; it may affect DOCX/PDF layout
    but never canonical text, TXT bytes or span offsets.
    """

    target = Path(target_without_suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    path = _target_path(target, fmt)
    effective_quality = quality if quality is not None else DocumentQuality.CLEAN

    if fmt == DocumentFormat.TXT:
        payload = rendered.canonical_text.encode(CANONICAL_ENCODING)
        atomic_write_bytes(path, payload)
    elif fmt == DocumentFormat.MARKDOWN:
        atomic_write_text(path, _markdown(rendered), encoding=CANONICAL_ENCODING)
    elif fmt == DocumentFormat.DOCX:
        atomic_write_bytes(path, _docx_bytes(rendered, effective_quality))
    elif fmt == DocumentFormat.PDF:
        atomic_write_bytes(path, _pdf_bytes(rendered, effective_quality))
    else:  # pragma: no cover - enum is exhaustive, this guards future members
        raise ValueError(f"unsupported document format {fmt!r}")
    return path
