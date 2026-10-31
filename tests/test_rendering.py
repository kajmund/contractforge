"""Tests for canonical rendering and document format writers."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import docx
import pytest
from factories import make_consulting_scenario, make_generated_package

from contractforge.errors import GenerationError
from contractforge.models import (
    FORMAT_EXTENSIONS,
    ClauseSpec,
    DocKind,
    DocumentFormat,
    DocumentQuality,
    DocumentSpec,
    GeneratedClause,
    GeneratedDocument,
    GeneratedPackage,
    VariationProfile,
)
from contractforge.rendering.canonical import (
    CANONICAL_ENCODING,
    SYNTHETIC_MARKER,
    render_document,
    render_package,
)
from contractforge.rendering.formats import write_document


def _default_render():
    scenario = make_consulting_scenario()
    package = make_generated_package(scenario)
    rendered = render_package(scenario, package)
    return scenario, package, rendered


def _rendered_with_quality(quality: DocumentQuality):
    scenario = make_consulting_scenario()
    scenario.variation = VariationProfile(
        language=scenario.language, document_quality=quality
    )
    package = make_generated_package(scenario)
    return scenario, package, render_package(scenario, package)["main_agreement"]


def _assert_spans_exact(rendered, generated: GeneratedDocument) -> None:
    text = rendered.canonical_text
    assert rendered.validate_spans() == []
    assert list(rendered.clause_spans) == [clause.clause_id for clause in generated.clauses]
    ordered = [rendered.clause_spans[clause.clause_id] for clause in generated.clauses]
    for previous, current in pairwise(ordered):
        assert previous.end <= current.start
    for index, clause in enumerate(generated.clauses, start=1):
        span = rendered.clause_spans[clause.clause_id]
        assert 0 <= span.start < span.end <= len(text)
        chunk = span.slice(text)
        assert chunk == f"{index}. {clause.heading}\n{clause.text}"
        assert clause.text in chunk
        assert clause.heading in chunk


# ---------------------------------------------------------------------------
# Canonical text
# ---------------------------------------------------------------------------


def test_canonical_text_has_metadata_header_title_and_numbered_clauses() -> None:
    _, _, rendered = _default_render()
    document = rendered["main_agreement"]
    lines = document.canonical_text.split("\n")

    assert lines[0] == SYNTHETIC_MARKER
    assert "contract_id: contract_0001" in lines
    assert "document_id: main_agreement" in lines
    assert "dokumenttyp: main_agreement" in lines
    assert "språk: sv" in lines
    assert "jurisdiktion: SE" in lines
    # blank line, title, blank line, then the first numbered clause
    assert lines[7] == "Konsultavtal"
    assert lines[8] == ""
    assert lines[9] == "1. Avtalsparter"
    assert document.title == "Konsultavtal"
    assert document.document_id == "main_agreement"
    assert document.kind == DocKind.MAIN_AGREEMENT


def test_canonical_text_is_utf8_and_round_trips() -> None:
    _, _, rendered = _default_render()
    text = rendered["main_agreement"].canonical_text
    assert CANONICAL_ENCODING == "utf-8"
    encoded = text.encode(CANONICAL_ENCODING)
    assert encoded.decode(CANONICAL_ENCODING) == text
    assert text.encode(CANONICAL_ENCODING) == text.encode("utf-8")
    assert "–" in text  # en dash in the synthetic marker


def test_render_package_renders_every_document_and_matches_render_document() -> None:
    scenario, package, rendered = _default_render()
    assert set(rendered) == {document.document_id for document in package.documents}
    for document in package.documents:
        spec = scenario.document(document.document_id)
        assert rendered[document.document_id] == render_document(scenario, document, spec)


def test_clause_spans_are_exact_and_cover_heading_and_verbatim_text() -> None:
    _, package, rendered = _default_render()
    for document in package.documents:
        _assert_spans_exact(rendered[document.document_id], document)


def test_clause_text_is_never_reflowed() -> None:
    scenario = make_consulting_scenario()
    spec = scenario.document("main_agreement")
    package = make_generated_package(scenario)
    generated = package.document("main_agreement")
    long_text = (
        "Ersättningen uppgår till 950 SEK per timme exklusive mervärdesskatt. "
        "Betalning sker mot faktura inom 30 dagar från fakturans datum. "
        "Om betalning uteblir utgår dröjsmålsränta enligt lag. ÅÄÖ åäö – § 3."
    )
    generated.clauses[1] = GeneratedClause(
        clause_id="clause_02",
        document_id="main_agreement",
        heading="Ersättning och betalning",
        text=long_text,
    )

    rendered = render_document(scenario, generated, spec)

    assert rendered.canonical_text.count(long_text) == 1
    span = rendered.clause_spans["clause_02"]
    chunk = span.slice(rendered.canonical_text)
    assert long_text in chunk
    assert chunk == f"2. Ersättning och betalning\n{long_text}"
    _assert_spans_exact(rendered, generated)


def test_line_breaks_quality_adds_a_blank_line_without_breaking_spans() -> None:
    clean_scenario = make_consulting_scenario()
    clean_package = make_generated_package(clean_scenario)
    clean = render_package(clean_scenario, clean_package)["main_agreement"]
    scenario, package, line_break = _rendered_with_quality(DocumentQuality.LINE_BREAKS)

    assert "\n\n\n" not in clean.canonical_text
    assert "\n\n\n" in line_break.canonical_text
    assert line_break.canonical_text != clean.canonical_text
    _assert_spans_exact(line_break, package.document("main_agreement"))


@pytest.mark.parametrize("quality", [DocumentQuality.TABLES, DocumentQuality.PAGE_BREAKS])
def test_layout_only_quality_does_not_change_canonical_text(quality) -> None:
    clean_scenario = make_consulting_scenario()
    clean_package = make_generated_package(clean_scenario)
    clean = render_package(clean_scenario, clean_package)["main_agreement"]
    _, package, variant = _rendered_with_quality(quality)

    assert variant.canonical_text == clean.canonical_text
    _assert_spans_exact(variant, package.document("main_agreement"))


def test_swedish_and_unicode_code_points_keep_exact_offsets() -> None:
    scenario = make_consulting_scenario()
    spec = scenario.document("main_agreement")
    package = make_generated_package(scenario)
    generated = package.document("main_agreement")
    unicode_text = "Räntan är 3 % – återbetalning sker i SEK. Symbol: 🔒 och ÅÄÖ."
    generated.clauses[0] = GeneratedClause(
        clause_id="clause_01",
        document_id="main_agreement",
        heading="Särskilda bestämmelser",
        text=unicode_text,
    )

    rendered = render_document(scenario, generated, spec)
    span = rendered.clause_spans["clause_01"]
    chunk = span.slice(rendered.canonical_text)

    assert chunk == f"1. Särskilda bestämmelser\n{unicode_text}"
    assert len(chunk) == len(f"1. Särskilda bestämmelser\n{unicode_text}")
    assert rendered.canonical_text[span.start + len("1. Särskilda bestämmelser\n") : span.end] == (
        unicode_text
    )


def test_render_package_rejects_unknown_document() -> None:
    scenario = make_consulting_scenario()
    package = GeneratedPackage(
        contract_id=scenario.contract_id,
        documents=[
            GeneratedDocument(
                document_id="not_in_scenario",
                kind=DocKind.APPENDIX,
                title="Bilaga",
            )
        ],
    )
    with pytest.raises(GenerationError):
        render_package(scenario, package)


def test_render_package_rejects_contract_mismatch() -> None:
    scenario = make_consulting_scenario()
    package = GeneratedPackage(contract_id="contract_9999", documents=[])
    with pytest.raises(GenerationError):
        render_package(scenario, package)


def test_render_document_rejects_missing_or_extra_clauses() -> None:
    scenario = make_consulting_scenario()
    spec = scenario.document("main_agreement")
    package = make_generated_package(scenario)
    generated = package.document("main_agreement")

    missing = generated.model_copy(
        update={"clauses": [clause for clause in generated.clauses if clause.clause_id != "clause_03"]}
    )
    with pytest.raises(GenerationError):
        render_document(scenario, missing, spec)

    extra = generated.model_copy(
        update={
            "clauses": [
                *generated.clauses,
                GeneratedClause(
                    clause_id="clause_99",
                    document_id="main_agreement",
                    heading="Extra",
                    text="Extra text.",
                ),
            ]
        }
    )
    with pytest.raises(GenerationError):
        render_document(scenario, extra, spec)


def test_render_document_rejects_document_id_mismatch() -> None:
    scenario = make_consulting_scenario()
    spec = scenario.document("main_agreement")
    other = GeneratedDocument(
        document_id="appendix_a", kind=DocKind.APPENDIX, title="Bilaga A"
    )
    with pytest.raises(GenerationError):
        render_document(scenario, other, spec)


def test_render_appendix_document() -> None:
    scenario = make_consulting_scenario()
    scenario.documents.append(
        DocumentSpec(
            document_id="appendix_a",
            kind=DocKind.APPENDIX,
            title_sv="Bilaga A",
            title_en="Appendix A",
            filename_stem="appendix_a",
            clauses=[
                ClauseSpec(
                    clause_id="appendix_clause_01",
                    document_id="appendix_a",
                    order=1,
                    heading_sv="Övriga villkor",
                )
            ],
        )
    )
    package = make_generated_package(scenario)
    rendered = render_package(scenario, package)

    appendix = rendered["appendix_a"]
    assert appendix.kind == DocKind.APPENDIX
    assert "dokumenttyp: appendix" in appendix.canonical_text
    assert "Bilaga A" in appendix.canonical_text
    _assert_spans_exact(appendix, package.document("appendix_a"))


# ---------------------------------------------------------------------------
# Format writers
# ---------------------------------------------------------------------------


def test_write_document_txt_is_byte_identical_to_canonical_text(tmp_path: Path) -> None:
    _, _, rendered = _default_render()
    document = rendered["main_agreement"]

    path = write_document(
        document,
        target_without_suffix=tmp_path / "main_agreement",
        fmt=DocumentFormat.TXT,
    )

    assert path == tmp_path / "main_agreement.txt"
    assert path.read_bytes() == document.canonical_text.encode("utf-8")
    assert path.read_text(encoding="utf-8") == document.canonical_text


def test_write_document_creates_all_formats_and_they_are_readable(tmp_path: Path) -> None:
    _, _, rendered = _default_render()
    document = rendered["main_agreement"]

    paths: dict[DocumentFormat, Path] = {}
    for fmt in DocumentFormat:
        path = write_document(
            document,
            target_without_suffix=tmp_path / "nested" / "main_agreement",
            fmt=fmt,
        )
        assert path == tmp_path / "nested" / f"main_agreement{FORMAT_EXTENSIONS[fmt]}"
        assert path.is_file()
        assert path.stat().st_size > 0
        paths[fmt] = path

    markdown = paths[DocumentFormat.MARKDOWN].read_text(encoding="utf-8")
    assert markdown.startswith("# Konsultavtal")
    assert "> contract_id: contract_0001" in markdown
    assert "## 1. Avtalsparter" in markdown
    assert "## 2. Ersättning" in markdown
    assert "Parterna är överens om följande: 950 SEK." in markdown

    word = docx.Document(paths[DocumentFormat.DOCX])
    paragraphs = "\n".join(paragraph.text for paragraph in word.paragraphs)
    assert "Konsultavtal" in paragraphs
    assert "1. Avtalsparter" in paragraphs
    assert "Parterna är överens om följande: 950 SEK." in paragraphs

    pdf = paths[DocumentFormat.PDF].read_bytes()
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 500


def test_write_document_quality_variants_are_readable(tmp_path: Path) -> None:
    for quality in DocumentQuality:
        scenario, _, document = _rendered_with_quality(quality)
        quality_value = scenario.variation.document_quality

        text_path = write_document(
            document,
            target_without_suffix=tmp_path / quality.value / "main_agreement",
            fmt=DocumentFormat.TXT,
            quality=quality_value,
        )
        assert text_path.read_bytes() == document.canonical_text.encode("utf-8")

        word_path = write_document(
            document,
            target_without_suffix=tmp_path / quality.value / "main_agreement",
            fmt=DocumentFormat.DOCX,
            quality=quality_value,
        )
        word = docx.Document(word_path)
        word_text = "\n".join(paragraph.text for paragraph in word.paragraphs)
        assert "Konsultavtal" in word_text

        pdf_path = write_document(
            document,
            target_without_suffix=tmp_path / quality.value / "main_agreement",
            fmt=DocumentFormat.PDF,
            quality=quality_value,
        )
        assert pdf_path.read_bytes().startswith(b"%PDF")


def test_tables_quality_renders_clauses_as_a_table(tmp_path: Path) -> None:
    _, _, document = _rendered_with_quality(DocumentQuality.TABLES)
    path = write_document(
        document,
        target_without_suffix=tmp_path / "main_agreement",
        fmt=DocumentFormat.DOCX,
        quality=DocumentQuality.TABLES,
    )
    word = docx.Document(path)
    assert len(word.tables) == 1
    cells = [cell.text for row in word.tables[0].rows for cell in row.cells]
    assert any("Parterna är överens om följande: 950 SEK." in cell for cell in cells)
    assert "Klausul" in cells


def test_formats_are_deterministic(tmp_path: Path) -> None:
    _, _, rendered = _default_render()
    document = rendered["main_agreement"]
    for fmt in DocumentFormat:
        left = write_document(
            document, target_without_suffix=tmp_path / "left" / "main_agreement", fmt=fmt
        )
        right = write_document(
            document, target_without_suffix=tmp_path / "right" / "main_agreement", fmt=fmt
        )
        assert left.read_bytes() == right.read_bytes(), f"{fmt} is not deterministic"
