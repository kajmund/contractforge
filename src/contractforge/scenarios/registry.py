"""Contract type definitions loaded from JSON data files.

Contract types are pure data. Every type-specific detail -- parties and roles,
fact specifications, clause plan, package composition (main agreement, appendix,
amendment), variation dimensions, distractor material and the anomaly material
used at difficulty 5-6 -- lives in ``scenarios/definitions/*.json`` and is parsed
into the strict pydantic models below. :class:`ScenarioGenerator` never branches
on the contract type; it only reads these models.

Design rules:

* ``extra="forbid"`` everywhere: a typo in a definition file is an error, never
  silently ignored.
* All ten contract types must be present. A partial definition set is a
  configuration error and raises :class:`ScenarioError`.
* Definition files are validated against each other (fact ids, clause ids,
  party references, package composition) so data mistakes fail at load time
  instead of producing invalid scenarios later.
* Organisationsnummer are clearly synthetic: ``org_number_series`` must be a
  ``5590xx`` prefix and the generator appends four deterministic digits.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from ..errors import ScenarioError
from ..models import (
    ClauseMode,
    ContractType,
    DocumentQuality,
    FactKind,
    StrictModel,
    VariationStyle,
)

DEFAULT_DEFINITIONS_DIR = Path(__file__).resolve().parent / "definitions"

#: Synthetic organisationsnummer are built from a ``5590xx`` series plus four
#: deterministic digits. No real organisation can own this fixed test series.
ORG_NUMBER_SERIES_RE = re.compile(r"^5590\d{2}$")
SYNTHETIC_ORG_NUMBER_RE = re.compile(r"^5590\d{2}-\d{4}$")

_WORD_RE = re.compile(r"\S+")


class FactVariant(StrictModel):
    """One allowed rendering of a fact, with explicit and paraphrase surfaces.

    ``value`` is the machine-readable truth. ``explicit_surfaces`` are literal
    textual forms the generated contract may contain (level 1) and
    ``paraphrase_surfaces`` are verified alternative legal phrasings (level 2).
    """

    value: Any
    explicit_surfaces: list[str] = Field(min_length=1)
    paraphrase_surfaces: list[str] = Field(min_length=1)
    description_sv: str = ""


class PartySpec(StrictModel):
    """A synthetic party template. Names and addresses come from pools."""

    party_id: str
    role: str
    role_sv: str
    role_en: str = ""
    kind: Literal["organisation", "individual"] = "organisation"
    name_pool: list[str] = Field(min_length=1)
    address_pool: list[str] = Field(default_factory=list)
    org_number_series: str | None = None

    @model_validator(mode="after")
    def _validate_party(self) -> PartySpec:
        if self.kind == "organisation":
            if self.org_number_series is None:
                raise ValueError(f"organisation party {self.party_id} needs org_number_series")
            if not ORG_NUMBER_SERIES_RE.fullmatch(self.org_number_series):
                raise ValueError(
                    f"party {self.party_id} org_number_series must match 5590xx, "
                    f"got {self.org_number_series!r}"
                )
        elif self.org_number_series is not None:
            raise ValueError(f"individual party {self.party_id} must not have org_number_series")
        seen: set[str] = set()
        for name in self.name_pool:
            if not name.strip():
                raise ValueError(f"party {self.party_id} has an empty name in name_pool")
            if name in seen:
                raise ValueError(f"party {self.party_id} has duplicate name {name!r}")
            seen.add(name)
        return self


class FactSpec(StrictModel):
    """Definition-level fact template.

    Facts of kind ``entity`` resolve their value from a :class:`PartySpec`
    (``entity_party_id``); every other kind needs at least one
    :class:`FactVariant` so the generator can pick a deterministic value and
    surfaces.
    """

    fact_id: str
    kind: FactKind
    label_sv: str
    label_en: str = ""
    unit: str | None = None
    currency: str | None = None
    entity_party_id: str | None = None
    keywords_sv: list[str] = Field(min_length=1)
    keywords_en: list[str] = Field(min_length=1)
    description_sv: str = ""
    variants: list[FactVariant] = Field(default_factory=list)
    based_on_fact_id: str | None = None

    @model_validator(mode="after")
    def _validate_fact(self) -> FactSpec:
        if not self.label_sv.strip():
            raise ValueError(f"fact {self.fact_id} needs a Swedish label")
        if not self.label_en.strip():
            raise ValueError(f"fact {self.fact_id} needs an English label")
        if self.kind == FactKind.ENTITY:
            if self.entity_party_id is None:
                raise ValueError(f"entity fact {self.fact_id} needs entity_party_id")
            if self.variants:
                raise ValueError(f"entity fact {self.fact_id} must not carry variants")
            return self
        if self.entity_party_id is not None:
            raise ValueError(
                f"fact {self.fact_id} has entity_party_id but kind {self.kind.value}"
            )
        if not self.variants:
            raise ValueError(f"fact {self.fact_id} needs at least one variant")
        if self.kind == FactKind.MONEY and not self.currency:
            raise ValueError(f"money fact {self.fact_id} needs currency")
        if self.kind == FactKind.DURATION and not self.unit:
            raise ValueError(f"duration fact {self.fact_id} needs unit")
        if self.kind == FactKind.PERCENTAGE and self.unit != "percent":
            raise ValueError(f"percentage fact {self.fact_id} needs unit 'percent'")
        return self


class ClausePlanEntry(StrictModel):
    """One planned clause. ``clause_id`` is stable inside the definition.

    ``optional`` clauses may be trimmed when the request asks for fewer clauses.
    ``distributed_facts`` marks the appendix clause that receives facts moved
    out of the main agreement at difficulty >= 4.
    """

    clause_id: str
    heading_sv: str
    heading_en: str = ""
    fact_ids: list[str] = Field(default_factory=list)
    mode: ClauseMode = ClauseMode.EXPLICIT
    optional: bool = False
    distributed_facts: bool = False
    depends_on: list[str] = Field(default_factory=list)
    reference_to: list[str] = Field(default_factory=list)
    instruction_sv: str = ""
    instruction_en: str = ""
    min_difficulty: int = Field(default=1, ge=1, le=6)
    max_difficulty: int = Field(default=6, ge=1, le=6)

    @model_validator(mode="after")
    def _validate_clause(self) -> ClausePlanEntry:
        if not self.heading_sv.strip():
            raise ValueError(f"clause {self.clause_id} needs a Swedish heading")
        if self.min_difficulty > self.max_difficulty:
            raise ValueError(f"clause {self.clause_id} has min_difficulty > max_difficulty")
        return self


class DefinitionDocument(StrictModel):
    """Appendix / amendment metadata from a definition."""

    document_id: str
    title_sv: str
    title_en: str = ""
    filename_stem: str


class PackageSpec(StrictModel):
    """Document package composition.

    The appendix is added for difficulty 3-6 and the amendment for difficulty
    5-6 when ``ScenarioRequest.package`` is true. When it is false, the extra
    clause plans are folded into the single main agreement instead.
    """

    appendix: DefinitionDocument | None = None
    amendment: DefinitionDocument | None = None


class VariationDimensions(StrictModel):
    """Seeded variation dimensions specific to the contract type."""

    styles: list[VariationStyle] = Field(
        default_factory=lambda: [VariationStyle.FORMAL], min_length=1
    )
    document_qualities: list[DocumentQuality] = Field(
        default_factory=lambda: [DocumentQuality.CLEAN], min_length=1
    )


class ContradictionSpec(StrictModel):
    """Material for the deliberate contradiction at difficulty 5."""

    base_fact_id: str
    fact: FactSpec
    description_sv: str
    description_en: str = ""


class ExceptionSpec(StrictModel):
    """Material for the deliberate exception at difficulty 5."""

    base_fact_id: str
    fact: FactSpec
    description_sv: str
    description_en: str = ""


class MissingInfoSpec(StrictModel):
    """Material for the missing-information level (difficulty 6)."""

    missing_documents: list[str] = Field(min_length=1)
    insufficient_evidence_fact_id: str
    conditional_fact: FactSpec
    conditional_governing_fact_id: str
    description_sv: str
    conditional_description_sv: str


class ContractTypeDefinition(StrictModel):
    """Complete data-driven definition of one contract type."""

    contract_type: ContractType
    definition_version: str = "1.0"
    title_sv: str
    title_en: str = ""
    description_sv: str = ""
    parties: list[PartySpec] = Field(min_length=2)
    facts: list[FactSpec] = Field(min_length=8)
    main_clauses: list[ClausePlanEntry] = Field(min_length=8, max_length=14)
    extra_main_clauses: list[ClausePlanEntry] = Field(default_factory=list)
    appendix_clauses: list[ClausePlanEntry] = Field(default_factory=list)
    amendment_clauses: list[ClausePlanEntry] = Field(default_factory=list)
    package: PackageSpec
    variation: VariationDimensions = Field(default_factory=VariationDimensions)
    distractor_facts: list[FactSpec] = Field(min_length=1)
    contradiction: ContradictionSpec
    exception: ExceptionSpec
    missing_information: MissingInfoSpec

    # -- helpers ---------------------------------------------------------
    @property
    def base_fact_ids(self) -> list[str]:
        return [fact.fact_id for fact in self.facts]

    @property
    def distractor_fact_ids(self) -> list[str]:
        return [fact.fact_id for fact in self.distractor_facts]

    def all_fact_ids(self) -> list[str]:
        ids = self.base_fact_ids + self.distractor_fact_ids
        ids.append(self.contradiction.fact.fact_id)
        ids.append(self.exception.fact.fact_id)
        ids.append(self.missing_information.conditional_fact.fact_id)
        return ids

    # -- validation ------------------------------------------------------
    @model_validator(mode="after")
    def _validate_definition(self) -> ContractTypeDefinition:
        errors: list[str] = []

        def _unique(values: list[str], what: str) -> None:
            seen: set[str] = set()
            for value in values:
                if value in seen:
                    errors.append(f"duplicate {what}: {value}")
                seen.add(value)

        party_ids = [party.party_id for party in self.parties]
        _unique(party_ids, "party_id")
        fact_ids = self.all_fact_ids()
        _unique(fact_ids, "fact_id")
        clause_ids = [
            clause.clause_id
            for plan in (
                self.main_clauses,
                self.extra_main_clauses,
                self.appendix_clauses,
                self.amendment_clauses,
            )
            for clause in plan
        ]
        _unique(clause_ids, "clause_id")

        base_ids = set(self.base_fact_ids)
        distractor_ids = set(self.distractor_fact_ids)
        conflict_id = self.contradiction.fact.fact_id
        exception_id = self.exception.fact.fact_id
        conditional_id = self.missing_information.conditional_fact.fact_id
        known_fact_ids = set(fact_ids)
        known_clause_ids = set(clause_ids)
        party_id_set = set(party_ids)

        for fact in [
            *self.facts,
            *self.distractor_facts,
            self.contradiction.fact,
            self.exception.fact,
            self.missing_information.conditional_fact,
        ]:
            if fact.entity_party_id is not None and fact.entity_party_id not in party_id_set:
                errors.append(
                    f"fact {fact.fact_id} references unknown party {fact.entity_party_id}"
                )

        for fact in self.distractor_facts:
            if fact.based_on_fact_id is not None and fact.based_on_fact_id not in base_ids:
                errors.append(
                    f"distractor fact {fact.fact_id} is based on unknown fact "
                    f"{fact.based_on_fact_id}"
                )

        # Main and extra clauses may only restate always-present base facts.
        for plan in (self.main_clauses, self.extra_main_clauses):
            for clause in plan:
                for fact_id in clause.fact_ids:
                    if fact_id not in base_ids:
                        errors.append(
                            f"main clause {clause.clause_id} references non-base fact {fact_id}"
                        )
                for dependency in clause.depends_on:
                    if dependency not in known_clause_ids:
                        errors.append(
                            f"clause {clause.clause_id} depends on unknown clause {dependency}"
                        )

        appendix_allowed = base_ids | distractor_ids
        for clause in self.appendix_clauses:
            for fact_id in clause.fact_ids:
                if fact_id not in appendix_allowed:
                    errors.append(
                        f"appendix clause {clause.clause_id} references unknown fact {fact_id}"
                    )

        amendment_allowed = base_ids | {conflict_id, exception_id, conditional_id}
        for clause in self.amendment_clauses:
            for fact_id in clause.fact_ids:
                if fact_id not in amendment_allowed:
                    errors.append(
                        f"amendment clause {clause.clause_id} references unknown fact {fact_id}"
                    )

        covered_facts = {
            fact_id for clause in self.main_clauses for fact_id in clause.fact_ids
        }
        for fact_id in self.base_fact_ids:
            if fact_id not in covered_facts:
                errors.append(f"base fact {fact_id} is not used by any main clause")

        if self.appendix_clauses:
            if self.package.appendix is None:
                errors.append("appendix_clauses are defined but package.appendix is missing")
            if not any(clause.distributed_facts for clause in self.appendix_clauses):
                errors.append("appendix_clauses need one clause with distributed_facts=true")
        if self.amendment_clauses and self.package.amendment is None:
            errors.append("amendment_clauses are defined but package.amendment is missing")

        for difficulty in (5, 6):
            if not any(
                clause.min_difficulty <= difficulty <= clause.max_difficulty
                for clause in self.amendment_clauses
            ):
                errors.append(f"amendment_clauses do not cover difficulty {difficulty}")

        if self.contradiction.base_fact_id not in base_ids:
            errors.append(
                f"contradiction base fact {self.contradiction.base_fact_id} is not a base fact"
            )
        if self.exception.base_fact_id not in base_ids:
            errors.append(f"exception base fact {self.exception.base_fact_id} is not a base fact")
        if self.missing_information.conditional_governing_fact_id not in base_ids:
            errors.append(
                "missing_information.conditional_governing_fact_id "
                f"{self.missing_information.conditional_governing_fact_id} is not a base fact"
            )
        if self.missing_information.insufficient_evidence_fact_id not in known_fact_ids:
            errors.append(
                "missing_information.insufficient_evidence_fact_id "
                f"{self.missing_information.insufficient_evidence_fact_id} is not a fact"
            )

        package_document_ids = {
            document.document_id
            for document in (self.package.appendix, self.package.amendment)
            if document is not None
        }
        for missing_document in self.missing_information.missing_documents:
            if missing_document in package_document_ids:
                errors.append(
                    f"missing document {missing_document} is also part of the package"
                )
            if not _WORD_RE.search(missing_document):
                errors.append(f"invalid missing document id {missing_document!r}")

        if errors:
            raise ValueError(
                f"invalid definition for {self.contract_type.value}: " + "; ".join(errors)
            )
        return self


def _load_definition(path: Path) -> ContractTypeDefinition:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScenarioError(f"could not read contract definition {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"contract definition {path.name} is not valid JSON: {exc}") from exc
    try:
        return ContractTypeDefinition.model_validate(payload)
    except ValueError as exc:
        raise ScenarioError(f"invalid contract definition {path.name}: {exc}") from exc


class ContractTypeRegistry:
    """Read-only registry of contract type definitions."""

    def __init__(self, definitions: dict[ContractType, ContractTypeDefinition]) -> None:
        self._definitions = dict(definitions)

    @classmethod
    def load(cls, definitions_dir: Path | None = None) -> ContractTypeRegistry:
        directory = DEFAULT_DEFINITIONS_DIR if definitions_dir is None else Path(definitions_dir)
        if not directory.is_dir():
            raise ScenarioError(f"contract definition directory not found: {directory}")
        paths = sorted(directory.glob("*.json"))
        if not paths:
            raise ScenarioError(f"no contract definitions (*.json) in {directory}")
        definitions: dict[ContractType, ContractTypeDefinition] = {}
        for path in paths:
            definition = _load_definition(path)
            if definition.contract_type in definitions:
                raise ScenarioError(
                    f"duplicate definition for contract type {definition.contract_type.value} "
                    f"in {directory}"
                )
            definitions[definition.contract_type] = definition
        missing = [
            contract_type.value
            for contract_type in ContractType
            if contract_type not in definitions
        ]
        if missing:
            raise ScenarioError(
                f"missing contract definitions in {directory}: {', '.join(missing)}"
            )
        return cls(definitions)

    def get(self, contract_type: ContractType | str) -> ContractTypeDefinition:
        if isinstance(contract_type, ContractType):
            normalized = contract_type
        else:
            try:
                normalized = ContractType(str(contract_type))
            except ValueError as exc:
                raise ScenarioError(
                    f"unknown contract type {contract_type!r}; known types: "
                    f"{', '.join(self.types())}"
                ) from exc
        definition = self._definitions.get(normalized)
        if definition is None:
            raise ScenarioError(
                f"no definition for contract type {normalized.value}; known types: "
                f"{', '.join(self.types())}"
            )
        return definition

    def types(self) -> list[str]:
        return [
            contract_type.value
            for contract_type in ContractType
            if contract_type in self._definitions
        ]

    def definitions(self) -> list[ContractTypeDefinition]:
        return [
            self._definitions[contract_type]
            for contract_type in ContractType
            if contract_type in self._definitions
        ]

    def __len__(self) -> int:
        return len(self._definitions)

    def __contains__(self, contract_type: object) -> bool:
        if isinstance(contract_type, ContractType):
            return contract_type in self._definitions
        if isinstance(contract_type, str):
            try:
                return ContractType(contract_type) in self._definitions
            except ValueError:
                return False
        return False


def load_registry(definitions_dir: Path | None = None) -> ContractTypeRegistry:
    """Load the contract type registry from ``definitions_dir`` (or the package default)."""

    return ContractTypeRegistry.load(definitions_dir)


__all__ = [
    "DEFAULT_DEFINITIONS_DIR",
    "ORG_NUMBER_SERIES_RE",
    "SYNTHETIC_ORG_NUMBER_RE",
    "ClausePlanEntry",
    "ContractTypeDefinition",
    "ContractTypeRegistry",
    "ContradictionSpec",
    "DefinitionDocument",
    "ExceptionSpec",
    "FactSpec",
    "FactVariant",
    "MissingInfoSpec",
    "PackageSpec",
    "PartySpec",
    "VariationDimensions",
    "load_registry",
]
