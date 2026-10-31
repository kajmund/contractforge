"""Tests for the data-driven scenario subsystem.

Covers the contract type registry (loading, strict validation, all ten types),
the definitions themselves (richness, synthetic data, no dead facts) and the
generator for every contract type at every difficulty level 1-6, including
determinism, package composition, difficulty features, reference integrity and
synthetic organisationsnummer.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from contractforge.errors import ScenarioError
from contractforge.models import (
    AnomalyKind,
    Answerability,
    ClauseMode,
    ContractType,
    Directness,
    DocKind,
    DocumentQuality,
    FactKind,
    Language,
    ReferenceMode,
    RelationKind,
    ScenarioRequest,
    StructureMode,
    VariationProfile,
)
from contractforge.scenarios.generator import ScenarioGenerator
from contractforge.scenarios.registry import (
    DEFAULT_DEFINITIONS_DIR,
    ORG_NUMBER_SERIES_RE,
    SYNTHETIC_ORG_NUMBER_RE,
    ContractTypeDefinition,
    load_registry,
)

ALL_TYPES = list(ContractType)
DIFFICULTIES = [1, 2, 3, 4, 5, 6]
DEFAULT_SEED = 2026

EXPECTED_PACKAGE_KINDS: dict[int, list[DocKind]] = {
    1: [DocKind.MAIN_AGREEMENT],
    2: [DocKind.MAIN_AGREEMENT],
    3: [DocKind.MAIN_AGREEMENT, DocKind.APPENDIX],
    4: [DocKind.MAIN_AGREEMENT, DocKind.APPENDIX],
    5: [DocKind.MAIN_AGREEMENT, DocKind.APPENDIX, DocKind.AMENDMENT],
    6: [DocKind.MAIN_AGREEMENT, DocKind.APPENDIX, DocKind.AMENDMENT],
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def registry():
    return load_registry()


@pytest.fixture(scope="session")
def generator(registry):
    return ScenarioGenerator(registry)


def make_request(
    contract_type: ContractType | str = ContractType.CONSULTING,
    difficulty: int = 1,
    *,
    seed: int | None = DEFAULT_SEED,
    clauses: int = 10,
    package: bool = True,
    language: Language = Language.SV,
    variation_overrides: dict[str, object] | None = None,
) -> ScenarioRequest:
    return ScenarioRequest(
        contract_type=contract_type,
        difficulty=difficulty,
        seed=seed,
        clauses=clauses,
        package=package,
        language=language,
        variation_overrides=variation_overrides or {},
    )


def generate(
    generator: ScenarioGenerator,
    contract_type: ContractType | str = ContractType.CONSULTING,
    difficulty: int = 1,
    *,
    index: int = 0,
    **kwargs: object,
):
    return generator.generate(make_request(contract_type, difficulty, **kwargs), index)


def all_clauses(scenario):
    return [
        clause for document in scenario.documents for clause in document.clauses
    ]


def fact_ids_in_clauses(scenario) -> set[str]:
    return {fact_id for clause in all_clauses(scenario) for fact_id in clause.fact_ids}


def definition_fact_specs(definition):
    specs = [*definition.facts, *definition.distractor_facts]
    specs.append(definition.contradiction.fact)
    specs.append(definition.exception.fact)
    specs.append(definition.missing_information.conditional_fact)
    return specs


def assert_reference_integrity(scenario) -> None:
    fact_ids = scenario.fact_ids
    party_ids = [party.party_id for party in scenario.parties]
    document_ids = [document.document_id for document in scenario.documents]
    clause_ids = scenario.clause_ids

    assert len(set(fact_ids)) == len(fact_ids)
    assert len(set(party_ids)) == len(party_ids)
    assert len(set(document_ids)) == len(document_ids)
    assert len(set(clause_ids)) == len(clause_ids)
    assert len({relation.relation_id for relation in scenario.relations}) == len(
        scenario.relations
    )
    assert len({anomaly.anomaly_id for anomaly in scenario.anomalies}) == len(
        scenario.anomalies
    )

    fact_id_set = set(fact_ids)
    party_id_set = set(party_ids)
    document_id_set = set(document_ids)
    clause_id_set = set(clause_ids)

    for document in scenario.documents:
        for clause in document.clauses:
            assert clause.document_id == document.document_id
            for fact_id in clause.fact_ids:
                assert fact_id in fact_id_set
            for dependency in clause.depends_on:
                assert dependency in clause_id_set
            for reference in clause.reference_to:
                # A clause may refer to another clause or to a whole document;
                # the document case is what makes the reference text-verifiable.
                assert reference in clause_id_set or reference in document_id_set
            assert len(set(clause.reference_to)) == len(clause.reference_to)
        for referenced in document.references_documents:
            assert referenced in document_id_set or referenced in scenario.missing_documents

    for fact in scenario.facts:
        if fact.entity_id is not None:
            assert fact.entity_id in party_id_set
        assert not fact.is_superseded or fact.superseded_by is not None

    for anomaly in scenario.anomalies:
        for fact_id in anomaly.fact_ids:
            assert fact_id in fact_id_set
        for document_id in anomaly.document_ids:
            assert document_id in document_id_set
        if anomaly.governing_fact_id is not None:
            assert anomaly.governing_fact_id in fact_id_set

    for missing in scenario.missing_documents:
        assert missing not in document_id_set
        assert missing != ""

    for relation in scenario.relations:
        assert relation.source
        assert relation.target


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_loads_all_ten_contract_types(registry):
    assert len(registry) == 10
    assert sorted(registry.types()) == sorted(contract_type.value for contract_type in ALL_TYPES)
    assert len(registry.definitions()) == 10


def test_registry_types_are_stable_enum_order(registry):
    assert registry.types() == [contract_type.value for contract_type in ALL_TYPES]


def test_registry_get_accepts_contract_type_and_string(registry):
    for contract_type in ALL_TYPES:
        definition = registry.get(contract_type)
        assert isinstance(definition, ContractTypeDefinition)
        assert definition.contract_type is contract_type
        assert registry.get(contract_type.value) is definition


def test_registry_unknown_type_raises_scenario_error(registry):
    with pytest.raises(ScenarioError):
        registry.get("not-a-contract-type")
    with pytest.raises(ScenarioError):
        registry.get("")


def test_registry_contains(registry):
    assert ContractType.EMPLOYMENT in registry
    assert "employment" in registry
    assert "unknown" not in registry
    assert 42 not in registry


def test_load_registry_helper_matches_default(registry):
    other = load_registry()
    assert other.types() == registry.types()
    assert other.get(ContractType.NDA).model_dump() == registry.get(ContractType.NDA).model_dump()


def test_registry_load_from_custom_directory(tmp_path: Path):
    target = tmp_path / "definitions"
    shutil.copytree(DEFAULT_DEFINITIONS_DIR, target)
    custom = load_registry(target)
    assert len(custom) == 10
    assert custom.types() == [contract_type.value for contract_type in ALL_TYPES]


def test_registry_rejects_missing_contract_type(tmp_path: Path):
    target = tmp_path / "definitions"
    shutil.copytree(DEFAULT_DEFINITIONS_DIR, target)
    (target / "employment.json").unlink()
    with pytest.raises(ScenarioError, match="missing contract definitions"):
        load_registry(target)


def test_registry_rejects_invalid_json(tmp_path: Path):
    target = tmp_path / "definitions"
    target.mkdir()
    (target / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ScenarioError, match="not valid JSON"):
        load_registry(target)


def test_registry_rejects_duplicate_contract_type(tmp_path: Path):
    target = tmp_path / "definitions"
    shutil.copytree(DEFAULT_DEFINITIONS_DIR, target)
    shutil.copy(target / "employment.json", target / "employment_copy.json")
    with pytest.raises(ScenarioError, match="duplicate definition"):
        load_registry(target)


def test_registry_rejects_empty_directory(tmp_path: Path):
    target = tmp_path / "definitions"
    target.mkdir()
    with pytest.raises(ScenarioError, match="no contract definitions"):
        load_registry(target)


def test_registry_rejects_missing_directory(tmp_path: Path):
    with pytest.raises(ScenarioError, match="not found"):
        load_registry(tmp_path / "does-not-exist")


def test_definition_model_forbids_unknown_fields():
    payload = json.loads(
        (DEFAULT_DEFINITIONS_DIR / "consulting.json").read_text(encoding="utf-8")
    )
    payload["unexpected_field"] = True
    with pytest.raises(PydanticValidationError):
        ContractTypeDefinition.model_validate(payload)


# ---------------------------------------------------------------------------
# Definitions: data quality
# ---------------------------------------------------------------------------


def test_definition_files_cover_all_contract_types():
    paths = sorted(DEFAULT_DEFINITIONS_DIR.glob("*.json"))
    assert len(paths) == 10
    loaded = {path.stem for path in paths}
    assert loaded == {contract_type.value for contract_type in ALL_TYPES}


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_definition_parses_strictly(contract_type):
    path = DEFAULT_DEFINITIONS_DIR / f"{contract_type.value}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    definition = ContractTypeDefinition.model_validate(payload)
    assert definition.contract_type is contract_type
    assert definition.title_sv
    assert definition.title_en
    assert definition.package.appendix is not None
    assert definition.package.amendment is not None


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_definition_richness(registry, contract_type):
    definition = registry.get(contract_type)
    assert 8 <= len(definition.main_clauses) <= 14
    assert len(definition.extra_main_clauses) >= 1
    assert len(definition.facts) >= 8
    assert len(definition.parties) >= 2
    assert len(definition.distractor_facts) >= 2
    assert len(definition.appendix_clauses) >= 2
    assert len(definition.amendment_clauses) >= 2


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_definition_main_clause_ids_are_stable(registry, contract_type):
    definition = registry.get(contract_type)
    main_ids = [clause.clause_id for clause in definition.main_clauses]
    assert main_ids == [f"clause_{index:02d}" for index in range(1, len(main_ids) + 1)]
    all_ids = [
        clause.clause_id
        for plan in (
            definition.main_clauses,
            definition.extra_main_clauses,
            definition.appendix_clauses,
            definition.amendment_clauses,
        )
        for clause in plan
    ]
    assert len(set(all_ids)) == len(all_ids)
    for clause_id in all_ids:
        assert re.fullmatch(r"[a-z0-9_]+", clause_id)


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_definition_every_base_fact_is_used(registry, contract_type):
    definition = registry.get(contract_type)
    used = {
        fact_id
        for clause in definition.main_clauses
        for fact_id in clause.fact_ids
    }
    for fact_id in definition.base_fact_ids:
        assert fact_id in used
    all_fact_ids = [spec.fact_id for spec in definition_fact_specs(definition)]
    assert len(set(all_fact_ids)) == len(all_fact_ids)


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_definition_fact_surfaces_and_units(registry, contract_type):
    definition = registry.get(contract_type)
    party_ids = {party.party_id for party in definition.parties}
    for spec in definition_fact_specs(definition):
        assert spec.label_sv
        assert spec.label_en
        assert spec.keywords_sv
        assert spec.keywords_en
        if spec.entity_party_id is not None:
            assert spec.kind is FactKind.ENTITY
            assert spec.entity_party_id in party_ids
            assert spec.variants == []
            continue
        assert spec.variants, spec.fact_id
        if spec.kind is FactKind.MONEY:
            assert spec.currency
        if spec.kind is FactKind.DURATION:
            assert spec.unit
        if spec.kind is FactKind.PERCENTAGE:
            assert spec.unit == "percent"
        for variant in spec.variants:
            assert variant.explicit_surfaces
            assert variant.paraphrase_surfaces


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_definition_org_number_series_are_synthetic(registry, contract_type):
    definition = registry.get(contract_type)
    for party in definition.parties:
        if party.kind == "organisation":
            assert party.org_number_series is not None
            assert ORG_NUMBER_SERIES_RE.fullmatch(party.org_number_series)
            assert party.org_number_series.startswith("5590")
        else:
            assert party.org_number_series is None


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_definition_missing_documents_are_not_packaged(registry, contract_type):
    definition = registry.get(contract_type)
    missing = definition.missing_information.missing_documents
    assert missing
    packaged = {
        document.document_id
        for document in (definition.package.appendix, definition.package.amendment)
        if document is not None
    }
    assert not packaged.intersection(missing)
    for missing_id in missing:
        assert re.fullmatch(r"[a-z0-9_]+", missing_id)


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_definition_distractors_near_but_different(registry, contract_type):
    definition = registry.get(contract_type)
    base_values: dict[str, set[object]] = {
        spec.fact_id: {
            variant.value
            for variant in spec.variants
        }
        for spec in definition.facts
    }
    for spec in definition.distractor_facts:
        assert spec.based_on_fact_id in base_values
        assert any(
            variant.value not in base_values[spec.based_on_fact_id]
            for variant in spec.variants
        )


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_definition_contradiction_values_differ(registry, contract_type):
    definition = registry.get(contract_type)
    base = next(
        spec for spec in definition.facts if spec.fact_id == definition.contradiction.base_fact_id
    )
    base_values = {variant.value for variant in base.variants}
    conflict_values = {variant.value for variant in definition.contradiction.fact.variants}
    assert not base_values.intersection(conflict_values)


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_definition_amendments_cover_level_five_and_six(registry, contract_type):
    definition = registry.get(contract_type)
    for difficulty in (5, 6):
        assert any(
            clause.min_difficulty <= difficulty <= clause.max_difficulty
            for clause in definition.amendment_clauses
        )
    level_five_modes = {
        clause.mode
        for clause in definition.amendment_clauses
        if clause.min_difficulty <= 5 <= clause.max_difficulty
    }
    assert ClauseMode.CONTRADICTION in level_five_modes
    assert ClauseMode.EXCEPTION in level_five_modes
    level_six_modes = {
        clause.mode
        for clause in definition.amendment_clauses
        if clause.min_difficulty <= 6 <= clause.max_difficulty
    }
    assert ClauseMode.REFERENCE in level_six_modes
    assert ClauseMode.CONDITIONAL in level_six_modes


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_definition_headings_and_instructions(registry, contract_type):
    definition = registry.get(contract_type)
    clauses = [
        *definition.main_clauses,
        *definition.extra_main_clauses,
        *definition.appendix_clauses,
        *definition.amendment_clauses,
    ]
    for clause in clauses:
        assert clause.heading_sv
        assert clause.heading_en
        assert clause.instruction_sv


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_definition_appendix_has_distributed_clause(registry, contract_type):
    definition = registry.get(contract_type)
    assert any(clause.distributed_facts for clause in definition.appendix_clauses)


# ---------------------------------------------------------------------------
# Generation: all types and difficulties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_generate_all_types_and_difficulties(generator, contract_type, difficulty):
    scenario = generate(generator, contract_type, difficulty)
    assert scenario.contract_type is contract_type
    assert scenario.difficulty == difficulty
    assert scenario.contract_id == "contract_0001"
    assert scenario.seed == DEFAULT_SEED
    assert scenario.language is Language.SV
    assert scenario.synthetic is True
    assert len(scenario.parties) >= 2
    assert len(scenario.facts) >= 8
    assert len(scenario.documents) >= 1
    assert "syntetisk" in scenario.notes_sv.lower()
    assert_reference_integrity(scenario)


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_generate_is_deterministic(generator, contract_type, difficulty):
    request = make_request(contract_type, difficulty)
    first = generator.generate(request, 0)
    second = generator.generate(request, 0)
    assert first.model_dump() == second.model_dump()


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_determinism_survives_new_registry_instance(contract_type):
    request = make_request(contract_type, 4)
    first = ScenarioGenerator(load_registry()).generate(request, 1)
    second = ScenarioGenerator(load_registry()).generate(request, 1)
    assert first.model_dump() == second.model_dump()


def test_contract_id_and_seed_derivation(generator):
    request = make_request(ContractType.LEASE, 2, seed=100)
    scenario = generator.generate(request, 4)
    assert scenario.contract_id == "contract_0005"
    assert scenario.seed == 104


def test_custom_contract_id_prefix(generator):
    request = make_request(
        ContractType.SALE, 1, seed=1, variation_overrides=None
    ).model_copy(update={"contract_id_prefix": "test"})
    scenario = generator.generate(request, 2)
    assert scenario.contract_id == "test_0003"


def test_index_changes_seed_and_contract_id(generator):
    request = make_request(ContractType.NDA, 1, seed=7)
    first = generator.generate(request, 0)
    second = generator.generate(request, 1)
    assert first.contract_id == "contract_0001"
    assert second.contract_id == "contract_0002"
    assert second.seed == first.seed + 1
    assert first.model_dump() != second.model_dump()


def test_frozen_seed_is_reproducible(generator):
    scenario = generate(generator, ContractType.SERVICES, seed=None)
    assert isinstance(scenario.seed, int)
    assert scenario.seed >= 1
    replay = generator.generate(
        make_request(ContractType.SERVICES, 1, seed=scenario.seed), 0
    )
    assert replay.model_dump() == scenario.model_dump()


def test_negative_index_raises(generator):
    with pytest.raises(ScenarioError, match="index"):
        generator.generate(make_request(ContractType.CONSULTING), -1)


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_scenario_json_roundtrip(generator, contract_type, difficulty):
    scenario = generate(generator, contract_type, difficulty)
    payload = scenario.model_dump(mode="json")
    restored = type(scenario).model_validate(payload)
    assert restored.model_dump(mode="json") == payload


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_every_fact_is_expressed_in_a_clause(generator, contract_type, difficulty):
    scenario = generate(generator, contract_type, difficulty)
    expressed = fact_ids_in_clauses(scenario)
    for fact in scenario.facts:
        assert fact.fact_id in expressed


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_party_names_come_from_definition_pools(registry, generator, contract_type):
    definition = registry.get(contract_type)
    scenario = generate(generator, contract_type, 1)
    pools = {party.party_id: party for party in definition.parties}
    for party in scenario.parties:
        spec = pools[party.party_id]
        assert party.name in spec.name_pool
        if party.address is not None:
            assert party.address in spec.address_pool
        assert party.synthetic is True


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_fact_values_come_from_definition_variants(registry, generator, contract_type):
    definition = registry.get(contract_type)
    specs = {spec.fact_id: spec for spec in definition_fact_specs(definition)}
    scenario = generate(generator, contract_type, 2)
    for fact in scenario.facts:
        spec = specs[fact.fact_id]
        if spec.entity_party_id is not None:
            assert fact.value == scenario.party(spec.entity_party_id).name
            assert fact.entity_id == spec.entity_party_id
            continue
        allowed = {variant.value for variant in spec.variants}
        assert fact.value in allowed
        assert fact.explicit_surfaces
        assert fact.paraphrase_surfaces


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_org_numbers_are_clearly_synthetic(registry, generator, contract_type, difficulty):
    definition = registry.get(contract_type)
    party_kinds = {party.party_id: party.kind for party in definition.parties}
    scenario = generate(generator, contract_type, difficulty)
    numbers = []
    for party in scenario.parties:
        if party_kinds[party.party_id] == "individual":
            assert party.org_number is None
            continue
        assert party.org_number is not None
        assert SYNTHETIC_ORG_NUMBER_RE.fullmatch(party.org_number), party.org_number
        numbers.append(party.org_number)
    assert numbers
    assert len(numbers) == len(set(numbers))


def _luhn_valid(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_org_numbers_fail_luhn_check(generator, contract_type, difficulty):
    """Synthetic org numbers must never look like a real, valid org number."""

    scenario = generate(generator, contract_type, difficulty)
    numbers = [party.org_number for party in scenario.parties if party.org_number]
    assert numbers
    for number in numbers:
        digits = number.replace("-", "")
        assert len(digits) == 10
        assert not _luhn_valid(digits), f"{number} passes the Luhn check"


def test_org_numbers_fail_luhn_check_over_many_generated_parties(generator):
    """2000 parties: the 5590xx-xxxx format holds and none is Luhn-valid."""

    numbers: list[str] = []
    for contract_type in ALL_TYPES:
        for seed in range(150):
            scenario = generate(generator, contract_type, 3, seed=seed)
            numbers.extend(
                party.org_number for party in scenario.parties if party.org_number
            )
    assert len(numbers) >= 2000
    for number in numbers:
        assert SYNTHETIC_ORG_NUMBER_RE.fullmatch(number), number
        assert not _luhn_valid(number.replace("-", "")), number
    # A deliberately wrong check digit must still vary between numbers.
    assert len({number[-1] for number in numbers}) > 1


# ---------------------------------------------------------------------------
# Document references are anchored in a clause that names the target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", [3, 4, 5, 6])
def test_package_document_references_are_anchored_in_a_clause(generator, contract_type, difficulty):
    """Every in-package document is referenced from a main-agreement clause.

    A document-level reference alone is not verifiable, so the generator records
    the reference on a clause as well; the clause text then names the document.
    """

    scenario = generate(generator, contract_type, difficulty)
    main = scenario.document("main_agreement")
    document_ids = {document.document_id for document in scenario.documents}
    anchored = {
        reference
        for clause in main.clauses
        for reference in clause.reference_to
        if reference in document_ids
    }
    # Declared-missing documents are only listed on the document, never anchored.
    missing = set(scenario.missing_documents)
    assert set(main.references_documents) == anchored | (missing & set(main.references_documents))
    for document in scenario.documents:
        if document.document_id == "main_agreement":
            continue
        assert document.document_id in anchored
        assert document.document_id in main.references_documents


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", [3, 4, 5, 6])
def test_document_reference_has_a_references_relation_from_that_clause(
    generator, contract_type, difficulty
):
    scenario = generate(generator, contract_type, difficulty)
    document_ids = {document.document_id for document in scenario.documents}
    document_relations = [
        relation
        for relation in scenario.relations
        if relation.kind is RelationKind.REFERENCES and relation.target in document_ids
    ]
    assert document_relations
    for relation in document_relations:
        source_document_id, source_clause_id = relation.source.split(":", 1)
        assert source_document_id == "main_agreement"
        clause = scenario.clause(source_clause_id)
        assert relation.target in clause.reference_to


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", [3, 4, 5, 6])
def test_distributed_reference_clauses_stay_fact_free(
    generator, contract_type, difficulty
):
    """Clauses emptied by fact distribution only point at the appendix clause.

    They must stay without facts (the facts now live in the appendix), so the
    document reference that the generator anchors on them is pure reference text.
    """

    scenario = generate(generator, contract_type, difficulty)
    distributed = [
        clause
        for clause in all_clauses(scenario)
        if clause.mode is ClauseMode.REFERENCE and clause.depends_on
    ]
    for clause in distributed:
        assert not clause.fact_ids
        assert clause.reference_to


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_language_and_synthetic_metadata(generator, contract_type, difficulty):
    scenario = generate(generator, contract_type, difficulty)
    assert scenario.jurisdiction == "SE"
    assert scenario.synthetic is True
    for document in scenario.documents:
        assert document.effective_date is not None
        assert document.title_sv
        assert "syntet" in document.notes.lower()


def test_english_language_request(generator, registry):
    request = make_request(
        ContractType.CONSULTING, 4, language=Language.EN
    )
    scenario = generator.generate(request)
    assert scenario.language is Language.EN
    for document in scenario.documents:
        assert document.title(Language.EN) == document.title_en
        for clause in document.clauses:
            assert clause.heading(Language.EN) == clause.heading_en
    definition = registry.get(ContractType.CONSULTING)
    assert scenario.document("main_agreement").title_en == definition.title_en


def test_main_document_title_and_kind(generator, registry):
    definition = registry.get(ContractType.LICENSE)
    scenario = generate(generator, ContractType.LICENSE, 1)
    main = scenario.document("main_agreement")
    assert main.kind is DocKind.MAIN_AGREEMENT
    assert main.title_sv == definition.title_sv
    assert main.filename_stem == "main_agreement"


# ---------------------------------------------------------------------------
# Difficulty levels: 1-6
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_level_one_is_explicit(generator, contract_type):
    scenario = generate(generator, contract_type, 1)
    assert scenario.variation.directness is Directness.EXPLICIT
    assert scenario.variation.document_quality is DocumentQuality.CLEAN
    assert scenario.variation.distractor_count == 0
    assert scenario.variation.reference_modes == []
    assert scenario.anomalies == []
    assert scenario.missing_documents == []
    assert scenario.relations == []
    assert [document.kind for document in scenario.documents] == [DocKind.MAIN_AGREEMENT]
    for clause in scenario.document("main_agreement").clauses:
        if clause.fact_ids:
            assert clause.mode is ClauseMode.EXPLICIT
    for fact in scenario.facts:
        assert fact.explicit_surfaces


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_level_two_is_paraphrase(generator, contract_type):
    scenario = generate(generator, contract_type, 2)
    assert scenario.variation.directness is Directness.PARAPHRASE
    assert scenario.anomalies == []
    assert scenario.missing_documents == []
    assert [document.kind for document in scenario.documents] == [DocKind.MAIN_AGREEMENT]
    for clause in scenario.document("main_agreement").clauses:
        if clause.fact_ids:
            assert clause.mode is ClauseMode.PARAPHRASE
            for fact_id in clause.fact_ids:
                assert scenario.fact(fact_id).paraphrase_surfaces


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_level_three_has_distractors_and_appendix(registry, generator, contract_type):
    definition = registry.get(contract_type)
    scenario = generate(generator, contract_type, 3)
    document_kinds = [document.kind for document in scenario.documents]
    assert document_kinds == [DocKind.MAIN_AGREEMENT, DocKind.APPENDIX]
    assert ReferenceMode.APPENDIX in scenario.variation.reference_modes
    distractor_ids = definition.distractor_fact_ids
    assert set(distractor_ids).issubset(set(scenario.fact_ids))
    assert scenario.variation.distractor_count == len(distractor_ids)
    distractor_clauses = [
        clause for clause in all_clauses(scenario) if clause.mode is ClauseMode.DISTRACTOR
    ]
    assert distractor_clauses
    referenced = {fact_id for clause in distractor_clauses for fact_id in clause.fact_ids}
    assert set(distractor_ids).issubset(referenced)
    main = scenario.document("main_agreement")
    assert "appendix_a" in main.references_documents


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_level_four_has_relations_and_distribution(generator, contract_type):
    scenario = generate(generator, contract_type, 4)
    assert scenario.variation.structure is StructureMode.MULTI_CLAUSE
    assert ReferenceMode.APPENDIX in scenario.variation.reference_modes
    assert ReferenceMode.CLAUSE in scenario.variation.reference_modes
    assert scenario.relations
    relation_kinds = {relation.kind for relation in scenario.relations}
    assert RelationKind.DEPENDS_ON in relation_kinds
    main = scenario.document("main_agreement")
    appendix = scenario.document("appendix_a")
    appendix_clause_ids = {clause.clause_id for clause in appendix.clauses}
    distributed = [
        clause
        for clause in main.clauses
        if clause.mode is ClauseMode.REFERENCE and clause.depends_on
    ]
    assert distributed
    for clause in distributed:
        assert not clause.fact_ids
        assert set(clause.depends_on).issubset(appendix_clause_ids)
        # The clause may additionally name the appendix as a whole document.
        assert set(clause.reference_to) <= appendix_clause_ids | {"appendix_a"}
        for target_id in clause.depends_on:
            target = scenario.clause(target_id)
            assert target.fact_ids


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_level_five_has_conflicts_and_exceptions(registry, generator, contract_type):
    definition = registry.get(contract_type)
    scenario = generate(generator, contract_type, 5)
    document_kinds = [document.kind for document in scenario.documents]
    assert document_kinds == [
        DocKind.MAIN_AGREEMENT,
        DocKind.APPENDIX,
        DocKind.AMENDMENT,
    ]
    conflict_ids = {
        definition.contradiction.base_fact_id,
        definition.contradiction.fact.fact_id,
    }
    exception_ids = {
        definition.exception.base_fact_id,
        definition.exception.fact.fact_id,
    }
    assert conflict_ids.issubset(set(scenario.fact_ids))
    assert exception_ids.issubset(set(scenario.fact_ids))
    assert scenario.fact(definition.contradiction.fact.fact_id).value != scenario.fact(
        definition.contradiction.base_fact_id
    ).value
    assert any(
        anomaly.answerability is Answerability.CONFLICTING_EVIDENCE
        for anomaly in scenario.anomalies
    )
    assert any(
        anomaly.kind is AnomalyKind.INTENTIONAL_CONTRADICTION for anomaly in scenario.anomalies
    )
    assert any(
        anomaly.kind is AnomalyKind.EXCEPTION and anomaly.governing_fact_id
        for anomaly in scenario.anomalies
    )
    modes = {clause.mode for clause in all_clauses(scenario)}
    assert ClauseMode.CONTRADICTION in modes
    assert ClauseMode.EXCEPTION in modes
    relation_kinds = {relation.kind for relation in scenario.relations}
    assert RelationKind.CONFLICTS_WITH in relation_kinds
    assert RelationKind.CONDITIONS in relation_kinds
    assert scenario.variation.dependency_modes


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_level_six_has_missing_information(registry, generator, contract_type):
    definition = registry.get(contract_type)
    scenario = generate(generator, contract_type, 6)
    assert scenario.missing_documents == definition.missing_information.missing_documents
    assert ReferenceMode.EXTERNAL_SOURCE in scenario.variation.reference_modes
    answerabilities = {anomaly.answerability for anomaly in scenario.anomalies}
    assert Answerability.INSUFFICIENT_EVIDENCE in answerabilities
    assert Answerability.CONDITIONAL in answerabilities
    assert any(
        anomaly.kind is AnomalyKind.MISSING_INFORMATION for anomaly in scenario.anomalies
    )
    referenced_missing = {
        referenced
        for document in scenario.documents
        for referenced in document.references_documents
        if referenced in scenario.missing_documents
    }
    assert referenced_missing
    external_relations = [
        relation
        for relation in scenario.relations
        if relation.target in scenario.missing_documents
    ]
    assert external_relations
    conditional_fact_id = definition.missing_information.conditional_fact.fact_id
    assert conditional_fact_id in scenario.fact_ids
    conditional_fact = scenario.fact(conditional_fact_id)
    assert conditional_fact.kind is not None
    assert any(
        conditional_fact_id in clause.fact_ids for clause in all_clauses(scenario)
    )
    assert any(
        anomaly.fact_ids and conditional_fact_id in anomaly.fact_ids
        for anomaly in scenario.anomalies
    )
    governing = scenario.fact(definition.missing_information.conditional_governing_fact_id)
    assert governing.fact_id in scenario.fact_ids


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", [1, 2, 3, 4])
def test_low_difficulty_scenarios_have_no_anomalies(generator, contract_type, difficulty):
    scenario = generate(generator, contract_type, difficulty)
    assert scenario.anomalies == []
    assert scenario.missing_documents == []


# ---------------------------------------------------------------------------
# Package composition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_package_composition(generator, contract_type, difficulty):
    scenario = generate(generator, contract_type, difficulty, package=True)
    assert [document.kind for document in scenario.documents] == EXPECTED_PACKAGE_KINDS[
        difficulty
    ]
    if difficulty >= 3:
        assert scenario.document("appendix_a").kind is DocKind.APPENDIX
    if difficulty >= 5:
        assert scenario.document("amendment_1").kind is DocKind.AMENDMENT
    main = scenario.document("main_agreement")
    for document in scenario.documents:
        if document.document_id != "main_agreement":
            assert document.document_id in main.references_documents


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_package_disabled_gives_single_agreement(generator, contract_type, difficulty):
    scenario = generate(generator, contract_type, difficulty, package=False)
    assert [document.kind for document in scenario.documents] == [DocKind.MAIN_AGREEMENT]
    assert scenario.document("main_agreement").references_documents == [] or all(
        reference in scenario.missing_documents
        for reference in scenario.document("main_agreement").references_documents
    )


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", [3, 4, 5, 6])
def test_package_disabled_keeps_difficulty_features(generator, contract_type, difficulty):
    scenario = generate(generator, contract_type, difficulty, package=False)
    assert fact_ids_in_clauses(scenario) == set(scenario.fact_ids)
    if difficulty >= 5:
        assert scenario.anomalies
    if difficulty == 6:
        assert scenario.missing_documents
    if difficulty == 3:
        assert scenario.variation.distractor_count >= 1
    # Appendix and amendment clauses are folded into the main agreement.
    assert len(scenario.document("main_agreement").clauses) > make_request(
        contract_type, difficulty, package=False
    ).clauses


# ---------------------------------------------------------------------------
# Clause planning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("clauses", [8, 10, 12, 14])
def test_main_clause_count_matches_request(generator, contract_type, clauses):
    scenario = generate(generator, contract_type, 4, clauses=clauses)
    main = scenario.document("main_agreement")
    assert len(main.clauses) == clauses
    assert [clause.order for clause in main.clauses] == list(
        range(1, clauses + 1)
    )
    assert len({clause.clause_id for clause in main.clauses}) == clauses


@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_clause_count_is_stable_across_difficulties(generator, difficulty):
    scenario = generate(generator, ContractType.CONSULTING, difficulty, clauses=12)
    assert len(scenario.document("main_agreement").clauses) == 12


@pytest.mark.parametrize("clauses", [1, 5, 20, 30])
def test_small_and_large_clause_requests(generator, clauses):
    scenario = generate(generator, ContractType.SALE, 5, clauses=clauses)
    main = scenario.document("main_agreement")
    assert len(main.clauses) == clauses
    assert fact_ids_in_clauses(scenario) == set(scenario.fact_ids)
    assert_reference_integrity(scenario)


def test_low_clause_count_keeps_all_facts(generator):
    scenario = generate(generator, ContractType.EMPLOYMENT, 1, clauses=1)
    main = scenario.document("main_agreement")
    assert len(main.clauses) == 1
    assert set(main.clauses[0].fact_ids) == set(scenario.fact_ids)


# ---------------------------------------------------------------------------
# Variation profile
# ---------------------------------------------------------------------------


def test_variation_profile_is_populated(generator):
    scenario = generate(generator, ContractType.COOPERATION, 6)
    variation = scenario.variation
    assert isinstance(variation, VariationProfile)
    assert variation.language is Language.SV
    assert variation.directness is Directness.MIXED
    assert variation.structure is StructureMode.MULTI_CLAUSE
    assert variation.distractor_count >= 1
    assert ReferenceMode.APPENDIX in variation.reference_modes
    assert ReferenceMode.EXTERNAL_SOURCE in variation.reference_modes


def test_variation_overrides_applied(generator):
    request = make_request(
        ContractType.SUPPLY,
        4,
        variation_overrides={
            "style": "complex",
            "distractor_count": 7,
            "document_quality": "tables",
        },
    )
    scenario = generator.generate(request)
    assert scenario.variation.style.value == "complex"
    assert scenario.variation.distractor_count == 7
    assert scenario.variation.document_quality is DocumentQuality.TABLES


def test_variation_overrides_unknown_key_raises(generator):
    request = make_request(
        ContractType.SUPPLY,
        1,
        variation_overrides={"not_a_variation_field": 1},
    )
    with pytest.raises(ScenarioError, match="unknown variation override"):
        generator.generate(request)


def test_variation_overrides_invalid_value_raises(generator):
    request = make_request(
        ContractType.SUPPLY,
        1,
        variation_overrides={"style": "banana"},
    )
    with pytest.raises(ScenarioError):
        generator.generate(request)


# ---------------------------------------------------------------------------
# Cross-cutting determinism / idempotence
# ---------------------------------------------------------------------------


def test_variation_styles_come_from_definition(registry, generator):
    for contract_type in ALL_TYPES:
        definition = registry.get(contract_type)
        scenario = generate(generator, contract_type, 2)
        assert scenario.variation.style in definition.variation.styles
        assert (
            scenario.variation.document_quality
            in definition.variation.document_qualities
        )


def test_difficulty_is_recorded(generator):
    for difficulty in DIFFICULTIES:
        scenario = generate(generator, ContractType.NDA, difficulty)
        assert scenario.difficulty == difficulty


def test_anomaly_document_ids_are_present_documents(generator):
    for difficulty in (5, 6):
        scenario = generate(generator, ContractType.CONSULTING, difficulty)
        document_ids = {document.document_id for document in scenario.documents}
        for anomaly in scenario.anomalies:
            assert set(anomaly.document_ids).issubset(document_ids)


def test_clause_heading_en_present_for_all_documents(generator):
    scenario = generate(generator, ContractType.SERVICES, 6)
    for document in scenario.documents:
        for clause in document.clauses:
            assert clause.heading_sv
            assert clause.heading_en
            assert clause.instruction_sv


# ---------------------------------------------------------------------------
# Text-anchored cross references (F2)
# ---------------------------------------------------------------------------


def _resolve_scenario_token(
    scenario, token: str
) -> tuple[str | None, str | None]:
    """Resolve a relation token to ``(document_id, clause_id | None)``."""

    if token.startswith("fact:"):
        fact_id = token[len("fact:") :]
        for document in scenario.documents:
            for clause in document.clauses:
                if fact_id in clause.fact_ids:
                    return document.document_id, clause.clause_id
        return None, None
    if token in scenario.missing_documents:
        return token, None
    if ":" in token:
        document_id, clause_id = token.split(":", 1)
        scenario.document(document_id)
        scenario.clause(clause_id)
        return document_id, clause_id
    if any(document.document_id == token for document in scenario.documents):
        return token, None
    clause = scenario.clause(token)
    return clause.document_id, clause.clause_id


def _assert_relations_are_text_anchored(scenario) -> None:
    """Every in-package cross reference must be nameable by its source clause.

    A reference whose source clause does not declare the target in
    ``reference_to`` (or whose source document does not declare a document
    reference) can never be supported by the generated text, which is exactly
    the false-evidence problem F2 describes.
    """

    for relation in scenario.relations:
        assert not relation.source.startswith("fact:"), relation.relation_id
        assert not relation.target.startswith("fact:"), relation.relation_id
        if relation.target in scenario.missing_documents:
            # Declared missing documents have no canonical text; validation
            # explicitly allows those references without evidence.
            continue
        source_document_id, source_clause_id = _resolve_scenario_token(
            scenario, relation.source
        )
        assert source_document_id is not None
        assert source_clause_id is not None, relation.relation_id
        source_clause = scenario.clause(source_clause_id)
        target_document_id, target_clause_id = _resolve_scenario_token(
            scenario, relation.target
        )
        assert target_document_id is not None
        if target_clause_id is not None:
            assert target_clause_id in source_clause.reference_to, relation.relation_id
        else:
            assert target_document_id in source_clause.reference_to or (
                target_document_id in scenario.document(source_document_id).references_documents
            ), relation.relation_id


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", [3, 4, 5, 6])
def test_relations_are_anchored_in_source_clauses(generator, contract_type, difficulty):
    scenario = generate(generator, contract_type, difficulty)
    _assert_relations_are_text_anchored(scenario)


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_level_five_anomaly_relations_target_main_document(registry, generator, contract_type):
    definition = registry.get(contract_type)
    scenario = generate(generator, contract_type, 5)
    by_id = {relation.relation_id: relation for relation in scenario.relations}
    conflict = by_id["rel_conflict_amendment"]
    exception = by_id["rel_exception_condition"]
    main_clause = scenario.document("main_agreement").clauses
    for relation, base_fact_id in (
        (conflict, definition.contradiction.base_fact_id),
        (exception, definition.exception.base_fact_id),
    ):
        target_document, target_clause = _resolve_scenario_token(scenario, relation.target)
        assert target_document == "main_agreement", relation.relation_id
        # A document-level target is what gold can evidence with the document
        # title; a bare clause heading would not name the target document.
        assert target_clause is None
        assert any(base_fact_id in clause.fact_ids for clause in main_clause)
        source_clause = scenario.clause(relation.source.split(":", 1)[1])
        assert "main_agreement" in source_clause.reference_to
    assert conflict.kind is RelationKind.CONFLICTS_WITH
    assert exception.kind is RelationKind.CONDITIONS


@pytest.mark.parametrize("contract_type", ALL_TYPES)
def test_level_six_conditional_relation_targets_main_document(registry, generator, contract_type):
    definition = registry.get(contract_type)
    scenario = generate(generator, contract_type, 6)
    relation = next(
        relation
        for relation in scenario.relations
        if relation.relation_id == "rel_conditional_governing"
    )
    governing_fact_id = definition.missing_information.conditional_governing_fact_id
    target_document, target_clause = _resolve_scenario_token(scenario, relation.target)
    assert target_document == "main_agreement"
    assert target_clause is None
    assert any(
        governing_fact_id in clause.fact_ids
        for clause in scenario.document("main_agreement").clauses
    )
    source_clause = scenario.clause(relation.source.split(":", 1)[1])
    assert "main_agreement" in source_clause.reference_to


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", [5, 6])
def test_governing_facts_stay_in_the_main_agreement(registry, generator, contract_type, difficulty):
    definition = registry.get(contract_type)
    scenario = generate(generator, contract_type, difficulty)
    if difficulty == 5:
        governing = {
            definition.contradiction.base_fact_id,
            definition.exception.base_fact_id,
        }
    else:
        governing = {
            definition.missing_information.conditional_governing_fact_id,
        }
    for clause in scenario.document("main_agreement").clauses:
        governing -= set(clause.fact_ids)
    assert not governing


@pytest.mark.parametrize("contract_type", ALL_TYPES)
@pytest.mark.parametrize("difficulty", DIFFICULTIES)
def test_relations_never_use_fact_tokens(contract_type, generator, difficulty):
    scenario = generate(generator, contract_type, difficulty)
    for relation in scenario.relations:
        assert not relation.source.startswith("fact:"), relation.relation_id
        assert not relation.target.startswith("fact:"), relation.relation_id
