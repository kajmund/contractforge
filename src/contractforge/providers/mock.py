"""Deterministic offline provider.

``MockProvider`` is an *explicit* provider choice (``provider="mock"``) for
tests, CI and benchmarks. It is never used as a silent fallback: provider
selection fails loudly instead.

The provider reads the scenario and document that
:mod:`contractforge.generation.prompts` places in ``LLMRequest.metadata`` and
renders one deterministic clause text per planned clause. Wording variants are
chosen with a stable SHA-256 hash of contract id, clause id and clause mode, so
the same scenario always yields the same package, independent of process,
platform and ``PYTHONHASHSEED``.

Text fidelity rules (the reason this provider exists):

* ``explicit`` clauses contain one of the fact's ``explicit_surfaces``
  verbatim, which is what deterministic validation checks for.
* ``paraphrase`` clauses prefer ``paraphrase_surfaces`` and Swedish number-word
  forms ("niohundrafemtio kronor").
* ``distractor`` clauses add a near-miss value, explicitly framed as an earlier
  draft that does not apply.
* ``definition`` and ``reference`` clauses use their own templates and never
  write document/clause/fact ids into the clause text.
* documented anomalies (contradiction, exception, supersession, missing
  information, ambiguity) are reproduced, or deliberately left out for missing
  information.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import Any

from ..errors import ConfigError, GenerationError
from ..models import (
    Anomaly,
    AnomalyKind,
    ClauseMode,
    ClauseSpec,
    DocKind,
    DocumentSpec,
    Fact,
    FactKind,
    Language,
    LLMRequest,
    LLMResponse,
    Scenario,
    Usage,
)

MOCK_MODEL = "mock-1"

_Template = tuple[str, str]  # (Swedish, English)

_TEMPLATES: dict[ClauseMode, tuple[_Template, ...]] = {
    ClauseMode.EXPLICIT: (
        (
            "Parterna är överens om att {facts}.",
            "The parties agree that {facts}.",
        ),
        (
            "Enligt detta avtal ska följande gälla: {facts}.",
            "Under this agreement the following shall apply: {facts}.",
        ),
        (
            "Följande villkor gäller för {heading}: {facts}.",
            "The following terms apply to {heading}: {facts}.",
        ),
    ),
    ClauseMode.PARAPHRASE: (
        (
            "I enlighet med parternas överenskommelse gäller att {facts}.",
            "In accordance with the parties' agreement, {facts}.",
        ),
        (
            "Det åligger parterna att tillse att {facts}.",
            "The parties shall ensure that {facts}.",
        ),
        (
            "Såvitt avser {heading} har parterna kommit överens om att {facts}.",
            "As regards {heading}, the parties have agreed that {facts}.",
        ),
    ),
    ClauseMode.SPLIT: (
        (
            "Vad gäller {heading} gäller följande: {facts}.",
            "As regards {heading}, the following applies: {facts}.",
        ),
        (
            "Parterna har fördelat skyldigheterna så att {facts}.",
            "The parties have allocated their obligations so that {facts}.",
        ),
        (
            "Villkoret för {heading} är att {facts}.",
            "The condition for {heading} is that {facts}.",
        ),
    ),
    ClauseMode.VAGUE: (
        (
            "Parterna avser att närmare reglera {heading}; i avvaktan på detta gäller att {facts}.",
            "The parties intend to regulate {heading} in more detail; pending this, {facts}.",
        ),
        (
            "I den utsträckning parterna finner skäligt ska följande gälla: {facts}.",
            "To the extent the parties deem reasonable, the following shall apply: {facts}.",
        ),
        (
            "Beträffande {heading} har parterna diskuterat att {facts}, utan att "
            "detta utgör en slutlig reglering.",
            "Regarding {heading}, the parties have discussed that {facts}, without "
            "this constituting a final provision.",
        ),
    ),
    ClauseMode.DISTRACTOR: (
        (
            "Parterna är överens om att {facts}. Enligt ett tidigare utkast angavs "
            "{distractor}, vilket inte gäller.",
            "The parties agree that {facts}. An earlier draft stated {distractor}, "
            "which does not apply.",
        ),
        (
            "Av {heading} framgår att {facts}. Ett äldre förslag med {distractor} "
            "har inte godkänts.",
            "The terms on {heading} show that {facts}. An older proposal involving "
            "{distractor} was not approved.",
        ),
        (
            "För tydlighetens skull: {facts}. Uppgiften {distractor} härrör från ett "
            "tidigare samtal och är inte avtalad.",
            "For clarity: {facts}. The figure {distractor} stems from an earlier "
            "discussion and is not agreed.",
        ),
    ),
    ClauseMode.DEFINITION: (
        (
            "För detta avtals ändamål gäller att {facts}.",
            "For the purposes of this agreement, {facts}.",
        ),
        (
            "Definitioner: {facts}.",
            "Definitions: {facts}.",
        ),
        (
            "Parterna definierar begreppen enligt följande: {facts}.",
            "The parties define the terms as follows: {facts}.",
        ),
    ),
    ClauseMode.REFERENCE: (
        (
            "Såsom framgår av {reference} gäller att {facts}.",
            "As set out in {reference}, {facts}.",
        ),
        (
            "Parterna hänvisar till {reference} avseende {heading}; följaktligen "
            "gäller att {facts}.",
            "The parties refer to {reference} regarding {heading}; accordingly, {facts}.",
        ),
        (
            "Med hänvisning till {reference} bekräftas att {facts}.",
            "With reference to {reference}, it is confirmed that {facts}.",
        ),
    ),
    ClauseMode.CONDITIONAL: (
        (
            "För det fall att {condition} gäller att {facts}.",
            "In the event that {condition}, {facts}.",
        ),
        (
            "Om {condition}, ska följande gälla: {facts}.",
            "If {condition}, the following shall apply: {facts}.",
        ),
        (
            "Villkoret för att {facts} är att {condition}.",
            "The condition for {facts} is that {condition}.",
        ),
    ),
    ClauseMode.EXCEPTION: (
        (
            "Undantaget från {exception_condition} är att {facts}.",
            "The exception to {exception_condition} is that {facts}.",
        ),
        (
            "Utöver vad som anges i {exception_condition} gäller att {facts}.",
            "In addition to what is stated in {exception_condition}, {facts}.",
        ),
        (
            "Med undantag för {exception_condition} ska följande gälla: {facts}.",
            "With the exception of {exception_condition}, the following shall apply: {facts}.",
        ),
    ),
    ClauseMode.CONTRADICTION: (
        (
            "Avtalet innehåller motstridiga bestämmelser: {facts}.",
            "The agreement contains conflicting provisions: {facts}.",
        ),
        (
            "Motstridiga uppgifter förekommer i avtalet: {facts}.",
            "Conflicting statements appear in the agreement: {facts}.",
        ),
        (
            "Bestämmelserna är inte samstämmiga: {facts}.",
            "The provisions are not consistent: {facts}.",
        ),
    ),
}

_FILLERS: tuple[_Template, ...] = (
    (
        "Parterna ska fullgöra sina skyldigheter avseende {heading} med omsorg och "
        "i enlighet med detta avtal.",
        "The parties shall perform their obligations regarding {heading} with due "
        "care and in accordance with this agreement.",
    ),
    (
        "I fråga om {heading} gäller vad som i övrigt följer av detta avtal och tillämplig lag.",
        "As regards {heading}, what otherwise follows from this agreement and "
        "applicable law shall apply.",
    ),
    (
        "Parterna ska samråda kring {heading} innan åtgärder vidtas som påverkar "
        "avtalsförhållandet.",
        "The parties shall consult on {heading} before taking measures that affect "
        "the contractual relationship.",
    ),
)

_MONEY_VERBS: tuple[tuple[str, ...], tuple[str, ...]] = (
    ("uppgår till", "ska vara", "fastställs till"),
    ("amounts to", "shall be", "is set at"),
)
_QUANTITY_VERBS: tuple[tuple[str, ...], tuple[str, ...]] = (
    ("ska vara", "fastställs till", "uppgår till"),
    ("shall be", "is set at", "amounts to"),
)
_PLAIN_VERBS: tuple[tuple[str, ...], tuple[str, ...]] = (
    ("ska vara", "är", "fastställs till"),
    ("shall be", "is", "is set as"),
)

_SV_ONES: tuple[str, ...] = (
    "noll",
    "ett",
    "två",
    "tre",
    "fyra",
    "fem",
    "sex",
    "sju",
    "åtta",
    "nio",
    "tio",
    "elva",
    "tolv",
    "tretton",
    "fjorton",
    "femton",
    "sexton",
    "sjutton",
    "arton",
    "nitton",
)
_SV_TENS: dict[int, str] = {
    20: "tjugo",
    30: "trettio",
    40: "fyrtio",
    50: "femtio",
    60: "sextio",
    70: "sjuttio",
    80: "åttio",
    90: "nittio",
}
# Unit tokens a deterministic semantic checker can normalize (Swedish/English
# duration and money words). Surfaces containing one of these are preferred so
# the mock does not pick, for example, "30 dygn" or "3 kalenderår" when a
# parseable alternative from the same fact exists.
_RECOGNIZED_DURATION_UNITS: frozenset[str] = frozenset(
    {
        "månad",
        "månader",
        "mån",
        "mån.",
        "month",
        "months",
        "kvartal",
        "quarter",
        "quarters",
        "halvår",
        "år",
        "year",
        "years",
        "vecka",
        "veckor",
        "week",
        "weeks",
        "dag",
        "dagar",
        "day",
        "days",
    }
)
_RECOGNIZED_MONEY_UNITS: frozenset[str] = frozenset({"sek", "kronor", "krona", "kr"})
_RECOGNIZED_PERCENT_UNITS: frozenset[str] = frozenset({"procent", "percent", "pct", "%"})
_SURFACE_TOKEN_RE = re.compile(r"[A-Za-zÅÄÖåäö]+|%")

_UNIT_WORDS: dict[str, tuple[str, str]] = {
    "months": ("månader", "months"),
    "month": ("månad", "month"),
    "weeks": ("veckor", "weeks"),
    "week": ("vecka", "week"),
    "days": ("dagar", "days"),
    "day": ("dag", "day"),
    "years": ("år", "years"),
    "year": ("år", "year"),
    "hours": ("timmar", "hours"),
    "hour": ("timme", "hour"),
}


class MockProvider:
    """Deterministic offline provider selected explicitly with ``provider="mock"``."""

    name = "mock"

    def __init__(self, *, model: str = MOCK_MODEL) -> None:
        if not model:
            raise ConfigError("a mock model id is required")
        self.model = model

    def __repr__(self) -> str:
        return f"MockProvider(model={self.model!r})"

    def complete(self, request: LLMRequest) -> LLMResponse:
        scenario = _scenario_from_metadata(request.metadata)
        document = _document_from_metadata(request.metadata)
        payload = _build_document_payload(scenario, document)
        return LLMResponse(
            text=json.dumps(payload, ensure_ascii=False),
            provider=self.name,
            model=self.model,
            usage=Usage(),
            latency_ms=0.0,
            finish_reason="stop",
            raw={
                "provider": self.name,
                "deterministic": True,
                "document_id": document.document_id,
            },
        )


# ---------------------------------------------------------------------------
# Metadata parsing
# ---------------------------------------------------------------------------


def _scenario_from_metadata(metadata: dict[str, Any]) -> Scenario:
    raw = metadata.get("scenario")
    if raw is None:
        raise GenerationError("mock provider requires request.metadata['scenario'] (scenario JSON)")
    try:
        if isinstance(raw, str):
            return Scenario.model_validate_json(raw)
        return Scenario.model_validate(raw)
    except ValueError as exc:
        raise GenerationError(f"mock provider could not parse scenario metadata: {exc}") from exc


def _document_from_metadata(metadata: dict[str, Any]) -> DocumentSpec:
    raw = metadata.get("document")
    if raw is None:
        raise GenerationError(
            "mock provider requires request.metadata['document'] (document spec JSON)"
        )
    try:
        if isinstance(raw, str):
            return DocumentSpec.model_validate_json(raw)
        return DocumentSpec.model_validate(raw)
    except ValueError as exc:
        raise GenerationError(f"mock provider could not parse document metadata: {exc}") from exc


# ---------------------------------------------------------------------------
# Document rendering
# ---------------------------------------------------------------------------


def _build_document_payload(scenario: Scenario, document: DocumentSpec) -> dict[str, Any]:
    language = scenario.language
    clauses = [
        {
            "clause_id": clause.clause_id,
            "heading": clause.heading(language),
            "text": _clause_text(scenario, document, clause, language),
        }
        for clause in document.clauses
    ]
    return {"document_id": document.document_id, "clauses": clauses}


def _clause_text(
    scenario: Scenario, document: DocumentSpec, clause: ClauseSpec, language: Language
) -> str:
    anomalies = _clause_anomalies(scenario, document, clause)
    facts = [scenario.fact(fact_id) for fact_id in clause.fact_ids]
    missing_ids = {
        fact_id
        for anomaly in anomalies
        if anomaly.kind is AnomalyKind.MISSING_INFORMATION
        for fact_id in anomaly.fact_ids
    }
    expressible = [fact for fact in facts if fact.fact_id not in missing_ids]
    missing = [fact for fact in facts if fact.fact_id in missing_ids]

    parts: list[str] = []
    if document.clauses and clause.clause_id == document.clauses[0].clause_id:
        if document.kind is DocKind.MAIN_AGREEMENT:
            intro = _party_intro(scenario, language)
        else:
            intro = _document_intro(scenario, document, language)
        if intro:
            parts.append(intro)
    if expressible:
        parts.append(_facts_sentence(scenario, document, clause, expressible, anomalies, language))
    if missing:
        parts.append(_missing_information_sentence(missing, language))
    if not parts:
        parts.append(_filler_sentence(clause, language))

    notes: list[str] = []
    notes.extend(_supersession_notes(scenario, expressible, language))
    notes.extend(_anomaly_notes(scenario, clause, anomalies, language))
    for note in dict.fromkeys(notes):
        if note and note not in parts:
            parts.append(note)

    return " ".join(parts).strip() or _filler_sentence(clause, language)


def _facts_sentence(
    scenario: Scenario,
    document: DocumentSpec,
    clause: ClauseSpec,
    facts: list[Fact],
    anomalies: list[Anomaly],
    language: Language,
) -> str:
    if clause.mode is ClauseMode.CONTRADICTION:
        facts_text = _contradiction_text(scenario, facts, anomalies, language)
    else:
        fragments = [_fact_fragment(scenario, fact, language, mode=clause.mode) for fact in facts]
        facts_text = _join_list(fragments, language)

    distractor_text = _distractor_text(scenario, facts, language)
    template_mode = clause.mode
    if template_mode is ClauseMode.DISTRACTOR and not distractor_text:
        # No numeric near-miss can be derived; keep the facts instead of
        # silently dropping them from the document.
        template_mode = ClauseMode.EXPLICIT

    template = _template(template_mode, scenario.contract_id, clause.clause_id, language)
    return template.format(
        facts=facts_text,
        heading=_lower_first(clause.heading(language)),
        condition=_condition_text(scenario, clause, language),
        exception_condition=_exception_condition_text(scenario, clause, language),
        reference=_reference_text(scenario, document, clause, language),
        distractor=distractor_text,
    )


def _filler_sentence(clause: ClauseSpec, language: Language) -> str:
    index = _stable_index(clause.clause_id, "filler") % len(_FILLERS)
    template = _FILLERS[index]
    text = template[0] if language is Language.SV else template[1]
    return text.format(heading=_lower_first(clause.heading(language)))


def _template(mode: ClauseMode, contract_id: str, clause_id: str, language: Language) -> str:
    options = _TEMPLATES[mode]
    index = _stable_index(contract_id, clause_id, mode.value, "template") % len(options)
    sv, en = options[index]
    return sv if language is Language.SV else en


# ---------------------------------------------------------------------------
# Fact rendering
# ---------------------------------------------------------------------------


def _fact_fragment(scenario: Scenario, fact: Fact, language: Language, *, mode: ClauseMode) -> str:
    label = _lower_first(_label(fact, language))
    surface = _surface(scenario, fact, language, mode=mode)
    if mode is ClauseMode.DEFINITION:
        quoted = _quote(label, language)
        if language is Language.SV:
            return f"{quoted} avser {surface}"
        return f"{quoted} means {surface}"
    return f"{label} {_value_verb(fact, language)} {surface}"


def _surface(scenario: Scenario, fact: Fact, language: Language, *, mode: ClauseMode) -> str:
    if mode is ClauseMode.PARAPHRASE and fact.paraphrase_surfaces:
        # A paraphrase clause must use a verified paraphrase surface, never an
        # explicit form, so the deterministic semantic checker sees the intended
        # (non-verbatim) rendering.
        options = [*fact.paraphrase_surfaces]
    elif fact.explicit_surfaces:
        # Deterministic validation checks explicit clauses for a verbatim
        # explicit surface, so paraphrases are only a last resort here.
        options = [*fact.explicit_surfaces]
    else:
        word_form = _number_word_surface(fact, language)
        options = [word_form] if word_form else [*fact.paraphrase_surfaces]
    if options:
        index = _stable_index(scenario.contract_id, fact.fact_id, mode.value, "surface") % len(
            options
        )
        return options[index]
    formatted = _format_value(scenario, fact, language)
    return formatted or _label(fact, language)


def _value_verb(fact: Fact, language: Language) -> str:
    if fact.kind in (FactKind.MONEY, FactKind.PERCENTAGE):
        sv, en = _MONEY_VERBS
    elif fact.kind in (FactKind.DURATION, FactKind.DATE, FactKind.INTEGER, FactKind.DECIMAL):
        sv, en = _QUANTITY_VERBS
    else:
        sv, en = _PLAIN_VERBS
    options = sv if language is Language.SV else en
    index = _stable_index(fact.fact_id, fact.kind.value, language.value, "verb") % len(options)
    return options[index]


def _format_value(scenario: Scenario, fact: Fact, language: Language) -> str:
    value = fact.value
    if fact.kind is FactKind.ENTITY and fact.entity_id is not None:
        for party in scenario.parties:
            if party.party_id == fact.entity_id:
                return party.name
    if fact.kind is FactKind.MONEY:
        return _format_amount(value, fact.currency)
    if fact.kind is FactKind.PERCENTAGE:
        unit = fact.unit or "%"
        return f"{_format_number(value)} {unit}"
    if fact.kind is FactKind.DURATION:
        return f"{_format_number(value)} {_unit_word(fact.unit, language)}".strip()
    if fact.kind is FactKind.DATE:
        return str(value)
    if fact.kind is FactKind.BOOLEAN:
        if language is Language.SV:
            return "ja" if value else "nej"
        return "yes" if value else "no"
    if fact.kind is FactKind.LIST and isinstance(value, (list, tuple)):
        return _join_list([str(item) for item in value], language)
    if value is None:
        return ""
    return str(value)


def _format_amount(value: Any, currency: str | None) -> str:
    number = _format_number(value)
    if currency:
        return f"{number} {currency}"
    return number


def _format_number(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}".replace(",", " ")
        formatted = f"{value:,.2f}".replace(",", " ")
        return formatted.replace(".", ",")
    return str(value)


def _unit_word(unit: str | None, language: Language) -> str:
    if not unit:
        return ""
    pair = _UNIT_WORDS.get(unit.lower())
    if pair is None:
        return unit
    return pair[0] if language is Language.SV else pair[1]


def _number_word_surface(fact: Fact, language: Language) -> str | None:
    if language is not Language.SV:
        return None
    value = fact.value
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value <= 999_999:
        return None
    words = _sv_number_words(value)
    if fact.kind is FactKind.DURATION:
        return f"{words} {_unit_word(fact.unit, language)}".strip()
    if fact.kind is FactKind.MONEY:
        suffix = "kronor" if (fact.currency or "").upper() == "SEK" else (fact.currency or "")
        return f"{words} {suffix}".strip()
    if fact.kind is FactKind.PERCENTAGE:
        return f"{words} procent"
    if fact.kind in (FactKind.INTEGER, FactKind.DECIMAL):
        return words
    return None


def _sv_number_words(value: int) -> str:
    if value < 20:
        return _SV_ONES[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        return _SV_TENS[tens * 10] + (_SV_ONES[ones] if ones else "")
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        prefix = "etthundra" if hundreds == 1 else f"{_SV_ONES[hundreds]}hundra"
        return prefix + (_sv_number_words(rest) if rest else "")
    if value < 1_000_000:
        thousands, rest = divmod(value, 1000)
        prefix = "ettusen" if thousands == 1 else f"{_sv_number_words(thousands)}tusen"
        return prefix + (_sv_number_words(rest) if rest else "")
    return str(value)


# ---------------------------------------------------------------------------
# Distractors
# ---------------------------------------------------------------------------


def _distractor_text(scenario: Scenario, facts: list[Fact], language: Language) -> str:
    phrases = []
    for fact in facts:
        phrase = _distractor_phrase(scenario, fact, language)
        if phrase:
            phrases.append(phrase)
    return _join_list(phrases, language)


def _distractor_phrase(scenario: Scenario, fact: Fact, language: Language) -> str | None:
    wrong = _wrong_value(fact, language)
    if wrong is None:
        return None
    label = _lower_first(_label(fact, language))
    if language is Language.SV:
        return f"{label} om {wrong}"
    return f"{label} of {wrong}"


def _wrong_value(fact: Fact, language: Language) -> str | None:
    value = fact.value
    if isinstance(value, bool):
        return None
    if fact.kind is FactKind.MONEY:
        if isinstance(value, (int, float)) and value > 0:
            return _format_amount(round(value * 0.8), fact.currency)
        return None
    if fact.kind is FactKind.PERCENTAGE:
        if isinstance(value, (int, float)) and value > 0:
            wrong = value + 5 if value <= 90 else value - 5
            return f"{_format_number(wrong)} %"
        return None
    if fact.kind is FactKind.DURATION:
        if isinstance(value, int) and value > 0:
            return f"{value + 1} {_unit_word(fact.unit, language)}".strip()
        return None
    if fact.kind is FactKind.INTEGER:
        if isinstance(value, int):
            return str(value + 1)
        return None
    if fact.kind is FactKind.DECIMAL:
        if isinstance(value, float):
            return _format_number(round(value + 1, 2))
        return None
    if fact.kind is FactKind.DATE:
        try:
            parsed = date.fromisoformat(str(value))
        except ValueError:
            return None
        return (parsed - timedelta(days=30)).isoformat()
    return None


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------


def _clause_anomalies(
    scenario: Scenario, document: DocumentSpec, clause: ClauseSpec
) -> list[Anomaly]:
    clause_fact_ids = set(clause.fact_ids)
    selected = []
    first_clause = document.clauses[0].clause_id if document.clauses else None
    for anomaly in scenario.anomalies:
        if (
            clause_fact_ids & set(anomaly.fact_ids)
            or (anomaly.governing_fact_id in clause_fact_ids)
            or (
                not anomaly.fact_ids
                and not anomaly.governing_fact_id
                and document.document_id in anomaly.document_ids
                and clause.clause_id == first_clause
            )
        ):
            selected.append(anomaly)
    return selected


def _missing_information_sentence(facts: list[Fact], language: Language) -> str:
    labels = _join_list(
        [_quote(_lower_first(_label(fact, language)), language) for fact in facts], language
    )
    if language is Language.SV:
        return f"Parterna har inte reglerat {labels}; frågan ska hanteras separat."
    return f"The parties have not regulated {labels}; the matter shall be handled separately."


def _supersession_notes(scenario: Scenario, facts: list[Fact], language: Language) -> list[str]:
    notes = []
    for fact in facts:
        if not fact.is_superseded or not fact.superseded_by:
            continue
        try:
            successor = scenario.fact(fact.superseded_by)
        except KeyError:  # pragma: no cover - scenario validation forbids this
            continue
        old_surface = _surface(scenario, fact, language, mode=ClauseMode.EXPLICIT)
        new_surface = _surface(scenario, successor, language, mode=ClauseMode.EXPLICIT)
        if language is Language.SV:
            notes.append(f"Det tidigare värdet {old_surface} har ersatts av {new_surface}.")
        else:
            notes.append(f"The earlier value {old_surface} has been replaced by {new_surface}.")
    return notes


def _anomaly_notes(
    scenario: Scenario,
    clause: ClauseSpec,
    anomalies: list[Anomaly],
    language: Language,
) -> list[str]:
    notes: list[str] = []
    for anomaly in anomalies:
        if anomaly.kind is AnomalyKind.MISSING_INFORMATION:
            continue
        if (
            anomaly.kind is AnomalyKind.INTENTIONAL_CONTRADICTION
            and clause.mode is ClauseMode.CONTRADICTION
        ):
            continue
        if anomaly.kind is AnomalyKind.SUPERSESSION:
            superseded = [
                fact
                for fact_id in anomaly.fact_ids
                if fact_id in scenario.fact_ids
                for fact in [scenario.fact(fact_id)]
                if fact.is_superseded
            ]
            if superseded:
                notes.extend(_supersession_notes(scenario, superseded, language))
                continue
        described = _ensure_period(_anomaly_description(anomaly, language))
        if described:
            notes.append(described)
            continue
        surfaces = _anomaly_surfaces(scenario, anomaly, language)
        if anomaly.kind is AnomalyKind.INTENTIONAL_CONTRADICTION and surfaces:
            if language is Language.SV:
                notes.append(
                    f"En annan bestämmelse anger {_join_list(surfaces, language)}, "
                    "vilket strider mot vad som anges här."
                )
            else:
                notes.append(
                    f"Another provision states {_join_list(surfaces, language)}, "
                    "which conflicts with what is stated here."
                )
        elif anomaly.kind is AnomalyKind.EXCEPTION and surfaces:
            if language is Language.SV:
                notes.append(f"Undantag gäller för {_join_list(surfaces, language)}.")
            else:
                notes.append(f"An exception applies to {_join_list(surfaces, language)}.")
        elif anomaly.kind is AnomalyKind.AMBIGUITY:
            if language is Language.SV:
                notes.append("Det är inte entydigt vad som avses med bestämmelsen.")
            else:
                notes.append("It is not unambiguous what the provision refers to.")
    return notes


def _anomaly_description(anomaly: Anomaly, language: Language) -> str:
    if language is Language.SV:
        return anomaly.description_sv.strip()
    return anomaly.description_en.strip() or anomaly.description_sv.strip()


def _anomaly_surfaces(scenario: Scenario, anomaly: Anomaly, language: Language) -> list[str]:
    fact_ids = list(anomaly.fact_ids)
    if anomaly.governing_fact_id:
        fact_ids.append(anomaly.governing_fact_id)
    surfaces = []
    for fact_id in dict.fromkeys(fact_ids):
        if fact_id not in scenario.fact_ids:
            continue
        fact = scenario.fact(fact_id)
        surfaces.append(_surface(scenario, fact, language, mode=ClauseMode.EXPLICIT))
    return surfaces


# ---------------------------------------------------------------------------
# Parties, references and conditions
# ---------------------------------------------------------------------------


def _party_intro(scenario: Scenario, language: Language) -> str:
    phrases = [_party_phrase(party, language) for party in scenario.parties]
    if not phrases:
        return ""
    joined = _join_list(phrases, language)
    if language is Language.SV:
        return f"Detta avtal har ingåtts mellan {joined}."
    return f"This agreement has been entered into between {joined}."


def _party_phrase(party: Any, language: Language) -> str:
    phrase = f"{party.name} ({party.role})"
    if not party.org_number:
        return phrase
    if language is Language.SV:
        return f"{phrase}, org.nr {party.org_number}"
    return f"{phrase}, company registration number {party.org_number}"


def _document_intro(scenario: Scenario, document: DocumentSpec, language: Language) -> str:
    titles = []
    for target in document.references_documents:
        try:
            titles.append(_quote(scenario.document(target).title(language), language))
        except KeyError:
            continue
    if titles and language is Language.SV:
        return (
            "Detta dokument utgör en del av avtalet och hänvisar till "
            f"{_join_list(titles, language)}."
        )
    if titles:
        return (
            "This document forms part of the agreement and refers to "
            f"{_join_list(titles, language)}."
        )
    if language is Language.SV:
        return "Detta dokument utgör en del av avtalet."
    return "This document forms part of the agreement."


def _condition_text(scenario: Scenario, clause: ClauseSpec, language: Language) -> str:
    headings = _dependency_headings(scenario, clause, language)
    if headings:
        quoted = [_quote(heading, language) for heading in headings]
        if language is Language.SV:
            noun = "klausulen" if len(quoted) == 1 else "klausulerna"
            return f"villkoren i {noun} {_join_list(quoted, language)} är uppfyllda"
        noun = "clause" if len(quoted) == 1 else "clauses"
        return f"the conditions in {noun} {_join_list(quoted, language)} are met"
    if language is Language.SV:
        return "förutsättningarna i detta avtal är uppfyllda"
    return "the conditions in this agreement are met"


def _exception_condition_text(scenario: Scenario, clause: ClauseSpec, language: Language) -> str:
    headings = _dependency_headings(scenario, clause, language)
    if headings:
        quoted = [_quote(heading, language) for heading in headings]
        if language is Language.SV:
            noun = "klausulen" if len(quoted) == 1 else "klausulerna"
            return f"{noun} {_join_list(quoted, language)}"
        noun = "clause" if len(quoted) == 1 else "clauses"
        return f"{noun} {_join_list(quoted, language)}"
    return "huvudregeln" if language is Language.SV else "the general rule"


def _reference_text(
    scenario: Scenario, document: DocumentSpec, clause: ClauseSpec, language: Language
) -> str:
    targets: list[str] = []
    for clause_id in clause.reference_to:
        try:
            heading = scenario.clause(clause_id).heading(language)
        except KeyError:  # pragma: no cover - scenario validation forbids this
            continue
        if language is Language.SV:
            targets.append(f"klausulen {_quote(heading, language)}")
        else:
            targets.append(f"clause {_quote(heading, language)}")
    for document_id in document.references_documents:
        try:
            title = scenario.document(document_id).title(language)
        except KeyError:
            continue
        targets.append(_quote(title, language))
    if not targets:
        return (
            "det aktuella avtalsdokumentet"
            if language is Language.SV
            else ("the relevant agreement document")
        )
    return _join_list(targets, language)


def _dependency_headings(scenario: Scenario, clause: ClauseSpec, language: Language) -> list[str]:
    headings = []
    for dependency in clause.depends_on:
        try:
            headings.append(scenario.clause(dependency).heading(language))
        except KeyError:  # pragma: no cover - scenario validation forbids this
            continue
    return headings


def _contradiction_text(
    scenario: Scenario,
    facts: list[Fact],
    anomalies: list[Anomaly],
    language: Language,
) -> str:
    candidates = list(facts)
    for anomaly in anomalies:
        for fact_id in anomaly.fact_ids:
            if fact_id in scenario.fact_ids:
                candidates.append(scenario.fact(fact_id))
        if anomaly.governing_fact_id and anomaly.governing_fact_id in scenario.fact_ids:
            candidates.append(scenario.fact(anomaly.governing_fact_id))

    fragments: list[str] = []
    seen: set[str] = set()
    for fact in candidates:
        if fact.fact_id in seen:
            continue
        seen.add(fact.fact_id)
        fragments.append(_fact_fragment(scenario, fact, language, mode=ClauseMode.EXPLICIT))

    if not fragments:
        if language is Language.SV:
            return "bestämmelserna är inte samstämmiga"
        return "the provisions are not consistent"
    if len(fragments) == 1:
        suffix = (
            ", samtidigt som en motstridig uppgift lämnats"
            if language is Language.SV
            else ", while a conflicting statement is made"
        )
        return fragments[0] + suffix
    separator = " samtidigt som " if language is Language.SV else " while "
    return separator.join(fragments)


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------


def _label(fact: Fact, language: Language) -> str:
    if language is Language.EN and fact.label_en:
        return fact.label_en
    if fact.label_sv:
        return fact.label_sv
    if fact.label_en:
        return fact.label_en
    # Never leak a fact id into clause text; valid scenarios always carry labels.
    return "villkoret" if language is Language.SV else "the term"


def _quote(text: str, language: Language) -> str:
    if language is Language.SV:
        return f"”{text}”"
    return f'"{text}"'


def _join_list(items: list[str], language: Language) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if language is Language.SV:
        return ", ".join(items[:-1]) + " och " + items[-1]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _lower_first(text: str) -> str:
    if not text:
        return text
    return text[0].lower() + text[1:]


def _ensure_period(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    if text.endswith((".", "!", "?")):
        return text
    return text + "."


def _stable_index(*parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:12], 16)
