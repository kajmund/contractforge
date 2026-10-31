"""Versioned prompt assembly.

Prompt text lives in ``generation/prompts/<PROMPT_VERSION>/*.md`` and is loaded
once per process. ``prompt_sha256()`` exposes a stable digest of the active
prompt version for manifests, so a generated dataset can be tied to exactly the
prompt that produced it.

The user prompt contains everything the model needs: language, jurisdiction,
style/complexity/difficulty, the planned clauses, the facts with their surfaces,
anomalies, references and the exact JSON output contract. It never contains
credentials.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..config import DEFAULT_MODEL, Settings
from ..errors import ConfigError
from ..models import (
    Anomaly,
    DocumentSpec,
    Fact,
    Language,
    LLMRequest,
    Scenario,
)
from ..utils import sha256_text

PROMPT_VERSION = "v1"
MOCK_MODEL = "mock-1"

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts" / PROMPT_VERSION
_PLACEHOLDER_RE = re.compile(r"\[\[[A-Z0-9_]+\]\]")


def _load_prompt(filename: str) -> str:
    path = _PROMPT_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"generation prompt asset is missing: {path}") from exc


_SYSTEM_TEMPLATE = _load_prompt("system.md")
_USER_TEMPLATE = _load_prompt("contract.md")


def prompt_sha256() -> str:
    """Stable digest of the bundled prompt templates for the manifest."""

    payload = "\n".join(
        [
            f"prompt_version={PROMPT_VERSION}",
            "system.md",
            _SYSTEM_TEMPLATE,
            "contract.md",
            _USER_TEMPLATE,
        ]
    )
    return sha256_text(payload)


def build_contract_request(
    scenario: Scenario,
    document: DocumentSpec,
    settings: Settings,
    *,
    attempt: int = 1,
    feedback: list[str] | None = None,
) -> LLMRequest:
    """Assemble the request for exactly one document of a scenario."""

    facts = _document_facts(scenario, document)
    anomalies = _document_anomalies(scenario, document, facts)
    values = _prompt_values(scenario, document, facts, anomalies, attempt, feedback)

    user = _render(_USER_TEMPLATE, values)
    system = _render(_SYSTEM_TEMPLATE, values)
    seed = settings.seed if settings.seed is not None else scenario.seed

    return LLMRequest(
        provider=settings.provider,
        model=_request_model(settings),
        prompt_version=PROMPT_VERSION,
        system=system,
        user=user,
        temperature=settings.temperature,
        top_p=settings.top_p,
        max_tokens=settings.max_tokens,
        seed=seed,
        response_format="json",
        metadata={
            "scenario": scenario.model_dump(mode="json"),
            "document": document.model_dump(mode="json"),
            "attempt": attempt,
            "feedback": list(feedback or []),
        },
    )


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _render(template: str, values: dict[str, str]) -> str:
    expected = {f"[[{name}]]" for name in values}
    missing = sorted(set(_PLACEHOLDER_RE.findall(template)) - expected)
    if missing:
        raise ConfigError(f"prompt template has unresolved placeholder(s): {', '.join(missing)}")
    rendered = template
    for name, value in values.items():
        rendered = rendered.replace(f"[[{name}]]", value)
    return rendered


def _prompt_values(
    scenario: Scenario,
    document: DocumentSpec,
    facts: list[Fact],
    anomalies: list[Anomaly],
    attempt: int,
    feedback: list[str] | None,
) -> dict[str, str]:
    language = scenario.language
    variation = scenario.variation
    return {
        "PROMPT_VERSION": PROMPT_VERSION,
        "LANGUAGE_NAME": _language_name(language),
        "LANGUAGE_CODE": language.value,
        "JURISDICTION": scenario.jurisdiction,
        "CONTRACT_TYPE": scenario.contract_type.value,
        "DOCUMENT_ID": document.document_id,
        "DOCUMENT_TITLE": document.title(language),
        "DOCUMENT_KIND": document.kind.value,
        "DIFFICULTY": str(scenario.difficulty),
        "COMPLEXITY": _complexity(language, scenario.difficulty),
        "STYLE": variation.style.value,
        "DIRECTNESS": variation.directness.value,
        "STRUCTURE": variation.structure.value,
        "DOCUMENT_QUALITY": variation.document_quality.value,
        "DISTRACTOR_COUNT": str(variation.distractor_count),
        "DEPENDENCY_MODES": _mode_list([mode.value for mode in variation.dependency_modes]),
        "REFERENCE_MODES": _mode_list([mode.value for mode in variation.reference_modes]),
        "ATTEMPT": str(attempt),
        "CLAUSE_COUNT": str(len(document.clauses)),
        "PARTIES_JSON": _json([party.model_dump(mode="json") for party in scenario.parties]),
        "VARIATION_JSON": _json(variation.model_dump(mode="json")),
        "CLAUSE_PLAN": _clause_plan(scenario, document, language),
        "FACTS_JSON": _json([fact.model_dump(mode="json") for fact in facts]),
        "ANOMALIES_JSON": _json([anomaly.model_dump(mode="json") for anomaly in anomalies]),
        "MISSING_DOCUMENTS": _mode_list(scenario.missing_documents),
        "DOCUMENT_REFERENCES": _document_references(scenario, document, language),
        "FEEDBACK": _feedback_text(feedback),
        "OUTPUT_SCHEMA": _output_schema(document),
    }


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _mode_list(values: list[str]) -> str:
    return ", ".join(values) if values else "(none)"


def _language_name(language: Language) -> str:
    return "svenska" if language is Language.SV else "engelska"


def _complexity(language: Language, difficulty: int) -> str:
    if language is Language.SV:
        if difficulty <= 2:
            return "enkel, tydliga standardvillkor"
        if difficulty <= 4:
            return "medel, flera samverkande villkor och referenser"
        if difficulty == 5:
            return "avancerad, avsiktliga motstridiga villkor och undantag"
        return "avancerad, avsiktligt saknad information som inte får fyllas i"
    if difficulty <= 2:
        return "simple, clear standard terms"
    if difficulty <= 4:
        return "moderate, several interacting terms and cross-references"
    if difficulty == 5:
        return "advanced, intentional contradictions and exceptions"
    return "advanced, intentional missing information that must not be filled in"


def _clause_plan(scenario: Scenario, document: DocumentSpec, language: Language) -> str:
    other_language = Language.EN if language is Language.SV else Language.SV
    lines = []
    for clause in document.clauses:
        heading = clause.heading(language)
        other_heading = clause.heading(other_language)
        instruction = (
            clause.instruction_en
            if language is Language.EN and clause.instruction_en
            else clause.instruction_sv
        )
        parts = [
            f"{clause.order}. clause_id={clause.clause_id}",
            f"mode={clause.mode.value}",
            f"planned_heading={heading!r}",
        ]
        if other_heading and other_heading != heading:
            parts.append(f"alternative_heading={other_heading!r}")
        if clause.fact_ids:
            parts.append(f"fact_ids={', '.join(clause.fact_ids)}")
        if clause.depends_on:
            parts.append(f"depends_on={', '.join(clause.depends_on)}")
        if clause.reference_to:
            parts.append(f"reference_to={', '.join(clause.reference_to)}")
        if instruction:
            parts.append(f"instruction={instruction}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _document_references(scenario: Scenario, document: DocumentSpec, language: Language) -> str:
    titles = []
    for document_id in document.references_documents:
        try:
            titles.append(scenario.document(document_id).title(language))
        except KeyError:
            titles.append(document_id)
    if not titles:
        return "(none)"
    return ", ".join(titles)


def _feedback_text(feedback: list[str] | None) -> str:
    if not feedback:
        return "(none — no earlier attempt failed)"
    return "\n".join(f"- {item}" for item in feedback)


def _output_schema(document: DocumentSpec) -> str:
    return _json(
        {
            "document_id": document.document_id,
            "clauses": [
                {
                    "clause_id": clause.clause_id,
                    "heading": "<heading>",
                    "text": "<clause text>",
                }
                for clause in document.clauses
            ],
        }
    )


# ---------------------------------------------------------------------------
# Scenario slices included in the prompt
# ---------------------------------------------------------------------------


def _document_facts(scenario: Scenario, document: DocumentSpec) -> list[Fact]:
    selected = {fact_id for clause in document.clauses for fact_id in clause.fact_ids}
    for anomaly in scenario.anomalies:
        if document.document_id in anomaly.document_ids or selected & set(anomaly.fact_ids):
            selected.update(anomaly.fact_ids)
            if anomaly.governing_fact_id:
                selected.add(anomaly.governing_fact_id)
    for fact_id in list(selected):
        try:
            fact = scenario.fact(fact_id)
        except KeyError:  # pragma: no cover - scenario validation forbids this
            continue
        if fact.superseded_by:
            selected.add(fact.superseded_by)
    return [fact for fact in scenario.facts if fact.fact_id in selected]


def _document_anomalies(
    scenario: Scenario, document: DocumentSpec, facts: list[Fact]
) -> list[Anomaly]:
    fact_ids = {fact.fact_id for fact in facts}
    selected = []
    for anomaly in scenario.anomalies:
        if (
            document.document_id in anomaly.document_ids
            or fact_ids & set(anomaly.fact_ids)
            or anomaly.governing_fact_id in fact_ids
        ):
            selected.append(anomaly)
    return selected


def _request_model(settings: Settings) -> str:
    if settings.provider == "mock" and (not settings.model or settings.model == DEFAULT_MODEL):
        return MOCK_MODEL
    return settings.model or DEFAULT_MODEL
