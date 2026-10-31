"""Dataset-level verification of an exported contract dataset.

The loader (owned by the export subsystem) only reads persisted shapes. This
module is the quality gate on top of it:

* manifest hashes and sizes are recomputed;
* ``.txt`` equals ``canonical_text`` from ``rendered.json``;
* every gold/evidence/question span is inside the canonical text, its quote
  matches the slice, and questions are supported by the text;
* manifest counts and status match the contract directories;
* approved contracts are never ``REJECTED``;
* a contract that cannot even be loaded becomes a per-contract ``FAIL`` check
  instead of crashing the whole run.

The loader import is lazy so a partially built checkout can still import
``contractforge.validation``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import (
    Answerability,
    CheckResult,
    CheckStatus,
    DatasetValidationReport,
    EvidencePrecision,
    EvidenceSpan,
    GoldenQuestion,
    LoadedContract,
    QuestionCategory,
    RenderedDocument,
)
from ..utils import normalize_for_search, read_json, sha256_file, sha256_text
from . import _span_problems, semantic

__all__ = ["validate_dataset"]

_ANSWERABILITY_BY_CATEGORY: dict[QuestionCategory, frozenset[Answerability]] = {
    QuestionCategory.CONTRADICTION: frozenset({Answerability.CONFLICTING_EVIDENCE}),
    QuestionCategory.ABSENCE: frozenset(
        {Answerability.INSUFFICIENT_EVIDENCE, Answerability.CONDITIONAL}
    ),
    QuestionCategory.INSUFFICIENT_EVIDENCE: frozenset(
        {Answerability.INSUFFICIENT_EVIDENCE, Answerability.CONDITIONAL}
    ),
    QuestionCategory.CONDITIONAL: frozenset(
        {
            Answerability.ANSWERABLE,
            Answerability.CONDITIONAL,
            Answerability.INSUFFICIENT_EVIDENCE,
        }
    ),
}


def _check(
    check_id: str,
    status: CheckStatus,
    message: str = "",
    *,
    details: dict[str, Any] | None = None,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        status=status,
        message=message,
        details=details or {},
    )


def _load_loader() -> Any | None:
    try:
        from ..export import loader
    except ImportError:  # pragma: no cover - export is built in the same release
        return None
    return loader


# ---------------------------------------------------------------------------
# Small local support checks
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return normalize_for_search(text)[0]


def _value_in_text(value: Any, text: str) -> bool:
    normalized = _norm(text)
    if not normalized:
        return False
    if isinstance(value, str):
        return bool(_norm(value)) and _norm(value) in normalized
    if isinstance(value, bool):
        return ("ja" in normalized) == value or (("nej" in normalized) and not value)
    if isinstance(value, (int, float)):
        number = float(value)
        candidates = {f"{number:g}", str(int(number)) if number.is_integer() else f"{number:g}"}
        return any(candidate in normalized for candidate in candidates if candidate)
    return _norm(str(value)) in normalized


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) == bool(right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return _norm(str(left)) == _norm(str(right))


def _span_covers(outer: EvidenceSpan, inner: EvidenceSpan) -> bool:
    return (
        outer.document_id == inner.document_id
        and outer.start <= inner.start
        and inner.end <= outer.end
    )


def _evidence_text(
    spans: list[EvidenceSpan], rendered: dict[str, RenderedDocument]
) -> str:
    parts: list[str] = []
    for span in spans:
        document = rendered.get(span.document_id)
        if document is None:
            continue
        text = document.canonical_text
        if 0 <= span.start < span.end <= len(text):
            parts.append(text[span.start : span.end])
    return "\n".join(parts)


def _question_answer_problems(
    question: GoldenQuestion,
    rendered: dict[str, RenderedDocument],
    gold_facts: dict[str, Any],
) -> list[str]:
    problems: list[str] = []
    evidence_text = _evidence_text(question.evidence, rendered)
    if question.answerability is Answerability.ANSWERABLE and not question.evidence:
        problems.append("ANSWERABLE fråga saknar evidensspann")
    claims: list[Any] = []
    if question.expected_answer.value is not None:
        claims.append(question.expected_answer.value)
    claims.extend(question.expected_answer.values)
    for value in claims:
        if _value_in_text(value, evidence_text):
            continue
        supported = False
        for fact_id in question.required_facts:
            gold_fact = gold_facts.get(fact_id)
            if gold_fact is None or gold_fact.precision is EvidencePrecision.CLAUSE:
                continue
            if not _values_equal(gold_fact.value, value):
                continue
            if all(
                any(_span_covers(span, gold_span) for span in question.evidence)
                for gold_span in gold_fact.evidence
            ):
                supported = True
                break
        if not supported:
            problems.append(f"förväntat svar {value!r} stöds inte av canonical text")
    if question.expected_answer.text:
        if question.category is QuestionCategory.RELATION:
            if not any(
                _value_in_text(question.expected_answer.text, document.canonical_text)
                for span in question.evidence
                if span.document_id in rendered
                for document in (rendered[span.document_id],)
            ):
                problems.append(
                    f"relationssvaret {question.expected_answer.text!r} saknas i källdokumentet"
                )
        elif question.answerability is Answerability.ANSWERABLE and not _value_in_text(
            question.expected_answer.text, evidence_text
        ):
            problems.append(
                f"förväntat svar {question.expected_answer.text!r} saknas i evidensen"
            )
    return problems


# ---------------------------------------------------------------------------
# Contract checks
# ---------------------------------------------------------------------------


def _manifest_artifact_checks(loaded: LoadedContract, prefix: str) -> list[CheckResult]:
    problems: list[str] = []
    for artifact in loaded.manifest.artifacts:
        if artifact.path == "manifest.json":
            continue
        path = loaded.root / artifact.path
        if not path.is_file():
            problems.append(f"saknad artefakt {artifact.path!r}")
            continue
        digest = sha256_file(path)
        if digest != artifact.sha256:
            problems.append(
                f"sha256 avviker för {artifact.path!r} (manifest {artifact.sha256[:12]}…, "
                f"faktisk {digest[:12]}…)"
            )
        size = path.stat().st_size
        if artifact.size_bytes and size != artifact.size_bytes:
            problems.append(
                f"storlek avviker för {artifact.path!r} (manifest {artifact.size_bytes}, "
                f"faktisk {size})"
            )
    if problems:
        return [
            _check(
                f"{prefix}.manifest.hashes",
                CheckStatus.FAIL,
                "manifestets hashar stämmer inte: " + "; ".join(problems[:6])
                + (" …" if len(problems) > 6 else ""),
                details={"problems": problems, "artifacts": len(loaded.manifest.artifacts)},
            )
        ]
    return [
        _check(
            f"{prefix}.manifest.hashes",
            CheckStatus.PASS,
            f"alla {len(loaded.manifest.artifacts)} artefakthashar stämmer.",
        )
    ]


def _rendered_checks(loaded: LoadedContract, prefix: str) -> list[CheckResult]:
    checks: list[CheckResult] = []
    scenario_ids = [document.document_id for document in loaded.scenario.documents]
    rendered_ids = list(loaded.rendered)
    missing = [document_id for document_id in scenario_ids if document_id not in rendered_ids]
    extra = [document_id for document_id in rendered_ids if document_id not in scenario_ids]
    if missing or extra or not rendered_ids:
        checks.append(
            _check(
                f"{prefix}.rendered.documents",
                CheckStatus.FAIL,
                (
                    f"rendered.json matchar inte scenariots dokument (saknas: {missing}, "
                    f"oväntade: {extra})."
                ),
                details={"missing": missing, "extra": extra},
            )
        )
    else:
        checks.append(
            _check(
                f"{prefix}.rendered.documents",
                CheckStatus.PASS,
                f"rendered.json innehåller alla {len(scenario_ids)} dokument.",
            )
        )
    problems: list[str] = []
    for document_id, rendered_document in loaded.rendered.items():
        for issue in rendered_document.validate_spans():
            problems.append(f"{document_id}: {issue}")
        if rendered_document.document_id != document_id:
            problems.append(
                f"rendered.json-nyckeln {document_id!r} matchar inte dokumentets id "
                f"{rendered_document.document_id!r}"
            )
    if problems:
        checks.append(
            _check(
                f"{prefix}.rendered.spans",
                CheckStatus.FAIL,
                "ogiltiga spann i rendered.json: " + "; ".join(problems[:6]),
                details={"problems": problems},
            )
        )
    else:
        checks.append(
            _check(
                f"{prefix}.rendered.spans",
                CheckStatus.PASS,
                "alla spann i rendered.json är giltiga.",
            )
        )
    return checks


def _canonical_text_checks(loaded: LoadedContract, prefix: str) -> list[CheckResult]:
    problems: list[str] = []
    for document_id, rendered_document in loaded.rendered.items():
        text = loaded.texts.get(document_id)
        if text is None:
            problems.append(f"kanonisk .txt saknas för {document_id!r}")
            continue
        if text != rendered_document.canonical_text:
            problems.append(
                f".txt för {document_id!r} är inte identisk med canonical_text "
                f"({len(text)} vs {len(rendered_document.canonical_text)} tecken)"
            )
    records = {record.document_id: record for record in loaded.manifest.documents}
    for document_id, record in records.items():
        rendered_document = loaded.rendered.get(document_id)
        if rendered_document is None:
            continue
        expected_hash = sha256_text(rendered_document.canonical_text)
        if record.canonical_sha256 != expected_hash:
            problems.append(
                f"DocumentRecord.canonical_sha256 avviker för {document_id!r} "
                f"({record.canonical_sha256[:12]}… vs {expected_hash[:12]}…)"
            )
    if problems:
        return [
            _check(
                f"{prefix}.canonical_text",
                CheckStatus.FAIL,
                "kanonisk text stämmer inte: " + "; ".join(problems[:6]),
                details={"problems": problems},
            )
        ]
    return [
        _check(
            f"{prefix}.canonical_text",
            CheckStatus.PASS,
            f".txt matchar canonical_text för {len(loaded.rendered)} dokument.",
        )
    ]


def _gold_checks(loaded: LoadedContract, prefix: str) -> list[CheckResult]:
    checks: list[CheckResult] = []
    if loaded.gold is None:
        return [
            _check(
                f"{prefix}.gold.present",
                CheckStatus.FAIL,
                "gold_facts.json saknas eller kunde inte läsas för ett godkänt avtal.",
            )
        ]
    gold = loaded.gold
    checks.append(
        _check(
            f"{prefix}.gold.present",
            CheckStatus.PASS,
            f"gold innehåller {len(gold.facts)} fakta, {len(gold.entities)} entiteter.",
        )
    )
    if gold.contract_id != loaded.scenario.contract_id:
        checks.append(
            _check(
                f"{prefix}.gold.contract_id",
                CheckStatus.FAIL,
                f"gold.contract_id {gold.contract_id!r} matchar inte scenariot "
                f"{loaded.scenario.contract_id!r}.",
            )
        )
    else:
        checks.append(
            _check(
                f"{prefix}.gold.contract_id",
                CheckStatus.PASS,
                "gold.contract_id matchar scenariot.",
            )
        )
    problems: list[str] = []
    gold_facts = {gold_fact.fact_id: gold_fact for gold_fact in gold.facts}
    intentionally_missing = {
        fact_id
        for anomaly in loaded.scenario.anomalies
        if anomaly.kind.value in ("missing_information", "ambiguity")
        for fact_id in anomaly.fact_ids
    }
    for fact in loaded.scenario.facts:
        gold_fact = gold_facts.get(fact.fact_id)
        if gold_fact is None:
            problems.append(f"gold saknar scenario-faktum {fact.fact_id!r}")
            continue
        canonical = semantic.canonical_quantity_for_fact(fact)
        if canonical is not None:
            gold_canonical = semantic.canonical_quantity_for_fact(gold_fact)  # type: ignore[arg-type]
            if (
                gold_canonical is None
                or gold_canonical.value != canonical.value
                or gold_canonical.unit != canonical.unit
            ):
                problems.append(
                    f"gold-värdet för {fact.fact_id!r} avviker från scenariot"
                )
        elif gold_fact.value != fact.value:
            problems.append(f"gold-värdet för {fact.fact_id!r} avviker från scenariot")
    for gold_fact in gold.facts:
        if gold_fact.fact_id not in loaded.scenario.fact_ids:
            problems.append(f"gold innehåller okänt faktum {gold_fact.fact_id!r}")
    if problems:
        checks.append(
            _check(
                f"{prefix}.gold.values",
                CheckStatus.FAIL,
                "gold matchar inte scenariot: " + "; ".join(problems[:6]),
                details={"problems": problems},
            )
        )
    else:
        checks.append(
            _check(
                f"{prefix}.gold.values",
                CheckStatus.PASS,
                f"gold täcker alla {len(loaded.scenario.facts)} scenario-fakta med rätt värden.",
            )
        )
    evidence_problems: list[str] = []
    for gold_fact in gold.facts:
        label = f"gold.faktum {gold_fact.fact_id}"
        evidence_problems.extend(_span_problems(gold_fact.evidence, loaded.rendered, label))
        if not gold_fact.evidence and gold_fact.fact_id not in intentionally_missing:
            evidence_problems.append(f"{label} saknar evidensspann")
    for entity in gold.entities:
        evidence_problems.extend(
            _span_problems(entity.evidence, loaded.rendered, f"gold.enhet {entity.entity_id}")
        )
        if not entity.evidence:
            evidence_problems.append(f"gold.enhet {entity.entity_id} saknar evidensspann")
    for anomaly in gold.anomalies:
        evidence_problems.extend(
            _span_problems(anomaly.evidence, loaded.rendered, f"gold.anomali {anomaly.anomaly_id}")
        )
    for index, reference in enumerate(gold.references):
        evidence_problems.extend(
            _span_problems(reference.evidence, loaded.rendered, f"gold.referens {index}")
        )
        if not reference.evidence:
            evidence_problems.append(f"gold.referens {index} saknar evidensspann")
    if evidence_problems:
        checks.append(
            _check(
                f"{prefix}.gold.evidence",
                CheckStatus.FAIL,
                "ogiltiga gold-evidensspann: " + "; ".join(evidence_problems[:6]),
                details={"problems": evidence_problems},
            )
        )
    else:
        checks.append(
            _check(
                f"{prefix}.gold.evidence",
                CheckStatus.PASS,
                "alla gold-evidensspann är giltiga och quote matchar slice.",
            )
        )
    question_ids = {question.question_id for question in loaded.questions}
    stale_links = [
        (gold_fact.fact_id, question_id)
        for gold_fact in gold.facts
        for question_id in gold_fact.question_ids
        if question_id not in question_ids
    ]
    if stale_links:
        checks.append(
            _check(
                f"{prefix}.gold.question_ids",
                CheckStatus.FAIL,
                f"gold.question_ids pekar på frågor som inte finns: {stale_links[:6]}",
                details={"stale_links": stale_links},
            )
        )
    else:
        checks.append(
            _check(
                f"{prefix}.gold.question_ids",
                CheckStatus.PASS,
                "gold.question_ids pekar på befintliga frågor.",
            )
        )
    return checks


def _question_checks(loaded: LoadedContract, prefix: str) -> list[CheckResult]:
    checks: list[CheckResult] = []
    questions = loaded.questions
    if not questions:
        checks.append(
            _check(
                f"{prefix}.questions.present",
                CheckStatus.WARN,
                "avtalet har inga golden questions; kontrollera att pipeline fyllde dem.",
            )
        )
        return checks
    checks.append(
        _check(
            f"{prefix}.questions.present",
            CheckStatus.PASS,
            f"avtalet har {len(questions)} golden questions.",
        )
    )
    gold_facts = {}
    if loaded.gold is not None:
        gold_facts = {gold_fact.fact_id: gold_fact for gold_fact in loaded.gold.facts}
    problems: list[str] = []
    unvalidated: list[str] = []
    for question in questions:
        label = f"fråga {question.question_id}"
        problems.extend(_span_problems(question.evidence, loaded.rendered, label))
        if not question.validated:
            unvalidated.append(question.question_id)
        for fact_id in question.required_facts:
            if fact_id not in gold_facts:
                problems.append(f"{label}: required_fact {fact_id!r} saknas i gold")
        allowed = _ANSWERABILITY_BY_CATEGORY.get(question.category)
        if allowed is not None and question.answerability not in allowed:
            problems.append(
                f"{label}: answerability {question.answerability.value} är ogiltig för "
                f"kategorin {question.category.value}"
            )
        if question.validated:
            problems.extend(
                [f"{label}: {problem}" for problem in _question_answer_problems(
                    question, loaded.rendered, gold_facts
                )]
            )
    if unvalidated:
        checks.append(
            _check(
                f"{prefix}.questions.unvalidated",
                CheckStatus.WARN,
                f"exporterade frågor utan validated=True: {unvalidated[:6]}",
                details={"unvalidated": unvalidated},
            )
        )
    if problems:
        checks.append(
            _check(
                f"{prefix}.questions",
                CheckStatus.FAIL,
                "ogiltiga/ostödda frågor: " + "; ".join(problems[:6]),
                details={"problems": problems},
            )
        )
    else:
        checks.append(
            _check(
                f"{prefix}.questions",
                CheckStatus.PASS,
                f"alla {len(questions)} frågor är giltiga och stöds av kanonisk text.",
            )
        )
    return checks


def _verify_contract(loaded: LoadedContract) -> list[CheckResult]:
    contract_id = loaded.scenario.contract_id
    prefix = f"dataset.contract.{contract_id}"
    checks: list[CheckResult] = []

    if loaded.scenario.synthetic:
        checks.append(
            _check(f"{prefix}.synthetic", CheckStatus.PASS, "scenariot är markerat syntetiskt.")
        )
    else:
        checks.append(
            _check(
                f"{prefix}.synthetic",
                CheckStatus.FAIL,
                "scenariot är inte markerat synthetic=true.",
            )
        )
    manifest = loaded.manifest
    identity_problems: list[str] = []
    if manifest.contract_id != contract_id:
        identity_problems.append(
            f"manifest.contract_id {manifest.contract_id!r} matchar inte scenariot"
        )
    if not manifest.synthetic:
        identity_problems.append("manifest.synthetic är inte true")
    if (
        loaded.verification is not None
        and manifest.verification.status is not loaded.verification.status
    ):
        identity_problems.append("manifest.verification.status matchar inte verification.json")
    if identity_problems:
        checks.append(
            _check(
                f"{prefix}.manifest.identity",
                CheckStatus.FAIL,
                "; ".join(identity_problems) + ".",
            )
        )
    else:
        checks.append(
            _check(
                f"{prefix}.manifest.identity",
                CheckStatus.PASS,
                "manifestets identitet, synthetic-flagga och status stämmer.",
            )
        )
    if loaded.load_errors:
        checks.append(
            _check(
                f"{prefix}.load_errors",
                CheckStatus.WARN,
                "loadern rapporterade: " + "; ".join(loaded.load_errors[:6]),
                details={"load_errors": loaded.load_errors},
            )
        )
    if loaded.verification is None:
        checks.append(
            _check(
                f"{prefix}.verification",
                CheckStatus.FAIL,
                "verification.json saknas eller kunde inte läsas.",
            )
        )
    elif loaded.verification.status.value == "REJECTED":
        checks.append(
            _check(
                f"{prefix}.verification",
                CheckStatus.FAIL,
                "avtalet ligger bland godkända avtal men verification.status är REJECTED.",
            )
        )
    else:
        checks.append(
            _check(
                f"{prefix}.verification",
                CheckStatus.PASS,
                f"avtalet är godkänt (status {loaded.verification.status.value}).",
            )
        )
    checks.extend(_manifest_artifact_checks(loaded, prefix))
    checks.extend(_rendered_checks(loaded, prefix))
    checks.extend(_canonical_text_checks(loaded, prefix))
    checks.extend(_gold_checks(loaded, prefix))
    checks.extend(_question_checks(loaded, prefix))
    return checks


# ---------------------------------------------------------------------------
# Dataset manifest checks
# ---------------------------------------------------------------------------


def _rejected_contract_checks(root: Path) -> tuple[int, list[CheckResult]]:
    """Inspect ``rejected/<contract_id>.json`` files.

    Rejections are real dataset entries: they are counted in the dataset totals
    and must be marked ``REJECTED``, but they are never part of the approved
    contract directories or of gold/questions.
    """

    rejected_dir = root / "rejected"
    if not rejected_dir.is_dir():
        return 0, [
            _check(
                "dataset.rejected",
                CheckStatus.PASS,
                "inga kasserade avtal (rejected/ saknas).",
            )
        ]
    paths = sorted(rejected_dir.glob("*.json"))
    problems: list[str] = []
    contract_ids: list[str] = []
    for path in paths:
        try:
            payload = read_json(path)
        except Exception as exc:  # noqa: BLE001 - reported as a dataset failure
            problems.append(f"{path.name}: {exc}")
            continue
        if not isinstance(payload, dict):
            problems.append(f"{path.name}: förväntade ett JSON-objekt")
            continue
        if payload.get("status") != "REJECTED":
            problems.append(
                f"{path.name}: status {payload.get('status')!r} är inte REJECTED"
            )
        contract_id = payload.get("contract_id")
        if not isinstance(contract_id, str) or not contract_id:
            problems.append(f"{path.name}: contract_id saknas")
        else:
            contract_ids.append(contract_id)
        if not isinstance(payload.get("scenario"), dict):
            problems.append(f"{path.name}: scenario saknas")
        if not isinstance(payload.get("verification"), dict):
            problems.append(f"{path.name}: verification saknas")
    duplicate_ids = sorted(
        {contract_id for contract_id in contract_ids if contract_ids.count(contract_id) > 1}
    )
    if duplicate_ids:
        problems.append(f"dubblerade kasserade contract_id:n: {duplicate_ids}")
    if problems:
        return len(paths), [
            _check(
                "dataset.rejected",
                CheckStatus.FAIL,
                "kasserade avtal är ogiltiga: " + "; ".join(problems[:6]),
                details={"problems": problems, "rejected_files": len(paths)},
            )
        ]
    return len(paths), [
        _check(
            "dataset.rejected",
            CheckStatus.PASS,
            (
                f"{len(paths)} kasserade avtal i rejected/ är markerade REJECTED och "
                "hålls utanför godkända avtal."
            ),
            details={"rejected_files": len(paths), "contract_ids": contract_ids},
        )
    ]


def _verify_dataset_manifest(
    root: Path,
    manifest: Any,
    loaded_contracts: list[LoadedContract],
    rejected_contracts: int,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    prefix = "dataset.manifest"
    if not manifest.synthetic:
        checks.append(
            _check(f"{prefix}.synthetic", CheckStatus.FAIL, "dataset-manifestet är inte syntetiskt.")
        )
    else:
        checks.append(
            _check(f"{prefix}.synthetic", CheckStatus.PASS, "dataset-manifestet är syntetiskt.")
        )
    records = {record.contract_id: record for record in manifest.contracts}
    loaded_ids = {contract.scenario.contract_id for contract in loaded_contracts}
    missing_records = sorted(loaded_ids - set(records))
    extra_records = sorted(set(records) - loaded_ids)
    if missing_records or extra_records:
        checks.append(
            _check(
                f"{prefix}.contracts",
                CheckStatus.FAIL,
                (
                    f"dataset_manifest.json matchar inte katalogerna (saknas: {missing_records}, "
                    f"överflödiga: {extra_records})."
                ),
                details={"missing": missing_records, "extra": extra_records},
            )
        )
    else:
        checks.append(
            _check(
                f"{prefix}.contracts",
                CheckStatus.PASS,
                f"dataset-manifestet listar alla {len(records)} avtal.",
            )
        )
    problems: list[str] = []
    for contract in loaded_contracts:
        record = records.get(contract.scenario.contract_id)
        if record is None:
            continue
        contract_dir = root / record.path
        if not contract_dir.is_dir():
            problems.append(f"katalogen {record.path!r} för {record.contract_id} finns inte")
            continue
        manifest_path = contract_dir / "manifest.json"
        if not manifest_path.is_file():
            problems.append(f"manifest.json saknas i {record.path!r}")
            continue
        digest = sha256_file(manifest_path)
        if digest != record.manifest_sha256:
            problems.append(
                f"manifest_sha256 avviker för {record.contract_id} "
                f"({record.manifest_sha256[:12]}… vs {digest[:12]}…)"
            )
        if record.status.value == "REJECTED" and not record.path.startswith("rejected"):
            problems.append(f"godkänt avtal {record.contract_id} är markerat REJECTED")
        if contract.verification is not None and record.status is not contract.verification.status:
            problems.append(
                f"dataset-manifestets status för {record.contract_id} "
                f"({record.status.value}) matchar inte verification.json "
                f"({contract.verification.status.value})"
            )
    if problems:
        checks.append(
            _check(
                f"{prefix}.records",
                CheckStatus.FAIL,
                "dataset-manifestets poster stämmer inte: " + "; ".join(problems[:6]),
                details={"problems": problems},
            )
        )
    else:
        checks.append(
            _check(
                f"{prefix}.records",
                CheckStatus.PASS,
                "alla manifest-poster, hashar och statusar stämmer.",
            )
        )
    totals = manifest.totals
    totals_problems: list[str] = []
    accepted = len(loaded_contracts)
    rejected = rejected_contracts
    total = accepted + rejected
    if totals.contracts_total != total:
        totals_problems.append(f"totals.contracts_total {totals.contracts_total} != {total}")
    if totals.contracts_accepted != accepted:
        totals_problems.append(
            f"totals.contracts_accepted {totals.contracts_accepted} != {accepted}"
        )
    if totals.contracts_rejected != rejected:
        totals_problems.append(
            f"totals.contracts_rejected {totals.contracts_rejected} != {rejected}"
        )
    if totals_problems:
        checks.append(
            _check(
                f"{prefix}.totals",
                CheckStatus.FAIL,
                "dataset-manifestets totals stämmer inte: " + "; ".join(totals_problems) + ".",
            )
        )
    else:
        checks.append(
            _check(
                f"{prefix}.totals",
                CheckStatus.PASS,
                "dataset-manifestets totals stämmer.",
            )
        )
    return checks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_dataset(
    root: Path, *, expect_min_contracts: int = 0
) -> DatasetValidationReport:
    """Validate an exported dataset directory.

    Broken contracts become per-contract ``FAIL`` checks; they never crash the
    whole validation run. ``expect_min_contracts`` turns an unexpectedly small
    dataset into a dataset-level failure.
    """

    root_path = Path(root)
    report = DatasetValidationReport(root=str(root_path))
    if not root_path.is_dir():
        report.errors.append(f"datasetkatalogen finns inte: {root_path}")
        return report.finalize()
    loader = _load_loader()
    if loader is None:
        report.checks.append(
            _check(
                "dataset.loader",
                CheckStatus.FAIL,
                "export.loader är inte tillgänglig; datasetet kan inte läsas in.",
            )
        )
        return report.finalize()
    try:
        contract_dirs = loader.find_contract_dirs(root_path)
    except Exception as exc:  # noqa: BLE001 - reported as a dataset failure
        report.errors.append(f"kunde inte lista avtalskataloger: {exc}")
        return report.finalize()

    loaded_contracts: list[LoadedContract] = []
    for contract_dir in contract_dirs:
        report.contracts_checked += 1
        try:
            loaded = loader.load_contract_dir(contract_dir)
        except Exception as exc:  # noqa: BLE001 - per-contract FAIL, never crash
            report.checks.append(
                _check(
                    f"dataset.contract.{contract_dir.name}.load",
                    CheckStatus.FAIL,
                    f"avtalet kunde inte läsas in: {exc}",
                    details={"contract_id": contract_dir.name, "path": str(contract_dir)},
                )
            )
            continue
        loaded_contracts.append(loaded)
        report.checks.extend(_verify_contract(loaded))

    dataset_manifest = None
    try:
        dataset_manifest = loader.load_dataset_manifest(root_path)
    except Exception as exc:  # noqa: BLE001 - reported as a dataset failure
        report.checks.append(
            _check(
                "dataset.manifest.load",
                CheckStatus.FAIL,
                f"dataset_manifest.json kunde inte läsas: {exc}",
            )
        )
    rejected_contracts, rejected_checks = _rejected_contract_checks(root_path)
    report.checks.extend(rejected_checks)
    if dataset_manifest is not None:
        report.checks.extend(
            _verify_dataset_manifest(
                root_path, dataset_manifest, loaded_contracts, rejected_contracts
            )
        )
    elif contract_dirs:
        report.checks.append(
            _check(
                "dataset.manifest",
                CheckStatus.FAIL,
                "dataset_manifest.json saknas trots att datasetet innehåller avtal.",
            )
        )
    else:
        report.checks.append(
            _check(
                "dataset.manifest",
                CheckStatus.SKIP,
                "dataset_manifest.json saknas och datasetet är tomt.",
            )
        )

    if expect_min_contracts and report.contracts_checked < expect_min_contracts:
        report.checks.append(
            _check(
                "dataset.expect_min_contracts",
                CheckStatus.FAIL,
                (
                    f"datasetet innehåller {report.contracts_checked} avtal men minst "
                    f"{expect_min_contracts} förväntades."
                ),
                details={
                    "checked": report.contracts_checked,
                    "expected": expect_min_contracts,
                },
            )
        )
    else:
        report.checks.append(
            _check(
                "dataset.expect_min_contracts",
                CheckStatus.PASS,
                (
                    f"datasetet innehåller {report.contracts_checked} avtal "
                    f"(minst {expect_min_contracts} förväntade)."
                ),
            )
        )
    return report.finalize()
