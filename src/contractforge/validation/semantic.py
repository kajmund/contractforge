"""Deterministic value and unit normalisation for contract fact verification.

The verifier must be able to answer "does this clause express the scenario
value?" without calling a model. This module does that with a small, fully
deterministic lexicon:

* Digits, including Swedish thousands spacing (``500 000``), Swedish decimal
  commas and English decimal points.
* Swedish (and a minimal English) number words: ``tre (3) månader``,
  ``ett kvartal``, ``tolv`` ...
* Durations are normalised to a common base of *months* using exact
  :class:`fractions.Fraction` arithmetic: ``kvartal`` -> 3 months,
  ``halvår`` -> 6 months, ``år`` -> 12 months, and ``vecka``/``dag`` are
  expressed as ``7/30`` and ``1/30`` months so that a 30 day notice period and
  a 1 month notice period compare equal without floating point drift.
* Money is normalised to the generic unit ``money`` (this project is SEK-only
  in the first version) including multipliers such as ``miljoner kronor``.
* Percentages accept ``%``, ``procent`` and ``percent``.

The result is used twice: validation reports concrete FAIL/WARN feedback for
repair, and gold only ever points at spans that really exist in the canonical
text. The module is intentionally dependency free (stdlib + domain models) so
it can be reused from every subsystem that needs the same normalisation.

No LLM is involved. An advisory judge may be attached externally; see
:func:`advisory_llm_checks` – it can only produce ``WARN`` checks and is marked
``independent=False`` because a model judgement is never independent facit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from fractions import Fraction
from typing import Any, Literal, Protocol

from ..models import CheckResult, CheckStatus, Fact, FactKind, Language
from ..utils import find_span

DURATION = "months"
PERCENT = "percent"
MONEY = "money"
PLAIN = "plain"

NUMERIC_UNITS: frozenset[str] = frozenset({DURATION, PERCENT, MONEY, PLAIN})

# Duration factor -> months. Fractions keep the comparison exact.
_DURATION_FACTORS: dict[str, Fraction] = {
    "månad": Fraction(1),
    "månader": Fraction(1),
    "mån": Fraction(1),
    "mån.": Fraction(1),
    "month": Fraction(1),
    "months": Fraction(1),
    "kvartal": Fraction(3),
    "quarter": Fraction(3),
    "quarters": Fraction(3),
    "halvår": Fraction(6),
    "half-year": Fraction(6),
    "halfyear": Fraction(6),
    "år": Fraction(12),
    "year": Fraction(12),
    "years": Fraction(12),
    "vecka": Fraction(7, 30),
    "veckor": Fraction(7, 30),
    "week": Fraction(7, 30),
    "weeks": Fraction(7, 30),
    "dag": Fraction(1, 30),
    "dagar": Fraction(1, 30),
    "day": Fraction(1, 30),
    "days": Fraction(1, 30),
    "dygn": Fraction(1, 30),
    "dygns": Fraction(1, 30),
    "timme": Fraction(1, 720),
    "timmar": Fraction(1, 720),
    "timmars": Fraction(1, 720),
    "hour": Fraction(1, 720),
    "hours": Fraction(1, 720),
    # Swedish compound units used by legal prose and the mock provider.
    "kalenderår": Fraction(12),
    "kalenderår.": Fraction(12),
    "kalendermånad": Fraction(1),
    "kalendermånader": Fraction(1),
    "arbetsdag": Fraction(1, 30),
    "arbetsdagar": Fraction(1, 30),
    "vardag": Fraction(1, 30),
    "vardagar": Fraction(1, 30),
    "semesterdag": Fraction(1, 30),
    "semesterdagar": Fraction(1, 30),
    "helgfri dag": Fraction(1, 30),
    "års": Fraction(12),
    "månads": Fraction(1),
    "månaders": Fraction(1),
    "dagars": Fraction(1, 30),
    "veckors": Fraction(7, 30),
}

_PERCENT_UNITS: tuple[str, ...] = ("procent", "percent", "pct", "%")
_CURRENCY_UNITS: tuple[str, ...] = (
    "sek",
    "kronor",
    "krona",
    "kr",
    "eur",
    "euro",
    "euros",
    "usd",
    "dollar",
    "dollars",
    "gbp",
    "pund",
    "pounds",
    "nok",
    "nkr",
    "dkk",
    "chf",
)
_UNIT_MODIFIERS: frozenset[str] = frozenset(
    {"helgfria", "helgfri", "kalender", "arbets", "samtliga", "hela"}
)
# Generic nouns that make a word number a count/duration without a canonical
# time or currency unit ("tolvtusen enheter", "två personer").
_GENERIC_UNIT_WORDS: frozenset[str] = frozenset(
    {
        "enhet",
        "enheter",
        "unit",
        "units",
        "styck",
        "person",
        "personer",
        "gång",
        "gånger",
        "tillfälle",
        "tillfällen",
        "punkt",
        "punkter",
        "parter",
        "avtal",
        "dokument",
    }
)
_MULTIPLIERS: dict[str, int] = {
    "tusen": 1000,
    "miljon": 10**6,
    "miljoner": 10**6,
    "miljard": 10**9,
    "miljarder": 10**9,
    "thousand": 1000,
    "million": 10**6,
    "millions": 10**6,
    "billion": 10**9,
    "billions": 10**9,
}

_SV_UNITS = {
    "noll": 0,
    "ett": 1,
    "en": 1,
    "två": 2,
    "tre": 3,
    "fyra": 4,
    "fem": 5,
    "sex": 6,
    "sju": 7,
    "åtta": 8,
    "nio": 9,
    "tio": 10,
    "elva": 11,
    "tolv": 12,
    "tretton": 13,
    "fjorton": 14,
    "femton": 15,
    "sexton": 16,
    "sjutton": 17,
    "arton": 18,
    "nitton": 19,
}
_SV_TENS = {
    "tjugo": 20,
    "trettio": 30,
    "fyrtio": 40,
    "femtio": 50,
    "sextio": 60,
    "sjuttio": 70,
    "åttio": 80,
    "nittio": 90,
}
_EN_UNITS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_EN_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}


def _build_word_numbers() -> dict[str, int]:
    words: dict[str, int] = {}
    words.update(_SV_UNITS)
    words.update(_SV_TENS)
    words.update(_EN_UNITS)
    words.update(_EN_TENS)
    # Common Swedish compounds: tjugotre, trettiofem, ...
    sv_units = [word for word in _SV_UNITS if word not in {"en"}]
    for tens_word, tens_value in _SV_TENS.items():
        for unit_word in sv_units:
            words[f"{tens_word}{unit_word}"] = tens_value + _SV_UNITS[unit_word]
    for tens_word, tens_value in _SV_TENS.items():
        for unit_word in ("ett", "en"):
            words[f"{tens_word}{unit_word}"] = tens_value + 1
    words.update({"hundra": 100, "tusen": 1000})
    return words


_WORD_NUMBERS = _build_word_numbers()

_NUMBER_SEGMENT_VALUES: dict[str, int] = {
    **_SV_UNITS,
    **_SV_TENS,
    **_EN_UNITS,
    **_EN_TENS,
}
_SCALE_SEGMENTS: dict[str, int] = {
    "hundra": 100,
    "tusen": 1000,
    "miljon": 10**6,
    "miljoner": 10**6,
    "miljard": 10**9,
    "miljarder": 10**9,
}
_NUMBER_SEGMENTS: list[str] = sorted(
    {*_NUMBER_SEGMENT_VALUES, *_SCALE_SEGMENTS}, key=len, reverse=True
)


def _parse_word_number(word: str) -> int | None:
    """Parse a Swedish/English number word, including Swedish compounds.

    ``fyrtiotusentvåhundrafemtio`` -> 40250, ``tolvtusen`` -> 12000.
    """

    direct = _WORD_NUMBERS.get(word)
    if direct is not None:
        return direct
    segments: list[str] = []
    position = 0
    while position < len(word):
        for segment in _NUMBER_SEGMENTS:
            if word.startswith(segment, position):
                segments.append(segment)
                position += len(segment)
                break
        else:
            return None
    if not segments:
        return None
    total = 0
    current = 0
    for segment in segments:
        if segment in _NUMBER_SEGMENT_VALUES:
            current += _NUMBER_SEGMENT_VALUES[segment]
        elif segment == "hundra":
            current = (current or 1) * 100
        elif segment == "tusen":
            total += (current or 1) * 1000
            current = 0
        elif segment in ("miljon", "miljoner"):
            total += (current or 1) * 1_000_000
            current = 0
        elif segment in ("miljard", "miljarder"):
            total += (current or 1) * 1_000_000_000
            current = 0
        else:  # pragma: no cover - all segments are handled above
            return None
    return total + current


_DIGIT_RE = re.compile(
    r"\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
)
_WORD_RE = re.compile(r"[A-Za-zÅÄÖåäö]+")
_UNIT_RE = re.compile(
    r"(?P<unit>"
    + "|".join(
        re.escape(word)
        for word in sorted(
            list(_DURATION_FACTORS) + list(_PERCENT_UNITS) + list(_CURRENCY_UNITS),
            key=len,
            reverse=True,
        )
    )
    + r")(?![\wÅÄÖåäö])",
    re.IGNORECASE,
)
_MULTIPLIER_RE = re.compile(
    r"(?P<mult>"
    + "|".join(
        re.escape(word) for word in sorted(_MULTIPLIERS, key=len, reverse=True)
    )
    + r")(?![\wÅÄÖåäö])",
    re.IGNORECASE,
)
_SKIP_CHARS = " \t\n\r\u00a0"
_PAREN_RE = re.compile(r"\(\s*[^()]{1,12}?\s*\)")


def _coerce_language(language: Language | str | None) -> Language:
    if isinstance(language, Language):
        return language
    if isinstance(language, str) and language.lower().startswith("en"):
        return Language.EN
    return Language.SV


# ---------------------------------------------------------------------------
# Quantities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Quantity:
    """A numeric value with a canonical unit and a span in the analysed text."""

    value: Fraction
    unit: str
    start: int
    end: int
    surface: str

    @property
    def is_numeric(self) -> bool:
        return self.unit in NUMERIC_UNITS


@dataclass(frozen=True)
class CanonicalQuantity:
    """Normalised representation of a scenario fact value."""

    value: Fraction
    unit: str


@dataclass(frozen=True)
class FactAnalysis:
    """Everything deterministic verification knows about one fact in one text."""

    explicit_surface: str | None = None
    explicit_span: tuple[int, int] | None = None
    paraphrase_surface: str | None = None
    paraphrase_span: tuple[int, int] | None = None
    semantic_span: tuple[int, int] | None = None
    semantic_quantity: Quantity | None = None
    keyword_span: tuple[int, int] | None = None
    keyword: str | None = None
    conflicts: tuple[Quantity, ...] = ()
    quantities: tuple[Quantity, ...] = ()
    all_list_values_present: bool = False

    @property
    def supported(self) -> bool:
        return (
            self.explicit_span is not None
            or self.paraphrase_span is not None
            or self.semantic_span is not None
        )


# ---------------------------------------------------------------------------
# Number parsing
# ---------------------------------------------------------------------------


def _parse_digit_number(raw: str, language: Language) -> Fraction | None:
    cleaned = raw.replace("\u00a0", "").replace(" ", "").strip()
    if not cleaned:
        return None
    if language == Language.EN:
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            if re.fullmatch(r"\d{1,3}(?:,\d{3})+", cleaned):
                cleaned = cleaned.replace(",", "")
            else:
                cleaned = cleaned.replace(",", ".")
    else:
        if "." in cleaned and "," in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "." in cleaned and re.fullmatch(r"\d{1,3}(?:\.\d{3})+", cleaned):
            cleaned = cleaned.replace(".", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
    try:
        return Fraction(cleaned)
    except (ValueError, ZeroDivisionError):
        return None


def parse_number(raw: str, language: Language | str = Language.SV) -> Fraction | None:
    """Parse one numeric token using the scenario language conventions."""

    return _parse_digit_number(raw, _coerce_language(language))


def _coerce_number(value: Any) -> Fraction | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(str(value))
    if isinstance(value, Fraction):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        number = _parse_digit_number(stripped, Language.SV)
        if number is not None:
            return number
        match = _DIGIT_RE.search(stripped)
        if match:
            return _parse_digit_number(match.group(0), Language.SV)
    return None


def _duration_factor(unit: str | None) -> Fraction | None:
    if unit is None:
        return None
    key = unit.strip().lower()
    if key in _DURATION_FACTORS:
        return _DURATION_FACTORS[key]
    singular = key.rstrip("s")
    return _DURATION_FACTORS.get(singular)


def _is_percent_unit(unit: str | None) -> bool:
    if unit is None:
        return False
    key = unit.strip().lower()
    return key in _PERCENT_UNITS or key.endswith("procent") or key.endswith("percent")


def canonical_quantity_for_fact(fact: Fact) -> CanonicalQuantity | None:
    """Return the normalised numeric value of a fact, or ``None`` if not numeric."""

    kind = fact.kind
    value = fact.value
    if kind is FactKind.INTEGER or kind is FactKind.DECIMAL:
        number = _coerce_number(value)
        if number is None:
            return None
        # The scenario may express an integer/decimal with a physical unit
        # ("30 days", "8 hours", "99.5 percent") or an implicit currency.
        duration_factor = _duration_factor(fact.unit)
        if duration_factor is not None:
            return CanonicalQuantity(number * duration_factor, DURATION)
        if _is_percent_unit(fact.unit):
            return CanonicalQuantity(number, PERCENT)
        if fact.currency:
            return CanonicalQuantity(number, MONEY)
        return CanonicalQuantity(number, PLAIN)
    if kind is FactKind.MONEY:
        number = _coerce_number(value)
        if number is None:
            return None
        return CanonicalQuantity(number, MONEY)
    if kind is FactKind.PERCENTAGE:
        number = _coerce_number(value)
        if number is None:
            return None
        return CanonicalQuantity(number, PERCENT)
    if kind is FactKind.DURATION:
        number = _coerce_number(value)
        factor = _duration_factor(fact.unit)
        if factor is None and isinstance(value, str):
            embedded = extract_quantities(value, Language.SV)
            if embedded and embedded[0].unit == DURATION:
                return CanonicalQuantity(embedded[0].value, DURATION)
        if number is None or factor is None:
            return None
        return CanonicalQuantity(number * factor, DURATION)
    return None


# ---------------------------------------------------------------------------
# Quantity extraction
# ---------------------------------------------------------------------------


def _skip_separators(text: str, position: int) -> int:
    length = len(text)
    while position < length:
        char = text[position]
        if char in _SKIP_CHARS or char in ")]}":
            position += 1
            continue
        if char == "(":
            match = _PAREN_RE.match(text, position)
            if match is not None:
                position = match.end()
                continue
        break
    return position


def _match_unit_after(
    text: str, position: int
) -> tuple[str, Fraction | None, int] | None:
    """Match a unit after a number, accepting compound Swedish unit words.

    Handles ``30 kalenderdagar``, ``3 kalenderår``, ``30 helgfria dagar`` and
    ``5 timmar``. Returns ``(kind, factor, end)`` where ``kind`` is
    ``"money"``/``"percent"``/``"duration"``.
    """

    pos = position
    for _ in range(2):
        pos = _skip_separators(text, pos)
        word_match = _WORD_RE.match(text, pos)
        if word_match is None or word_match.group(0).lower() not in _UNIT_MODIFIERS:
            break
        pos = word_match.end()
    unit_match = _UNIT_RE.match(text, pos)
    if unit_match is not None:
        token = unit_match.group("unit").lower()
        unit_end = unit_match.end()
        if token in _CURRENCY_UNITS:
            return "money", None, unit_end
        if token in _PERCENT_UNITS:
            return "percent", None, unit_end
        return "duration", _DURATION_FACTORS[token], unit_end
    word_match = _WORD_RE.match(text, pos)
    if word_match is not None:
        word = word_match.group(0).lower()
        for candidate, factor in sorted(
            _DURATION_FACTORS.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if len(candidate) >= 2 and len(word) > len(candidate) and word.endswith(candidate):
                return "duration", factor, word_match.end()
    return None


def _quantity_from_number(
    text: str,
    start: int,
    end: int,
    value: Fraction,
    *,
    allow_plain: bool,
) -> Quantity | None:
    position = _skip_separators(text, end)
    multiplier = Fraction(1)
    has_multiplier = False
    multiplier_end = position
    multiplier_match = _MULTIPLIER_RE.match(text, position)
    if multiplier_match is not None:
        word = multiplier_match.group("mult").lower()
        multiplier = Fraction(_MULTIPLIERS[word])
        has_multiplier = True
        multiplier_end = multiplier_match.end()
        position = _skip_separators(text, multiplier_end)
    unit_result = _match_unit_after(text, position)
    if unit_result is not None:
        kind, factor, unit_end = unit_result
        if kind == "money":
            return Quantity(value * multiplier, MONEY, start, unit_end, text[start:unit_end])
        if kind == "percent":
            return Quantity(value, PERCENT, start, unit_end, text[start:unit_end])
        return Quantity(
            value * (factor or Fraction(1)), DURATION, start, unit_end, text[start:unit_end]
        )
    if has_multiplier:
        return Quantity(
            value * multiplier, MONEY, start, multiplier_end, text[start:multiplier_end]
        )
    if allow_plain:
        return Quantity(value, PLAIN, start, end, text[start:end])
    word_match = _WORD_RE.match(text, _skip_separators(text, position))
    if word_match is not None and word_match.group(0).lower() in _GENERIC_UNIT_WORDS:
        return Quantity(value, PLAIN, start, word_match.end(), text[start:word_match.end()])
    return None


def _dedupe_overlaps(quantities: list[Quantity]) -> list[Quantity]:
    quantities.sort(key=lambda quantity: (quantity.start, -quantity.end))
    kept: list[Quantity] = []
    for quantity in quantities:
        if kept and kept[-1].start <= quantity.start < kept[-1].end:
            continue
        kept.append(quantity)
    return kept


def extract_quantities(
    text: str, language: Language | str = Language.SV
) -> list[Quantity]:
    """Extract numeric quantities with canonical units, in text order.

    The returned spans are Unicode code point offsets into ``text``.
    """

    if not text:
        return []
    language = _coerce_language(language)
    candidates: list[Quantity] = []
    for match in _DIGIT_RE.finditer(text):
        number = _parse_digit_number(match.group(0), language)
        if number is None:
            continue
        quantity = _quantity_from_number(
            text, match.start(), match.end(), number, allow_plain=True
        )
        if quantity is not None:
            candidates.append(quantity)
    for match in _WORD_RE.finditer(text):
        word = match.group(0).lower()
        number_value = _parse_word_number(word)
        if number_value is None:
            continue
        quantity = _quantity_from_number(
            text,
            match.start(),
            match.end(),
            Fraction(number_value),
            allow_plain=False,
        )
        if quantity is not None:
            candidates.append(quantity)
    return _dedupe_overlaps(candidates)


# ---------------------------------------------------------------------------
# Fact analysis
# ---------------------------------------------------------------------------


def _find_surface(text: str, surfaces: list[str]) -> tuple[str, tuple[int, int]] | None:
    for surface in surfaces:
        if not surface or not surface.strip():
            continue
        span = find_span(text, surface)
        if span is not None:
            return surface, span
    return None


def _keywords(fact: Fact, language: Language) -> list[str]:
    primary = fact.keywords_sv if language == Language.SV else fact.keywords_en
    secondary = fact.keywords_en if language == Language.SV else fact.keywords_sv
    return [word for word in [*primary, *secondary] if word and word.strip()]


def _find_keyword(text: str, fact: Fact, language: Language) -> tuple[str, tuple[int, int]] | None:
    for keyword in _keywords(fact, language):
        span = find_span(text, keyword)
        if span is not None:
            return keyword, span
    return None


_SV_MONTH_NAMES = (
    "januari",
    "februari",
    "mars",
    "april",
    "maj",
    "juni",
    "juli",
    "augusti",
    "september",
    "oktober",
    "november",
    "december",
)
_EN_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH_LOOKUP: dict[str, int] = {
    **{name: index + 1 for index, name in enumerate(_SV_MONTH_NAMES)},
    **{name: index + 1 for index, name in enumerate(_EN_MONTH_NAMES)},
}
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_LOCAL_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-zÅÄÖåäö]+)\s+(\d{4})")


def _parse_date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    iso_match = _ISO_DATE_RE.search(text)
    if iso_match is not None:
        try:
            return date(
                int(iso_match.group(1)),
                int(iso_match.group(2)),
                int(iso_match.group(3)),
            )
        except ValueError:
            return None
    local_match = _LOCAL_DATE_RE.search(text)
    if local_match is not None:
        month = _MONTH_LOOKUP.get(local_match.group(2).lower())
        if month is not None:
            try:
                return date(int(local_match.group(3)), month, int(local_match.group(1)))
            except ValueError:
                return None
    return None


def _date_needles(value: Any) -> list[str]:
    parsed = _parse_date_value(value)
    if parsed is None:
        text = str(value).strip() if value is not None else ""
        return [text] if text else []
    needles: list[str] = [parsed.isoformat(), f"{parsed.year}-{parsed.month:02d}-{parsed.day:02d}"]
    day_forms = (str(parsed.day), f"{parsed.day:02d}")
    for day in day_forms:
        for names in (_SV_MONTH_NAMES, _EN_MONTH_NAMES):
            needles.append(f"{day} {names[parsed.month - 1]} {parsed.year}")
    return list(dict.fromkeys(needle for needle in needles if needle))


def _text_semantic_needles(fact: Fact) -> list[str]:
    """Literal textual needles that count as a semantic hit for text-ish facts."""

    kind = fact.kind
    value = fact.value
    if kind is FactKind.DATE:
        return _date_needles(value)
    if kind is FactKind.BOOLEAN:
        if bool(value):
            return ["ja", "sant", "gäller", "ska", "yes", "true"]
        return ["nej", "falskt", "inte", "no", "false"]
    if kind is FactKind.LIST:
        return [str(item) for item in value if str(item).strip()]
    if kind in (
        FactKind.TEXT,
        FactKind.ENTITY,
        FactKind.DEFINED_TERM,
    ):
        return [str(value)] if str(value).strip() else []
    return []


def _sibling_canonical_values(
    facts: Iterable[Fact], unit: str
) -> set[tuple[str, Fraction]]:
    """``(unit, value)`` pairs of sibling facts, for conflict exclusion."""

    values: set[tuple[str, Fraction]] = set()
    for sibling in facts:
        sibling_quantity = canonical_quantity_for_fact(sibling)
        if sibling_quantity is not None and sibling_quantity.unit == unit:
            values.add((unit, sibling_quantity.value))
    return values


def analyze_fact(
    fact: Fact,
    text: str,
    language: Language | str = Language.SV,
    *,
    co_located_facts: Iterable[Fact] = (),
    conflicts_with_facts: Iterable[Fact] = (),
) -> FactAnalysis:
    """Analyse how ``fact`` is expressed in one clause body text.

    ``co_located_facts`` are the *other* facts the clause is expected to state.
    A clause may legitimately carry several numeric facts of the same canonical
    unit (a probation period of 6 months next to 25 vacation days); those
    values are correct sibling facts, not distractors, so they are excluded from
    :attr:`FactAnalysis.conflicts`.

    ``conflicts_with_facts`` are the exception: facts the scenario declares as
    intentionally contradicting ``fact``. Their values stay in
    :attr:`FactAnalysis.conflicts` so callers see both sides of the declared
    contradiction.
    """

    language = _coerce_language(language)
    explicit = _find_surface(text, list(fact.explicit_surfaces))
    paraphrase = _find_surface(text, list(fact.paraphrase_surfaces))
    keyword = _find_keyword(text, fact, language)

    quantities = extract_quantities(text, language)
    canonical = canonical_quantity_for_fact(fact)
    semantic_span: tuple[int, int] | None = None
    semantic_quantity: Quantity | None = None
    conflicts: tuple[Quantity, ...] = ()
    all_list_values_present = False

    if canonical is not None:
        matches = [
            quantity
            for quantity in quantities
            if quantity.unit == canonical.unit and quantity.value == canonical.value
        ]
        if matches:
            semantic_quantity = matches[0]
            semantic_span = (matches[0].start, matches[0].end)
        if canonical.unit in (DURATION, MONEY, PERCENT):
            sibling_values = _sibling_canonical_values(co_located_facts, canonical.unit)
            sibling_values -= _sibling_canonical_values(conflicts_with_facts, canonical.unit)
            conflicts = tuple(
                quantity
                for quantity in quantities
                if quantity.unit == canonical.unit
                and quantity.value != canonical.value
                and (quantity.unit, quantity.value) not in sibling_values
            )
    else:
        needles = _text_semantic_needles(fact)
        if fact.kind is FactKind.LIST:
            spans = [find_span(text, needle) for needle in needles]
            if needles and all(span is not None for span in spans):
                all_list_values_present = True
                first = next(span for span in spans if span is not None)
                semantic_span = first
        else:
            for needle in needles:
                span = find_span(text, needle)
                if span is not None:
                    semantic_span = span
                    break

    if semantic_span is None and fact.kind is FactKind.DATE:
        # A declared explicit/paraphrase date surface is itself a verified hit.
        if explicit is not None:
            semantic_span = explicit[1]
        elif paraphrase is not None:
            semantic_span = paraphrase[1]

    return FactAnalysis(
        explicit_surface=explicit[0] if explicit else None,
        explicit_span=explicit[1] if explicit else None,
        paraphrase_surface=paraphrase[0] if paraphrase else None,
        paraphrase_span=paraphrase[1] if paraphrase else None,
        semantic_span=semantic_span,
        semantic_quantity=semantic_quantity,
        keyword_span=keyword[1] if keyword else None,
        keyword=keyword[0] if keyword else None,
        conflicts=conflicts,
        quantities=tuple(quantities),
        all_list_values_present=all_list_values_present,
    )


# ---------------------------------------------------------------------------
# Operator assessment: is a deviating value binding or merely mentioned?
# ---------------------------------------------------------------------------

BINDING = "binding"
NON_BINDING = "non_binding"
AMBIGUOUS = "ambiguous"

OperatorVerdict = Literal["binding", "non_binding", "ambiguous"]


@dataclass(frozen=True)
class OperatorAssessment:
    """Verdict for one deviating quantity together with the evidence behind it."""

    verdict: OperatorVerdict
    sentence: str
    markers: tuple[str, ...] = ()

    @property
    def binding(self) -> bool:
        return self.verdict == BINDING

    @property
    def non_binding(self) -> bool:
        return self.verdict == NON_BINDING


# Phrases that frame a number as something merely discussed, earlier, optional,
# conditional or explicitly not applicable. Matching is done on lower case text
# with whitespace collapsed, so a marker may span a line break.
_NON_BINDING_MARKERS: tuple[str, ...] = (
    # Swedish
    "diskuterat",
    "diskuterades",
    "diskuterad",
    "diskussion",
    "ursprunglig",
    "ursprungligen",
    "tidigare",
    "alternativt",
    "alternativ",
    "skulle kunna",
    "förslag",
    "utkast",
    "ej bindande",
    "icke bindande",
    "inte bindande",
    "under förhandling",
    "preliminär",
    "om vi",
    "villkorat av",
    "avviker från",
    "se dock",
    "undantag",
    "dock inte",
    "inte gäller",
    "gäller inte",
    "inte avtalad",
    "inte avtalat",
    "inte godkänd",
    "inte godkänts",
    "har inte godkänts",
    "äldre",
    "härrör",
    "samtal",
    "förhandlingar",
    # English
    "discussed",
    "original",
    "previously",
    "previous",
    "draft",
    "proposal",
    "suggested",
    "non-binding",
    "not binding",
    "conditional",
    "subject to",
    "except",
    "does not apply",
    "do not apply",
    "not agreed",
    "not approved",
)

# Phrases that amend, replace or supersede a value: the number they govern is
# the operative one, so a deviating value inside such a sentence is binding.
_BINDING_MARKERS: tuple[str, ...] = (
    # Swedish
    "ersätts",
    "ersätter",
    "ersatt",
    "ändras till",
    "ändras från",
    "ändrat",
    "gäller i stället",
    "i stället",
    "ska i stället vara",
    "härefter",
    "hädanefter",
    "justeras till",
    "justerad",
    "uppgår nu till",
    "uppgår till",
    "från och med nu",
    "slutligt",
    "överenskommits till",
    "överenskommits om",
    "gäller från",
    "reduceras till",
    "höjs till",
    "sänks till",
    "tillägget anger",
    "gällande belopp är",
    "som nu gäller",
    # English
    "is replaced",
    "are replaced",
    "replaced by",
    "replaces",
    "shall instead",
    "amended to",
    "amended",
    "henceforth",
    "now amounts to",
    "supersedes",
    "superseded by",
    "as of now",
)

# Abbreviations whose period must not end a sentence.
_ABBREVIATIONS: frozenset[str] = frozenset(
    {
        "ab",
        "hb",
        "kb",
        "ek",
        "ek.",
        "ekon.",
        "m.fl",
        "m.m",
        "bl.a",
        "t.ex",
        "fr.o.m",
        "t.o.m",
        "d.v.s",
        "dvs",
        "jfr",
        "nr",
        "ang",
        "kap",
        "mom",
        "p",
        "§",
        "no.",
        "inc",
        "ltd",
        "co",
        "plc",
        "asa",
    }
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WS_RE = re.compile(r"\s+")


def _normalized_lower(text: str) -> tuple[str, list[int]]:
    """Lower case ``text`` collapsing whitespace, keeping an offset map."""

    chars: list[str] = []
    offsets: list[int] = []
    pending_space = False
    for index, char in enumerate(text):
        if char.isspace():
            pending_space = bool(chars)
            continue
        if pending_space:
            chars.append(" ")
            offsets.append(index)
            pending_space = False
        chars.append(char.lower())
        offsets.append(index)
    return "".join(chars), offsets


def _sentence_bounds(text: str) -> list[tuple[int, int]]:
    """Sentence spans in ``text``; periods in abbreviations do not split."""

    bounds: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_SPLIT_RE.finditer(text):
        sentence = text[start : match.start()]
        last_word = re.search(r"([\wÅÄÖåäö§]+)\.$", sentence)
        if last_word is not None and last_word.group(1).lower() in _ABBREVIATIONS:
            continue
        bounds.append((start, match.start()))
        start = match.end()
    if start < len(text):
        bounds.append((start, len(text)))
    stripped = [(span[0], span[1]) for span in bounds if text[span[0] : span[1]].strip()]
    return stripped or [(0, len(text))]


def _sentence_containing(text: str, position: int) -> tuple[int, int]:
    for start, end in _sentence_bounds(text):
        if start <= position < end:
            return start, end
    return 0, len(text)


def _find_markers(haystack: str, markers: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for marker in markers:
        if marker in haystack and marker not in found:
            found.append(marker)
    return found


def _operator_target(text: str, position: int) -> str:
    """The sentence around ``position``, narrowed to its sub-clause if any.

    A distractor frame in a sibling sub-clause must not whitewash an explicit
    replacement: in "Avtalet ersätts härmed; timarvodet är 9 999 SEK" the value
    sits in the second sub-clause, which carries no distractor frame.
    """

    start, end = _sentence_containing(text, position)
    target = text[start:end]
    relative = position - start
    cut = max(target.rfind(separator, 0, relative) for separator in (";", ":"))
    return target[cut + 1 :] if cut > 0 else target


def assess_quantity_operator(
    text: str,
    fact: Fact | None,
    quantity: Quantity,
    language: Language | str = Language.SV,
) -> OperatorAssessment:
    """Assess the operator around ``quantity`` and return the matched markers."""

    del fact  # The verdict is textual; the fact only labels the caller's check.
    del language  # Marker lexicons cover both languages; wording decides.
    # A replacement frame usually sits in a sibling sub-clause of the same
    # sentence ("Detta villkor ersätts härmed: beloppet är 9 999 SEK"), so the
    # whole sentence is checked first; the narrower sub-clause is used when the
    # sentence names both an earlier and a current value.
    start, end = _sentence_containing(text, quantity.start)
    candidates = (text[start:end], _operator_target(text, quantity.start))
    haystacks = [(_normalized_lower(target)[0], target) for target in candidates]
    for haystack, target in haystacks:
        binding = _find_markers(haystack, _BINDING_MARKERS)
        if binding:
            return OperatorAssessment(BINDING, target.strip(), tuple(binding))
    for haystack, target in haystacks:
        non_binding = _find_markers(haystack, _NON_BINDING_MARKERS)
        if non_binding:
            return OperatorAssessment(NON_BINDING, target.strip(), tuple(non_binding))
    return OperatorAssessment(AMBIGUOUS, candidates[0].strip(), ())


def assess_operator(
    text: str,
    fact: Fact | None,
    quantity_span: tuple[int, int],
    language: Language | str = Language.SV,
) -> OperatorVerdict:
    """Decide whether a deviating value is binding, non-binding or ambiguous.

    The decision is deterministic and purely textual. It looks at the sentence
    (or the sub-clause after ``;``/``:``) that contains ``quantity_span`` and
    either finds a distractor frame ("diskuterades", "tidigare utkast",
    "not binding", ...) or a replacement/amendment frame ("ersätts härmed",
    "is replaced by", ...).

    A *binding* marker always wins over a non-binding one: "det tidigare värdet
    ersätts härmed av 9 999 SEK" both mentions the past and establishes a new
    obligation, and the new obligation is what the text actually says.

    Returns ``"ambiguous"`` when neither side is signalled so the caller can
    fail closed: an unexplained deviating number in a clause that should state
    a known value is treated as binding. Gold is never consulted; only the
    wording decides, which keeps the check independent of the generator.
    """

    if quantity_span[1] <= quantity_span[0] or quantity_span[0] < 0:
        return AMBIGUOUS
    start = quantity_span[0]
    probe = Quantity(Fraction(0), PLAIN, start, quantity_span[1], text[start : quantity_span[1]])
    return assess_quantity_operator(text, fact, probe, language).verdict


# ---------------------------------------------------------------------------
# Display helpers (used in FAIL messages -> repair feedback)
# ---------------------------------------------------------------------------


def display_expected(fact: Fact) -> str:
    parts = [str(fact.value)]
    if fact.unit:
        parts.append(str(fact.unit))
    if fact.currency:
        parts.append(str(fact.currency))
    return " ".join(part for part in parts if part)


def display_quantity(quantity: Quantity) -> str:
    return f"'{quantity.surface}'"


def display_conflicts(conflicts: tuple[Quantity, ...]) -> str:
    if not conflicts:
        return "inga"
    return ", ".join(f"'{quantity.surface}'" for quantity in conflicts)


# ---------------------------------------------------------------------------
# Advisory (never independent) LLM judge
# ---------------------------------------------------------------------------


class SemanticJudge(Protocol):
    """Advisory judge protocol. It may only produce WARN checks."""

    def review(self, request: JudgeRequest) -> list[str]:
        """Return human readable concerns; empty means no objection."""


@dataclass(frozen=True)
class JudgeRequest:
    contract_id: str
    document_id: str
    clause_id: str
    fact_id: str
    clause_text: str


def advisory_llm_checks(
    judge: SemanticJudge,
    *,
    contract_id: str,
    document_id: str,
    clause_id: str,
    fact: Fact,
    clause_text: str,
) -> list[CheckResult]:
    """Run an advisory judge and force every result to ``WARN``.

    The deterministic checks are authoritative. A model judgement is *not*
    independent facit, so callers must not use it to accept or reject a
    document; it only adds review hints. ``validate_package`` never invokes
    this by default.
    """

    request = JudgeRequest(
        contract_id=contract_id,
        document_id=document_id,
        clause_id=clause_id,
        fact_id=fact.fact_id,
        clause_text=clause_text,
    )
    results: list[CheckResult] = []
    for message in judge.review(request):
        results.append(
            CheckResult(
                check_id=f"semantic.judge.{clause_id}.{fact.fact_id}",
                status=CheckStatus.WARN,
                message=message,
                document_id=document_id,
                clause_id=clause_id,
                details={
                    "source": "llm_judge",
                    "independent": False,
                    "advisory_only": True,
                },
            )
        )
    return results


__all__ = [
    "AMBIGUOUS",
    "BINDING",
    "DURATION",
    "FactAnalysis",
    "MONEY",
    "NON_BINDING",
    "NUMERIC_UNITS",
    "OperatorAssessment",
    "OperatorVerdict",
    "PERCENT",
    "PLAIN",
    "CanonicalQuantity",
    "JudgeRequest",
    "Quantity",
    "SemanticJudge",
    "advisory_llm_checks",
    "analyze_fact",
    "assess_operator",
    "assess_quantity_operator",
    "canonical_quantity_for_fact",
    "display_conflicts",
    "display_expected",
    "display_quantity",
    "extract_quantities",
    "parse_number",
]
