"""Deterministic, data-driven scenario generator.

The generator contains **no** contract-type specific branches. Every type
specific detail (parties, facts, clause plan, package composition, distractor
material and anomaly material) is read from a
:class:`~contractforge.scenarios.registry.ContractTypeDefinition`. The
difficulty level drives the same generic steps for every contract type:

1. all facts are rendered explicitly (difficulty 1) or as paraphrases (2);
2. distractor facts that contradict nothing but look plausible nearby values
   are added (difficulty 3, package appendix);
3. facts are distributed between the main agreement and an appendix through
   explicit relations (difficulty 4);
4. deliberate contradictions and exceptions are expressed in an amendment and
   documented as :class:`Anomaly` objects (difficulty 5);
5. required documents are missing and the scenario records
   ``missing_documents`` plus ``CONDITIONAL`` / ``INSUFFICIENT_EVIDENCE``
   answerability (difficulty 6).

The same ``(request, index)`` always produces the same scenario when
``request.seed`` is set: ``seed = request.seed + index`` and every choice is
drawn from a single seeded :class:`random.Random` instance.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from ..errors import ScenarioError
from ..models import (
    Anomaly,
    AnomalyKind,
    Answerability,
    ClauseMode,
    ClauseSpec,
    DependencyMode,
    Directness,
    DocKind,
    DocumentQuality,
    DocumentSpec,
    Fact,
    Party,
    ReferenceMode,
    Relation,
    RelationKind,
    Scenario,
    ScenarioRequest,
    StructureMode,
    VariationProfile,
)
from .registry import (
    ClausePlanEntry,
    ContractTypeDefinition,
    ContractTypeRegistry,
    DefinitionDocument,
    FactSpec,
)

MAIN_DOCUMENT_ID = "main_agreement"
"""Stable id of the main agreement document."""

MIN_APPENDIX_DIFFICULTY = 3
"""From this difficulty a document package adds an appendix."""

MIN_AMENDMENT_DIFFICULTY = 5
"""From this difficulty a document package adds an amendment."""

MIN_DISTRIBUTION_DIFFICULTY = 4
"""From this difficulty facts are distributed between main agreement and appendix."""

_EPOCH = date(2026, 1, 1)
_FROZEN_SEED_LIMIT = 2**31 - 1


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _luhn_valid(digits: str) -> bool:
    """Return whether ``digits`` passes the Luhn (mod 10) check."""

    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _wrong_check_digit(payload: str, rng: random.Random) -> str:
    """Return a digit that makes ``payload + digit`` fail the Luhn check.

    A controlled series must never be mistakable for a real organisation
    number, so the check digit is *deliberately* wrong instead of drawn freely
    (a free draw would be Luhn-valid one time in ten). Excluding the single
    correct digit keeps the numbers varied while they always fail Luhn.
    """

    correct = next(
        (digit for digit in range(10) if _luhn_valid(payload + str(digit))), None
    )
    wrong = [digit for digit in range(10) if digit != correct]
    return str(rng.choice(wrong))


@dataclass
class _ClauseDraft:
    """Mutable clause plan entry used while composing the package."""

    clause_id: str
    heading_sv: str
    heading_en: str
    fact_ids: list[str] = field(default_factory=list)
    mode: ClauseMode = ClauseMode.EXPLICIT
    optional: bool = False
    distributed_facts: bool = False
    depends_on: list[str] = field(default_factory=list)
    reference_to: list[str] = field(default_factory=list)
    instruction_sv: str = ""
    instruction_en: str = ""


@dataclass
class _DistributionRecord:
    """One fact distribution from a main clause to an appendix clause."""

    source_clause_id: str
    target_clause_id: str
    fact_ids: list[str]


class ScenarioGenerator:
    """Generate deterministic synthetic contract scenarios."""

    def __init__(self, registry: ContractTypeRegistry) -> None:
        self._registry = registry

    def generate(self, request: ScenarioRequest, index: int = 0) -> Scenario:
        if index < 0:
            raise ScenarioError(f"scenario index must be >= 0, got {index}")
        definition = self._registry.get(request.contract_type)
        if request.seed is not None:
            seed = request.seed + index
        else:
            # A frozen random seed is stored in the scenario so the generated
            # dataset entry can be reproduced by inspecting the scenario.
            seed = random.SystemRandom().randrange(1, _FROZEN_SEED_LIMIT)
        builder = _ScenarioBuilder(
            request=request,
            definition=definition,
            seed=seed,
            index=index,
        )
        return builder.build()


class _ScenarioBuilder:
    """Builds exactly one scenario from one definition and one seed."""

    def __init__(
        self,
        *,
        request: ScenarioRequest,
        definition: ContractTypeDefinition,
        seed: int,
        index: int,
    ) -> None:
        self.request = request
        self.definition = definition
        self.seed = seed
        self.index = index
        self.rng = random.Random(seed)
        self.contract_id = f"{request.contract_id_prefix}_{index + 1:04d}"
        self.parties: list[Party] = []
        self.facts: list[Fact] = []
        self.parties_by_id: dict[str, Party] = {}
        self.facts_by_id: dict[str, Fact] = {}
        self.main_drafts: list[_ClauseDraft] = []
        self.appendix_drafts: list[_ClauseDraft] = []
        self.amendment_drafts: list[_ClauseDraft] = []
        self.distributions: list[_DistributionRecord] = []
        self.document_reference_clauses: set[str] = set()
        self.clause_documents: dict[str, str] = {}
        self.documents: list[DocumentSpec] = []
        self.appendix_document_id: str | None = None
        self.amendment_document_id: str | None = None
        self.effective_date = _EPOCH + timedelta(days=self.rng.randrange(0, 365))

    # -- public entry point ----------------------------------------------
    def build(self) -> Scenario:
        self.parties = self._build_parties()
        self.parties_by_id = {party.party_id: party for party in self.parties}
        self.facts = self._build_facts()
        self.facts_by_id = {fact.fact_id: fact for fact in self.facts}

        self.main_drafts = self._fit_main_drafts()
        if self.request.difficulty >= MIN_APPENDIX_DIFFICULTY:
            self.appendix_drafts = self._build_drafts(self.definition.appendix_clauses)
        self.amendment_drafts = self._build_drafts(self.definition.amendment_clauses)
        self._distribute_facts()
        self._anchor_semantic_references()
        self.documents = self._assemble_documents()
        document_anchors = self._anchor_document_references()
        self.documents = self._assemble_documents()

        variation = self._build_variation()
        scenario = Scenario(
            contract_id=self.contract_id,
            contract_type=self.request.contract_type,
            jurisdiction=self.request.jurisdiction,
            language=self.request.language,
            difficulty=self.request.difficulty,
            seed=self.seed,
            parties=self.parties,
            facts=self.facts,
            documents=self.documents,
            variation=variation,
            relations=self._build_relations(document_anchors),
            anomalies=self._build_anomalies(),
            missing_documents=self._missing_documents(),
            synthetic=True,
            notes_sv=(
                f"Syntetiskt scenario för avtalstyp {self.request.contract_type.value} "
                f"(svårighetsgrad {self.request.difficulty}). Alla parter, "
                "organisationsnummer och belopp är påhittade."
            ),
        )
        self._self_check(scenario)
        return scenario

    # -- parties ---------------------------------------------------------
    def _build_parties(self) -> list[Party]:
        parties: list[Party] = []
        used_org_numbers: set[str] = set()
        for spec in self.definition.parties:
            name = self.rng.choice(spec.name_pool)
            address = self.rng.choice(spec.address_pool) if spec.address_pool else None
            org_number = None
            if spec.kind == "organisation":
                org_number = self._synthetic_org_number(
                    spec.party_id, spec.org_number_series, used_org_numbers
                )
            parties.append(
                Party(
                    party_id=spec.party_id,
                    name=name,
                    role=spec.role,
                    org_number=org_number,
                    address=address,
                    synthetic=True,
                )
            )
        return parties

    def _synthetic_org_number(
        self, party_id: str, series: str | None, used: set[str]
    ) -> str:
        if series is None:  # guarded by the definition model
            raise ScenarioError(f"organisation party {party_id} has no org_number_series")
        for _ in range(1000):
            # ``5590xx-XXXX``: the 10-digit number is body (9 digits) + one
            # check digit. The check digit is chosen to be wrong, so every
            # generated number fails the Luhn check and cannot be a real org.nr.
            body = f"{series}{self.rng.randint(0, 999):03d}"
            check = _wrong_check_digit(body, self.rng)
            candidate = f"{body[:6]}-{body[6:]}{check}"
            if candidate not in used:
                used.add(candidate)
                return candidate
        raise ScenarioError(f"could not generate a unique synthetic org number for {party_id}")

    # -- facts -----------------------------------------------------------
    def _build_facts(self) -> list[Fact]:
        difficulty = self.request.difficulty
        facts = [self._build_fact(spec) for spec in self.definition.facts]
        if difficulty >= MIN_APPENDIX_DIFFICULTY:
            facts.extend(self._build_fact(spec) for spec in self.definition.distractor_facts)
        if difficulty == 5:
            facts.append(self._build_fact(self.definition.contradiction.fact))
            facts.append(self._build_fact(self.definition.exception.fact))
        if difficulty == 6:
            facts.append(
                self._build_fact(self.definition.missing_information.conditional_fact)
            )
        return facts

    def _build_fact(self, spec: FactSpec) -> Fact:
        if spec.entity_party_id is not None:
            party = self.parties_by_id.get(spec.entity_party_id)
            if party is None:
                raise ScenarioError(
                    f"fact {spec.fact_id} references unknown party {spec.entity_party_id}"
                )
            return Fact(
                fact_id=spec.fact_id,
                kind=spec.kind,
                value=party.name,
                entity_id=party.party_id,
                label_sv=spec.label_sv,
                label_en=spec.label_en,
                description_sv=spec.description_sv,
                explicit_surfaces=[party.name],
                paraphrase_surfaces=[party.name],
                keywords_sv=list(spec.keywords_sv),
                keywords_en=list(spec.keywords_en),
            )
        if not spec.variants:  # guarded by the definition model
            raise ScenarioError(f"fact {spec.fact_id} has no variants")
        variant = self.rng.choice(spec.variants)
        return Fact(
            fact_id=spec.fact_id,
            kind=spec.kind,
            value=variant.value,
            unit=spec.unit,
            currency=spec.currency,
            label_sv=spec.label_sv,
            label_en=spec.label_en,
            description_sv=variant.description_sv or spec.description_sv,
            explicit_surfaces=list(variant.explicit_surfaces),
            paraphrase_surfaces=list(variant.paraphrase_surfaces),
            keywords_sv=list(spec.keywords_sv),
            keywords_en=list(spec.keywords_en),
        )

    # -- clause plans ----------------------------------------------------
    def _draft_from_entry(
        self, entry: ClausePlanEntry, *, difficulty: int | None = None
    ) -> _ClauseDraft:
        fact_ids = self._resolve_placeholders(entry.fact_ids)
        level = self.request.difficulty if difficulty is None else difficulty
        if fact_ids and level == 1:
            mode = ClauseMode.EXPLICIT
        elif fact_ids and level == 2:
            mode = ClauseMode.PARAPHRASE
        else:
            mode = entry.mode
        return _ClauseDraft(
            clause_id=entry.clause_id,
            heading_sv=entry.heading_sv,
            heading_en=entry.heading_en,
            fact_ids=fact_ids,
            mode=mode,
            optional=entry.optional,
            distributed_facts=entry.distributed_facts,
            depends_on=list(entry.depends_on),
            reference_to=list(entry.reference_to),
            instruction_sv=entry.instruction_sv,
            instruction_en=entry.instruction_en,
        )

    def _resolve_placeholders(self, fact_ids: list[str]) -> list[str]:
        mapping = {
            "$conflict": self.definition.contradiction.fact.fact_id,
            "$exception": self.definition.exception.fact.fact_id,
            "$conditional": self.definition.missing_information.conditional_fact.fact_id,
        }
        return [mapping.get(fact_id, fact_id) for fact_id in fact_ids]

    def _build_drafts(self, plan: list[ClausePlanEntry]) -> list[_ClauseDraft]:
        difficulty = self.request.difficulty
        drafts = [
            self._draft_from_entry(entry)
            for entry in plan
            if entry.min_difficulty <= difficulty <= entry.max_difficulty
        ]
        return drafts

    def _fit_main_drafts(self) -> list[_ClauseDraft]:
        drafts = [self._draft_from_entry(entry) for entry in self.definition.main_clauses]
        target = self.request.clauses
        orphan_fact_ids: list[str] = []
        while len(drafts) > target:
            index = self._last_removable_index(drafts)
            removed = drafts.pop(index)
            orphan_fact_ids.extend(removed.fact_ids)
        if orphan_fact_ids and drafts:
            drafts[-1].fact_ids = _unique(drafts[-1].fact_ids + orphan_fact_ids)

        pool = [self._draft_from_entry(entry) for entry in self.definition.extra_main_clauses]
        counter = 0
        while len(drafts) < target:
            if not pool:
                drafts.append(
                    _ClauseDraft(
                        clause_id=self._next_draft_id(drafts, "clause_extra"),
                        heading_sv="Övriga bestämmelser",
                        heading_en="Other provisions",
                        optional=True,
                    )
                )
                continue
            base = pool[counter % len(pool)]
            counter += 1
            clause_id = base.clause_id
            if any(draft.clause_id == clause_id for draft in drafts):
                clause_id = f"{base.clause_id}_{counter:02d}"
            drafts.append(replace(base, clause_id=clause_id))

        clause_ids = [draft.clause_id for draft in drafts]
        if len(set(clause_ids)) != len(clause_ids):
            raise ScenarioError(f"duplicate clause id while planning {self.contract_id}")
        return drafts

    @staticmethod
    def _last_removable_index(drafts: list[_ClauseDraft]) -> int:
        for index in range(len(drafts) - 1, -1, -1):
            if drafts[index].optional:
                return index
        return len(drafts) - 1

    @staticmethod
    def _next_draft_id(drafts: list[_ClauseDraft], base: str) -> str:
        existing = {draft.clause_id for draft in drafts}
        counter = 1
        candidate = f"{base}_{counter:02d}"
        while candidate in existing:
            counter += 1
            candidate = f"{base}_{counter:02d}"
        return candidate

    # -- fact distribution (difficulty >= 4) -----------------------------
    def _distribute_facts(self) -> None:
        if self.request.difficulty < MIN_DISTRIBUTION_DIFFICULTY:
            return
        # Facts that carry a documented anomaly stay in the main agreement: the
        # amendment/exception clause must be able to name the main clause that
        # holds the governing value, otherwise the relation is unobservable.
        protected = self._protected_distribution_fact_ids()
        eligible = [
            index
            for index, draft in enumerate(self.main_drafts)
            if draft.fact_ids
            and draft.mode in (ClauseMode.EXPLICIT, ClauseMode.PARAPHRASE)
            and not protected.intersection(draft.fact_ids)
        ]
        if not eligible:
            return
        chosen = sorted(self.rng.sample(eligible, min(2, len(eligible))))
        target = self._distribution_target()
        for index in chosen:
            source = self.main_drafts[index]
            moved = list(source.fact_ids)
            if not moved:
                continue
            target.fact_ids = _unique(target.fact_ids + moved)
            source.fact_ids = []
            source.mode = ClauseMode.REFERENCE
            source.depends_on = _unique(source.depends_on + [target.clause_id])
            source.reference_to = _unique(source.reference_to + [target.clause_id])
            self.distributions.append(
                _DistributionRecord(
                    source_clause_id=source.clause_id,
                    target_clause_id=target.clause_id,
                    fact_ids=moved,
                )
            )

    def _protected_distribution_fact_ids(self) -> set[str]:
        """Facts that must stay in the main agreement.

        Two reasons:
        * a level 5/6 anomaly clause must be able to name the clause holding the
          governing fact, otherwise the relation is unobservable;
        * at least one distractor's base fact is kept in the main agreement so a
          cross-document comparison question always has one value on each side
          (main agreement vs appendix), which is the F2/F10 package-question
          requirement.
        """

        difficulty = self.request.difficulty
        protected: set[str] = set()
        anchor = next(
            (
                spec.based_on_fact_id
                for spec in self.definition.distractor_facts
                if spec.based_on_fact_id is not None
            ),
            None,
        )
        if anchor is not None:
            protected.add(anchor)
        if difficulty == 5:
            protected.update(
                {
                    self.definition.contradiction.base_fact_id,
                    self.definition.exception.base_fact_id,
                }
            )
        elif difficulty == 6:
            protected.add(
                self.definition.missing_information.conditional_governing_fact_id
            )
        return protected

    def _anchor_semantic_references(self) -> None:
        """Anchor contradiction/exception/conditional clauses in the main text.

        A ``CONFLICTS_WITH``/``CONDITIONS`` relation is only verifiable when the
        source clause names the target. The target is the main agreement *as a
        document*: gold's narrow reference span can then be the document title
        ("Anställningsavtal"), which unambiguously identifies the target, while
        a bare clause heading would not say which document it belongs to. The
        governing facts are protected from distribution, so the semantic target
        really is the main agreement; the answerability stays in the declared
        :class:`Anomaly` objects.
        """

        difficulty = self.request.difficulty
        anchor_modes: list[ClauseMode] = []
        if difficulty == 5:
            anchor_modes = [ClauseMode.CONTRADICTION, ClauseMode.EXCEPTION]
        elif difficulty == 6:
            anchor_modes = [ClauseMode.CONDITIONAL]
        for mode in anchor_modes:
            draft = self._find_amendment_draft(mode)
            if draft is None:
                continue
            draft.reference_to = _unique(draft.reference_to + [MAIN_DOCUMENT_ID])

    def _distribution_target(self) -> _ClauseDraft:
        for draft in self.appendix_drafts:
            if draft.distributed_facts:
                return draft
        for draft in self.main_drafts:
            if draft.distributed_facts:
                return draft
        draft = _ClauseDraft(
            clause_id=self._next_draft_id(self.main_drafts, "clause_distribution"),
            heading_sv="Detaljreglering",
            heading_en="Detailed regulation",
            mode=ClauseMode.REFERENCE,
        )
        self.main_drafts.append(draft)
        return draft

    # -- documents -------------------------------------------------------
    def _package_appendix(self) -> DefinitionDocument | None:
        if not self.request.package:
            return None
        if self.request.difficulty < MIN_APPENDIX_DIFFICULTY:
            return None
        return self.definition.package.appendix

    def _package_amendment(self) -> DefinitionDocument | None:
        if not self.request.package:
            return None
        if self.request.difficulty < MIN_AMENDMENT_DIFFICULTY:
            return None
        return self.definition.package.amendment

    def _assemble_documents(self) -> list[DocumentSpec]:
        difficulty = self.request.difficulty
        appendix = self._package_appendix()
        amendment = self._package_amendment()
        if amendment is not None and not self.amendment_drafts:
            raise ScenarioError(
                f"definition {self.definition.contract_type.value} has no amendment "
                f"clauses for difficulty {difficulty}"
            )

        main_drafts = list(self.main_drafts)
        if appendix is None:
            main_drafts.extend(self.appendix_drafts)
        if amendment is None:
            main_drafts.extend(self.amendment_drafts)

        missing = self._missing_documents()
        main_references: list[str] = []
        if appendix is not None:
            main_references.append(appendix.document_id)
        if amendment is not None:
            main_references.append(amendment.document_id)
        if missing:
            main_references.append(missing[0])

        plans: list[
            tuple[str, DocKind, str, str, str, list[_ClauseDraft], list[str]]
        ] = [
            (
                MAIN_DOCUMENT_ID,
                DocKind.MAIN_AGREEMENT,
                self.definition.title_sv,
                self.definition.title_en,
                "main_agreement",
                main_drafts,
                main_references,
            )
        ]
        if appendix is not None:
            self.appendix_document_id = appendix.document_id
            plans.append(
                (
                    appendix.document_id,
                    DocKind.APPENDIX,
                    appendix.title_sv,
                    appendix.title_en,
                    appendix.filename_stem,
                    self.appendix_drafts,
                    [MAIN_DOCUMENT_ID],
                )
            )
        if amendment is not None:
            self.amendment_document_id = amendment.document_id
            amendment_references = [MAIN_DOCUMENT_ID]
            if missing:
                amendment_references.append(missing[0])
            plans.append(
                (
                    amendment.document_id,
                    DocKind.AMENDMENT,
                    amendment.title_sv,
                    amendment.title_en,
                    amendment.filename_stem,
                    self.amendment_drafts,
                    amendment_references,
                )
            )

        document_ids = [plan[0] for plan in plans]
        if len(set(document_ids)) != len(document_ids):
            raise ScenarioError(f"duplicate document id in {self.contract_id}")
        clause_count = sum(len(plan[5]) for plan in plans)
        self.clause_documents = {
            draft.clause_id: plan[0] for plan in plans for draft in plan[5]
        }
        if len(self.clause_documents) != clause_count:
            raise ScenarioError(f"duplicate clause id in {self.contract_id}")

        documents: list[DocumentSpec] = []
        for document_id, kind, title_sv, title_en, stem, drafts, references in plans:
            documents.append(
                DocumentSpec(
                    document_id=document_id,
                    kind=kind,
                    title_sv=title_sv,
                    title_en=title_en,
                    filename_stem=stem,
                    clauses=self._to_clauses(document_id, drafts),
                    references_documents=references,
                    effective_date=self.effective_date,
                    notes="Syntetiskt dokument skapat av ContractForge.",
                )
            )
        return documents

    def _to_clauses(
        self, document_id: str, drafts: list[_ClauseDraft]
    ) -> list[ClauseSpec]:
        all_clause_ids = set(self.clause_documents)
        all_document_ids = {document.document_id for document in self.documents}
        clauses: list[ClauseSpec] = []
        for order, draft in enumerate(drafts, start=1):
            depends_on = [
                dependency
                for dependency in draft.depends_on
                if dependency in all_clause_ids and dependency != draft.clause_id
            ]
            # ``reference_to`` holds clause ids; a whole document is a valid
            # reference target too (the clause text names the document title).
            reference_to = [
                reference
                for reference in draft.reference_to
                if reference in all_clause_ids or reference in all_document_ids
            ]
            clauses.append(
                ClauseSpec(
                    clause_id=draft.clause_id,
                    document_id=document_id,
                    order=order,
                    heading_sv=draft.heading_sv,
                    heading_en=draft.heading_en,
                    fact_ids=list(draft.fact_ids),
                    mode=draft.mode,
                    depends_on=depends_on,
                    reference_to=reference_to,
                    instruction_sv=draft.instruction_sv
                    or self._default_instruction_sv(draft),
                    instruction_en=draft.instruction_en
                    or self._default_instruction_en(draft),
                )
            )
        return clauses

    def _default_instruction_sv(self, draft: _ClauseDraft) -> str:
        labels = [
            self.facts_by_id[fact_id].label_sv.lower()
            for fact_id in draft.fact_ids
            if fact_id in self.facts_by_id
        ]
        if labels:
            return (
                f"Reglera {draft.heading_sv.lower()} och uttryck följande uppgifter "
                f"tydligt: {', '.join(labels)}."
            )
        return f"Reglera {draft.heading_sv.lower()} med sedvanlig avtalsformulering."

    def _default_instruction_en(self, draft: _ClauseDraft) -> str:
        heading = draft.heading_en or draft.heading_sv
        return f"Regulate {heading.lower()} with customary contract language."

    # -- relations -------------------------------------------------------
    def _reference(self, clause_id: str) -> str:
        try:
            document_id = self.clause_documents[clause_id]
        except KeyError as exc:
            raise ScenarioError(
                f"relation references unknown clause {clause_id} in {self.contract_id}"
            ) from exc
        return f"{document_id}:{clause_id}"

    def _governing_document_reference(self, fact_id: str) -> str:
        """Document token for a governing fact, so the relation is text-anchored.

        References to ``fact:<id>`` carry no textual anchor of their own. The
        governing facts are protected from distribution, so the main agreement
        is the document that actually holds them; a document target gives gold a
        narrow span on the main agreement title (see
        ``_anchor_semantic_references``).
        """

        if not any(fact_id in draft.fact_ids for draft in self.main_drafts):
            raise ScenarioError(
                f"governing fact {fact_id} is not in the main agreement of "
                f"{self.contract_id}"
            )
        return MAIN_DOCUMENT_ID

    def _find_amendment_draft(self, mode: ClauseMode) -> _ClauseDraft | None:
        for draft in self.amendment_drafts:
            if draft.mode == mode:
                return draft
        return None

    # -- document references ---------------------------------------------
    def _package_documents(self) -> list[tuple[str, ClauseMode]]:
        """``(document_id, entry_mode)`` for every document the main agreement pulls in."""

        documents: list[tuple[str, ClauseMode]] = []
        if self.appendix_document_id is not None:
            documents.append((self.appendix_document_id, ClauseMode.DEFINITION))
        if self.amendment_document_id is not None:
            amendment_entry = next(
                (
                    entry
                    for entry in self.definition.amendment_clauses
                    if entry.min_difficulty <= self.request.difficulty <= entry.max_difficulty
                ),
                None,
            )
            entry_mode = amendment_entry.mode if amendment_entry is not None else ClauseMode.EXPLICIT
            documents.append((self.amendment_document_id, entry_mode))
        return documents

    def _document_reference_anchor(
        self,
        document_id: str,
        entry_mode: ClauseMode,
        *,
        prefer_reference_clauses: bool,
    ) -> _ClauseDraft | None:
        """Pick the main-agreement clause whose text must name the other document.

        A document-level reference is only verifiable when the source side really
        says it, so the anchor is a main-agreement clause that already carries an
        explicit reference of its own (more than one reference would break the
        correspondence between the clause wording and the recorded relation).
        Document references already anchored on a clause are reused instead of
        spreading one reference over several clauses.
        """

        candidates = [
            draft
            for draft in self.main_drafts
            if self.clause_documents.get(draft.clause_id) == MAIN_DOCUMENT_ID
            and len(draft.reference_to) <= 1
            and draft.clause_id not in self.document_reference_clauses
        ]
        if not candidates:
            return None
        if prefer_reference_clauses:
            reference_mode = [
                draft for draft in candidates if draft.mode is ClauseMode.REFERENCE
            ]
            if reference_mode:
                candidates = reference_mode
        explicit = [draft for draft in candidates if draft.mode is not ClauseMode.REFERENCE]
        if explicit:
            return explicit[0]
        for mode in (ClauseMode.EXPLICIT, ClauseMode.PARAPHRASE, entry_mode):
            matches = [draft for draft in candidates if draft.mode is mode]
            if matches:
                return matches[0]
        return candidates[0]

    def _anchor_document_references(self) -> list[tuple[str, str]]:
        """Anchor every package document in a main-agreement clause.

        Returns ``(source_clause_id, target_document_id)`` pairs. The reference is
        recorded in the clause (``reference_to``) *and* in the document
        (``references_documents``), so both the scenario and the derived gold set
        can point at a clause whose text actually names the target document.
        """

        anchors: list[tuple[str, str]] = []
        for document_id, entry_mode in self._package_documents():
            anchor = self._document_reference_anchor(
                document_id,
                entry_mode,
                prefer_reference_clauses=not anchors,
            )
            if anchor is None:
                continue
            anchor.reference_to = _unique(anchor.reference_to + [document_id])
            self.document_reference_clauses.add(anchor.clause_id)
            anchors.append((anchor.clause_id, document_id))
        return anchors

    def _build_relations(self, document_anchors: list[tuple[str, str]]) -> list[Relation]:
        difficulty = self.request.difficulty
        relations: list[Relation] = []
        for index, (source_clause_id, target_document_id) in enumerate(document_anchors, start=1):
            relations.append(
                Relation(
                    relation_id=(
                        "rel_appendix_reference"
                        if index == 1
                        else f"rel_document_reference_{index:02d}"
                    ),
                    kind=RelationKind.REFERENCES,
                    source=self._reference(source_clause_id),
                    target=target_document_id,
                    description_sv=(
                        "Huvudavtalet hänvisar till bilagan för närmare detaljer."
                        if index == 1
                        else "Huvudavtalet hänvisar till tillägget för ändrade villkor."
                    ),
                )
            )
        for index, distribution in enumerate(self.distributions, start=1):
            relations.append(
                Relation(
                    relation_id=f"rel_distribution_{index:02d}",
                    kind=RelationKind.DEPENDS_ON,
                    source=self._reference(distribution.source_clause_id),
                    target=self._reference(distribution.target_clause_id),
                    description_sv=(
                        "Huvudavtalets bestämmelse förutsätter detaljregleringen i bilagan."
                    ),
                )
            )
        if difficulty == 5:
            contradiction = self._find_amendment_draft(ClauseMode.CONTRADICTION)
            exception = self._find_amendment_draft(ClauseMode.EXCEPTION)
            if contradiction is not None:
                relations.append(
                    Relation(
                        relation_id="rel_conflict_amendment",
                        kind=RelationKind.CONFLICTS_WITH,
                        source=self._reference(contradiction.clause_id),
                        target=self._governing_document_reference(
                            self.definition.contradiction.base_fact_id
                        ),
                        description_sv="Tillägget motsäger ett villkor i huvudavtalet.",
                    )
                )
            if exception is not None:
                relations.append(
                    Relation(
                        relation_id="rel_exception_condition",
                        kind=RelationKind.CONDITIONS,
                        source=self._reference(exception.clause_id),
                        target=self._governing_document_reference(
                            self.definition.exception.base_fact_id
                        ),
                        description_sv="Undantaget villkorar ett villkor i huvudavtalet.",
                    )
                )
        if difficulty == 6:
            missing = self._missing_documents()
            external = self._find_amendment_draft(ClauseMode.REFERENCE)
            conditional = self._find_amendment_draft(ClauseMode.CONDITIONAL)
            if external is not None and missing:
                relations.append(
                    Relation(
                        relation_id="rel_external_document",
                        kind=RelationKind.REFERENCES,
                        source=self._reference(external.clause_id),
                        target=missing[0],
                        description_sv="Klausulen hänvisar till ett externt dokument.",
                    )
                )
            if conditional is not None:
                relations.append(
                    Relation(
                        relation_id="rel_conditional_governing",
                        kind=RelationKind.CONDITIONS,
                        source=self._reference(conditional.clause_id),
                        target=self._governing_document_reference(
                            self.definition.missing_information.conditional_governing_fact_id
                        ),
                        description_sv=(
                            "Villkoret styrs av ett villkor i huvudavtalet; anomalin bär "
                            "den villkorade answerabilityn."
                        ),
                    )
                )
        return relations

    # -- anomalies -------------------------------------------------------
    def _build_anomalies(self) -> list[Anomaly]:
        difficulty = self.request.difficulty
        anomalies: list[Anomaly] = []
        if difficulty == 5:
            contradiction = self.definition.contradiction
            contradiction_clause = self._find_amendment_draft(ClauseMode.CONTRADICTION)
            conflict_documents = [MAIN_DOCUMENT_ID]
            if contradiction_clause is not None:
                conflict_documents.append(
                    self.clause_documents.get(
                        contradiction_clause.clause_id, MAIN_DOCUMENT_ID
                    )
                )
            anomalies.append(
                Anomaly(
                    anomaly_id="anomaly_conflicting_terms",
                    kind=AnomalyKind.INTENTIONAL_CONTRADICTION,
                    fact_ids=[
                        contradiction.base_fact_id,
                        contradiction.fact.fact_id,
                    ],
                    document_ids=_unique(conflict_documents),
                    answerability=Answerability.CONFLICTING_EVIDENCE,
                    description_sv=contradiction.description_sv,
                    description_en=contradiction.description_en,
                )
            )
            exception = self.definition.exception
            exception_clause = self._find_amendment_draft(ClauseMode.EXCEPTION)
            exception_documents = [MAIN_DOCUMENT_ID]
            if exception_clause is not None:
                exception_documents.append(
                    self.clause_documents.get(exception_clause.clause_id, MAIN_DOCUMENT_ID)
                )
            anomalies.append(
                Anomaly(
                    anomaly_id="anomaly_exception_scope",
                    kind=AnomalyKind.EXCEPTION,
                    fact_ids=[exception.base_fact_id, exception.fact.fact_id],
                    document_ids=_unique(exception_documents),
                    governing_fact_id=exception.base_fact_id,
                    answerability=Answerability.ANSWERABLE,
                    description_sv=exception.description_sv,
                    description_en=exception.description_en,
                )
            )
        elif difficulty == 6:
            missing = self._missing_documents()
            missing_information = self.definition.missing_information
            conditional_clause = self._find_amendment_draft(ClauseMode.CONDITIONAL)
            conditional_documents = [MAIN_DOCUMENT_ID]
            if conditional_clause is not None:
                conditional_documents.append(
                    self.clause_documents.get(conditional_clause.clause_id, MAIN_DOCUMENT_ID)
                )
            topic = self.facts_by_id[missing_information.insufficient_evidence_fact_id]
            anomalies.append(
                Anomaly(
                    anomaly_id="anomaly_missing_information",
                    kind=AnomalyKind.MISSING_INFORMATION,
                    fact_ids=[],
                    document_ids=[MAIN_DOCUMENT_ID],
                    answerability=Answerability.INSUFFICIENT_EVIDENCE,
                    description_sv=(
                        f"{missing_information.description_sv} Berör uppgiften "
                        f"{topic.label_sv.lower()}. Saknade dokument: {', '.join(missing)}."
                    ),
                )
            )
            anomalies.append(
                Anomaly(
                    anomaly_id="anomaly_conditional_dependency",
                    kind=AnomalyKind.AMBIGUITY,
                    fact_ids=[missing_information.conditional_fact.fact_id],
                    document_ids=_unique(conditional_documents),
                    governing_fact_id=missing_information.conditional_governing_fact_id,
                    answerability=Answerability.CONDITIONAL,
                    description_sv=missing_information.conditional_description_sv,
                )
            )
        return anomalies

    # -- variation profile -----------------------------------------------
    def _build_variation(self) -> VariationProfile:
        difficulty = self.request.difficulty
        directness = Directness.MIXED
        if difficulty == 1:
            directness = Directness.EXPLICIT
        elif difficulty == 2:
            directness = Directness.PARAPHRASE
        reference_modes: list[ReferenceMode] = []
        if self.appendix_document_id is not None:
            reference_modes.append(ReferenceMode.APPENDIX)
        if difficulty >= MIN_DISTRIBUTION_DIFFICULTY:
            reference_modes.append(ReferenceMode.CLAUSE)
        if difficulty == 6:
            reference_modes.append(ReferenceMode.EXTERNAL_SOURCE)
        dependency_modes: list[DependencyMode] = []
        if difficulty == 4:
            dependency_modes.append(DependencyMode.CONDITIONS)
        elif difficulty == 5:
            dependency_modes.extend(
                [DependencyMode.EXCEPTIONS, DependencyMode.CONTRADICTIONS]
            )
        elif difficulty == 6:
            dependency_modes.extend([DependencyMode.CONDITIONS, DependencyMode.EXCEPTIONS])
        quality = DocumentQuality.CLEAN
        if difficulty > 1:
            quality = self.rng.choice(self.definition.variation.document_qualities)
        payload: dict[str, Any] = {
            "language": self.request.language,
            "style": self.rng.choice(self.definition.variation.styles),
            "directness": directness,
            "structure": (
                StructureMode.MULTI_CLAUSE
                if difficulty >= MIN_DISTRIBUTION_DIFFICULTY
                else StructureMode.SINGLE_CLAUSE
            ),
            "reference_modes": reference_modes,
            "distractor_count": (
                len(self.definition.distractor_facts)
                if difficulty >= MIN_APPENDIX_DIFFICULTY
                else 0
            ),
            "document_quality": quality,
            "dependency_modes": dependency_modes,
        }
        overrides = self.request.variation_overrides
        if overrides:
            unknown = sorted(set(overrides) - set(VariationProfile.model_fields))
            if unknown:
                raise ScenarioError(
                    f"unknown variation override(s): {', '.join(unknown)}"
                )
            payload.update(overrides)
        try:
            return VariationProfile.model_validate(payload)
        except ValueError as exc:
            raise ScenarioError(
                f"invalid variation overrides for {self.contract_id}: {exc}"
            ) from exc

    # -- missing information ---------------------------------------------
    def _missing_documents(self) -> list[str]:
        if self.request.difficulty != 6:
            return []
        return list(self.definition.missing_information.missing_documents)

    # -- self checks -----------------------------------------------------
    def _self_check(self, scenario: Scenario) -> None:
        referenced_facts: set[str] = set()
        for document in scenario.documents:
            for clause in document.clauses:
                referenced_facts.update(clause.fact_ids)
        unreferenced = [
            fact.fact_id for fact in scenario.facts if fact.fact_id not in referenced_facts
        ]
        if unreferenced:
            raise ScenarioError(
                f"generated scenario {scenario.contract_id} leaves facts without a clause: "
                f"{', '.join(unreferenced)}"
            )
        if (
            self.request.package
            and scenario.difficulty >= MIN_APPENDIX_DIFFICULTY
            and len(scenario.documents) < 2
        ):
            raise ScenarioError(
                f"difficulty {scenario.difficulty} with package=True must contain more "
                f"than one document ({scenario.contract_id})"
            )
        if scenario.difficulty == 5 and not scenario.anomalies:
            raise ScenarioError(f"difficulty 5 scenario {scenario.contract_id} has no anomalies")
        if scenario.difficulty == 6 and not scenario.missing_documents:
            raise ScenarioError(
                f"difficulty 6 scenario {scenario.contract_id} has no missing documents"
            )
        main_document = scenario.document(MAIN_DOCUMENT_ID)
        exact_main_clause_count = (
            self.request.package
            or self.request.difficulty < MIN_APPENDIX_DIFFICULTY
        )
        if exact_main_clause_count and len(main_document.clauses) != self.request.clauses:
            raise ScenarioError(
                f"got {len(main_document.clauses)} main clauses, expected "
                f"{self.request.clauses} ({scenario.contract_id})"
            )
