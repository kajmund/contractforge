from __future__ import annotations

import json

from contractforge.utils import (
    atomic_write_json,
    contains_span_text,
    find_span,
    normalize_for_search,
    read_json,
    sha256_text,
)


def test_normalize_for_search_maps_offsets():
    text = "Uppsägningstid:   Tre (3)  månader."
    normalized, offsets = normalize_for_search(text)
    position = normalized.find("tre (3) månader")
    assert position >= 0
    start = offsets[position]
    end = offsets[position + len("tre (3) månader") - 1] + 1
    assert text[start:end].strip() == "Tre (3)  månader"


def test_find_span_tolerates_whitespace_and_case():
    text = "Klausul 3. Uppsägning ska ske senast Tre   (3)  Månader före utgången."
    span = find_span(text, "tre (3) månader")
    assert span is not None
    start, end = span
    assert " ".join(text[start:end].lower().split()) == "tre (3) månader"


def test_find_span_respects_region():
    text = "tre månader. tre månader."
    first = find_span(text, "tre månader", 0, 11)
    assert first is not None
    assert first == (0, 11)
    second = find_span(text, "tre månader", 12)
    assert second is not None
    assert second[0] == 13


def test_find_span_missing_returns_none():
    assert find_span("inga siffror här", "tre månader") is None


def test_contains_span_text():
    text = "Avtalet kan sägas upp med tre månaders uppsägningstid."
    assert contains_span_text(text, 0, len(text), "tre månaders")
    assert not contains_span_text(text, 0, 20, "tre månaders")


def test_sha256_is_stable():
    assert sha256_text("abc") == sha256_text("abc")
    assert sha256_text("abc") != sha256_text("abd")


def test_atomic_json_roundtrip(tmp_path):
    path = tmp_path / "nested" / "data.json"
    atomic_write_json(path, {"a": 1, "å": "ä"})
    assert read_json(path) == {"a": 1, "å": "ä"}
    assert json.loads(path.read_text(encoding="utf-8"))["å"] == "ä"
