"""Score a completed blind rating round into a quality report.

The protocol (``docs/quality-protocol.md``) separates two things that are easy
to mix up:

* **quality** – how often the expected answer is correct (Q2) and how often the
  evidence supports it (Q3);
* **agreement** – how often two independent raters reached the same code, per
  axis.

This module computes both from the filled rater sheets and enforces the rules
that make the numbers trustworthy:

* agreement is computed on the **individual, original** codes, per axis, before
  any post-hoc resolution (7.5);
* a consensus/third-rater resolution may only decide the quality of a post and
  is reported separately;
* the unweighted sample value is the standard result; a population estimate is
  an extra, clearly labelled figure (5.4);
* every failing post needs exactly one of the seven error causes (4.4);
* a model-based assessment is labelled as such and is never described as human
  legal review (8).
"""

from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .errors import UsageError, ValidationError
from .models import utc_now
from .utils import atomic_write_json, atomic_write_text, read_json, sha256_file

SCORE_SCHEMA_VERSION = "1.0"
SCORE_FILENAME = "quality_score.json"
SCORE_MARKDOWN_FILENAME = "quality_report_draft.md"

AXES = ("Q1", "Q2", "Q3", "Q4")
REQUIRED_AXES = ("Q1", "Q2", "Q3")
AXIS_COLUMNS = {
    "Q1": "Q1_answerable",
    "Q2": "Q2_answer_correct",
    "Q3": "Q3_evidence_sufficient",
    "Q4": "Q4_anomaly_handling",
}
YES = "ja"
NO = "nej"
UNSURE = "osäker"
NOT_APPLICABLE = "ej_tillamplig"
CAUSES = (
    "frågeformulering",
    "facitvärde",
    "evidensspann",
    "validatorlucka",
    "genereringsfel",
    "juridisk_tolkning",
)
RATER_KINDS = ("human", "model")

#: Reviewer-kind labels used in the report (8: model-based != human review).
KIND_LABELS = {
    "human": "mänsklig juridisk bedömning",
    "model": "modellbaserad oberoende bedömning",
}

#: Gate thresholds from protocol 5.2. Two-sided: point estimate and Wilson bound.
GATES: dict[str, dict[str, float]] = {
    "Q2": {"point": 0.95, "wilson_lower": 0.90},
    "Q3": {"point": 0.90, "wilson_lower": 0.85},
    "Q1": {"point": 0.95, "wilson_lower": None},  # G3 is split by class, see GATE_NOTES
}
AGREEMENT_GATES: dict[str, dict[str, float]] = {
    "Q2": {"agreement": 0.90, "kappa": 0.70},
    "Q1": {"agreement": 0.80, "kappa": 0.60},
    "Q3": {"agreement": 0.80, "kappa": 0.60},
    "Q4": {"agreement": 0.80, "kappa": 0.60},
}
Z_95 = 1.959964
MIN_GROUP_N = 10


@dataclass
class AxisResult:
    axis: str
    n: int = 0
    yes: int = 0
    no: int = 0
    unsure: int = 0
    not_applicable: int = 0
    missing: int = 0
    agreement_n: int = 0
    agreement_same: int = 0
    kappa: float | None = None
    p_e: float | None = None
    wilson_lower: float | None = None
    wilson_upper: float | None = None
    gate_passed: bool | None = None
    gate_reasons: list[str] = field(default_factory=list)

    @property
    def proportion(self) -> float | None:
        return (self.yes / self.n) if self.n else None

    @property
    def agreement(self) -> float | None:
        return (self.agreement_same / self.agreement_n) if self.agreement_n else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "n": self.n,
            "yes": self.yes,
            "no": self.no,
            "unsure": self.unsure,
            "not_applicable": self.not_applicable,
            "missing": self.missing,
            "proportion_yes": self.proportion,
            "wilson_95": (
                [self.wilson_lower, self.wilson_upper]
                if self.wilson_lower is not None
                else None
            ),
            "gate_passed": self.gate_passed,
            "gate_reasons": self.gate_reasons,
            "agreement": {
                "n": self.agreement_n,
                "same": self.agreement_same,
                "p0": self.agreement,
                "pe": self.p_e,
                "kappa": self.kappa,
            },
        }


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson score interval for a proportion (protocol 4.2)."""

    if n <= 0:
        return None
    p_hat = successes / n
    denom = 1.0 + z * z / n
    centre = p_hat + z * z / (2 * n)
    margin = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n))
    lower = max(0.0, (centre - margin) / denom)
    upper = min(1.0, (centre + margin) / denom)
    return (lower, upper)


def cohen_kappa(pairs: list[tuple[str, str]]) -> tuple[float | None, float | None, int]:
    """Cohen's kappa for two raters (protocol 4.9). Returns ``(kappa, p_e, n)``."""

    rated = [(a, b) for a, b in pairs if a in (YES, NO, UNSURE) and b in (YES, NO, UNSURE)]
    n = len(rated)
    if n == 0:
        return None, None, 0
    codes = sorted({code for pair in rated for code in pair})
    observed = sum(1 for a, b in rated if a == b) / n
    expected = sum(
        (sum(1 for a, _ in rated if a == code) / n) * (sum(1 for _, b in rated if b == code) / n)
        for code in codes
    )
    if expected >= 1.0:  # pragma: no cover - both raters used one single code
        return None, expected, n
    return (observed - expected) / (1 - expected), expected, n


def _read_sheet(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        raise UsageError(f"bedömningsblanketten saknas: {path}", details={"path": str(path)})
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    if not reader.fieldnames:
        raise ValidationError(f"blanketten har ingen rubrikrad: {path}")
    rows: dict[str, dict[str, str]] = {}
    for row in reader:
        sample_id = (row.get("sample_id") or "").strip()
        if not sample_id:
            continue
        rows[sample_id] = {key: (value or "").strip() for key, value in row.items() if key}
    return rows


def _normalise(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in (YES, NO, UNSURE, NOT_APPLICABLE):
        return lowered
    if lowered in ("ej tillämplig", "ej tillamplig", "n/a", "na", "-", ""):
        return "" if lowered == "" else NOT_APPLICABLE
    raise ValidationError(
        f"ogiltig bedömningskod {value!r}; tillåtna koder är "
        f"{YES}/{NO}/{UNSURE}/{NOT_APPLICABLE}",
        details={"value": value},
    )


@dataclass
class ScoreInput:
    ratings_dir: Path
    sheet_a: Path
    sheet_b: Path
    causes: Path | None = None
    consensus: Path | None = None
    rater_a_kind: str = "model"
    rater_a_name: str = "ratare A"
    rater_b_kind: str = "model"
    rater_b_name: str = "ratare B"
    population: Path | None = None


def score_round(spec: ScoreInput) -> dict[str, Any]:
    """Score a completed round and return the report payload (also written to disk)."""

    for kind in (spec.rater_a_kind, spec.rater_b_kind):
        if kind not in RATER_KINDS:
            raise UsageError(
                f"rater kind måste vara en av {RATER_KINDS}, fick {kind!r}",
                details={"kind": kind},
            )

    manifest_path = spec.ratings_dir / "manifest.json"
    if not manifest_path.is_file():
        raise UsageError(
            f"mätpaketet saknar manifest.json: {spec.ratings_dir}",
            details={"path": str(spec.ratings_dir)},
        )
    manifest = read_json(manifest_path)
    questions = {record["sample_id"]: record for record in manifest.get("questions", [])}
    if not questions:
        raise ValidationError("mätpaketet innehåller inga frågor")

    sheet_a = _read_sheet(spec.sheet_a)
    sheet_b = _read_sheet(spec.sheet_b)
    missing_axes = [
        column
        for column in AXIS_COLUMNS.values()
        if column not in (sheet_a and next(iter(sheet_a.values())).keys())
        or column not in (sheet_b and next(iter(sheet_b.values())).keys())
    ]
    if missing_axes:
        raise ValidationError(
            f"granskningsblanketterna saknar axelkolumner {missing_axes}",
            details={
                "sheet_a": str(spec.sheet_a),
                "sheet_b": str(spec.sheet_b),
            },
        )
    unknown = (set(sheet_a) | set(sheet_b)) - set(questions)
    if unknown:
        raise ValidationError(
            f"blanketterna innehåller {len(unknown)} okända sample_id:n",
            details={"unknown": sorted(unknown)[:10]},
        )

    raters_meta = _raters_meta(spec.ratings_dir, spec.sheet_a, spec.sheet_b)
    consensus_posts: dict[str, dict[str, str]] = {}
    if spec.consensus is not None:
        consensus_posts = _read_consensus(spec.consensus, questions)

    per_axis = {axis: AxisResult(axis=axis) for axis in AXES}
    disagreements: dict[str, list[dict[str, Any]]] = {axis: [] for axis in AXES}
    final_codes: dict[str, str] = {}
    unresolved = 0
    for sample_id in sorted(questions):
        codes_a = _codes(sheet_a.get(sample_id, {}))
        codes_b = _codes(sheet_b.get(sample_id, {}))
        resolved = consensus_posts.get(sample_id)
        for axis in AXES:
            result = per_axis[axis]
            code_a = codes_a.get(axis, "")
            code_b = codes_b.get(axis, "")
            final = _final_code(axis, code_a, code_b, resolved)
            if final is None:
                unresolved += 1
                result.missing += 1
                continue
            if final == NOT_APPLICABLE:
                result.not_applicable += 1
            else:
                if axis == "Q2":
                    final_codes[sample_id] = final
                result.n += 1
                if final == YES:
                    result.yes += 1
                elif final == NO:
                    result.no += 1
                elif final == UNSURE:
                    result.unsure += 1
            if axis in AXES and code_a and code_b:
                result.agreement_n += 1
                if code_a == code_b:
                    result.agreement_same += 1
            if axis in AXES and code_a and code_b and code_a != code_b:
                disagreements[axis].append(
                    {
                        "sample_id": sample_id,
                        "a": code_a,
                        "b": code_b,
                        "final": final,
                        "type": _disagreement_type(codes_a, codes_b, axis, code_a, code_b),
                    }
                )

    for axis in AXES:
        result = per_axis[axis]
        interval = wilson_interval(result.yes, result.n)
        if interval is not None:
            result.wilson_lower, result.wilson_upper = interval

    # Agreement is computed on the ORIGINAL individual codes only (7.5).
    original_pairs = {axis: [] for axis in AXES}
    for sample_id in questions:
        codes_a = _codes(sheet_a.get(sample_id, {}))
        codes_b = _codes(sheet_b.get(sample_id, {}))
        for axis in AXES:
            code_a, code_b = codes_a.get(axis, ""), codes_b.get(axis, "")
            if code_a in (YES, NO, UNSURE) and code_b in (YES, NO, UNSURE):
                original_pairs[axis].append((code_a, code_b))
    for axis in AXES:
        kappa, p_e, _n = cohen_kappa(original_pairs[axis])
        per_axis[axis].kappa = kappa
        per_axis[axis].p_e = p_e

    agreement_gates = _agreement_gates(per_axis)
    causes = _read_causes(spec.causes, questions) if spec.causes else {}
    cause_counts = _cause_counts(causes)
    errors = _error_posts(per_axis, questions, sheet_a, sheet_b, consensus_posts, disagreements)
    unclassified = [post for post in errors if post not in causes]
    quality_gates = _quality_gates(per_axis, original_pairs)
    population = _population_estimate(final_codes, spec.population, questions)

    coded_posts = sum(1 for codes in final_codes.values())
    any_coding = coded_posts > 0 or any(
        result.agreement_n for result in per_axis.values()
    )
    if not any_coding:
        raise ValidationError(
            "inget av urvalets 100 frågor är kodat i blanketterna – "
            "fyll i ratings_A.csv och ratings_B.csv innan du kör score-ratings",
            details={"sampled": len(questions)},
        )
    missing_posts = sorted(
        sample_id
        for sample_id in questions
        if not _codes(sheet_a.get(sample_id, {})) and not _codes(sheet_b.get(sample_id, {}))
    )
    overall_pass = all(
        gate.get("passed") is True
        for gate in list(quality_gates.values()) + list(agreement_gates.values())
        if gate.get("applicable", True)
    ) and not unclassified and not missing_posts
    kind_label = (
        KIND_LABELS["model"]
        if "model" in (spec.rater_a_kind, spec.rater_b_kind)
        else KIND_LABELS["human"]
    )

    payload: dict[str, Any] = {
        "schema_version": SCORE_SCHEMA_VERSION,
        "kind": "quality_score",
        "created_at": utc_now().isoformat(),
        "source": {
            "ratings_dir": str(spec.ratings_dir),
            "source_sha256": manifest.get("source_sha256"),
            "selection_sha256": manifest.get("selection_sha256"),
            "sample_size": manifest.get("sampling", {}).get("sample_size"),
            "pool_size": manifest.get("sampling", {}).get("pool_size"),
            "sheet_a": {"path": str(spec.sheet_a), "sha256": sha256_file(spec.sheet_a)},
            "sheet_b": {"path": str(spec.sheet_b), "sha256": sha256_file(spec.sheet_b)},
            "raters": raters_meta,
        },
        "reviewer": {
            "rater_a": {"kind": spec.rater_a_kind, "name": spec.rater_a_name},
            "rater_b": {"kind": spec.rater_b_kind, "name": spec.rater_b_name},
            "assessment_type": kind_label,
            "is_model_based": "model" in (spec.rater_a_kind, spec.rater_b_kind),
            "caveat": (
                "Modellbaserad bedömning – inte mänsklig juridisk granskning. Två modeller "
                "kan dela systematiska fel."
                if "model" in (spec.rater_a_kind, spec.rater_b_kind)
                else "Mänsklig juridisk bedömning enligt protokollet."
            ),
        },
        "axes": {axis: per_axis[axis].to_dict() for axis in AXES},
        "gates": {"quality": quality_gates, "agreement": agreement_gates},
        "disagreements": {
            "per_axis": {axis: _disagreement_summary(disagreements[axis]) for axis in AXES},
            "unresolved_posts": unresolved,
        },
        "errors": {
            "causes": cause_counts,
            "unclassified_posts": unclassified,
            "gate_passed": not unclassified,
        },
        "population_estimate": population,
        "status": "measured" if overall_pass else "measured – gates not all met",
        "gates_all_met": overall_pass,
        "completeness": {
            "sampled": len(questions),
            "uncoded_posts": len(missing_posts),
            "uncoded_sample_ids": missing_posts[:20],
            "complete": not missing_posts,
            "gate_passed": not missing_posts,
        },
        "notes": [
            "Överensstämmelse är beräknad på ursprungliga individuella koder (7.5); en "
            "konsensuslösning påverkar endast postens kvalitet.",
            "Urvalsvärdet är standard; en populationsskattning är ett separat tal (5.4).",
            "Ett sammanslaget kappa över alla axlar får inte användas som grind (4.3).",
        ],
    }

    atomic_write_json(spec.ratings_dir / SCORE_FILENAME, payload)
    atomic_write_text(
        spec.ratings_dir / SCORE_MARKDOWN_FILENAME, render_markdown(payload)
    )
    return payload


def _raters_meta(ratings_dir: Path, sheet_a: Path, sheet_b: Path) -> dict[str, Any]:
    meta: dict[str, Any] = {"orders_identical": None, "source": "unknown"}
    raters_path = ratings_dir / "raters.json"
    if raters_path.is_file():
        recorded = read_json(raters_path)
        meta["orders_identical"] = recorded.get("orders_identical")
        meta["source"] = "raters.json"
    else:
        # No raters.json: compare the row order of the two sheets directly.
        order_a = list(_read_sheet(sheet_a))
        order_b = list(_read_sheet(sheet_b))
        meta["orders_identical"] = order_a == order_b
        meta["source"] = "sheet comparison"
    return meta


def _codes(row: dict[str, str]) -> dict[str, str]:
    codes: dict[str, str] = {}
    for axis, column in AXIS_COLUMNS.items():
        raw = row.get(column, "")
        if not raw:
            continue
        code = _normalise(raw)
        if code == NOT_APPLICABLE:
            # A rater marking an axis "not applicable" on one sheet and a real
            # code on the other is a disagreement, not an empty cell.
            codes[axis] = NOT_APPLICABLE
        else:
            codes[axis] = code
    return codes


def _final_code(
    axis: str,
    code_a: str,
    code_b: str,
    resolved: dict[str, str] | None,
) -> str | None:
    if resolved and axis in resolved:
        return resolved[axis]
    if not code_a and not code_b:
        return None
    if code_a and code_b:
        if code_a == code_b:
            return code_a
        # An unresolved disagreement counts as "not proven correct" (4.1);
        # Q4 without an applicable side is kept as not applicable.
        return UNSURE
    return code_a or code_b


def _disagreement_type(
    codes_a: dict[str, str],
    codes_b: dict[str, str],
    axis: str,
    code_a: str,
    code_b: str,
) -> Literal["felavvikelse", "gränsfall", "faktaoenighet"]:
    """Classify a disagreement (protocol 4.3).

    * ``gränsfall``     – one side chose `osäker` (cannot decide).
    * ``felavvikelse``  – the raters disagree about *different kinds* of
      judgement: the answer may be right but the evidence insufficient, or the
      reverse.
    * ``faktaoenighet`` – a different hard code about the same kind of
      question, with every other axis aligned.
    """

    if code_a == UNSURE or code_b == UNSURE:
        return "gränsfall"

    def differs(other: str) -> bool:
        left = codes_a.get(other, "")
        right = codes_b.get(other, "")
        return left != right

    cross_axis = any(differs(other) for other in AXES if other != axis)
    if cross_axis:
        return "felavvikelse"
    if code_a != code_b:
        return "faktaoenighet"
    return "faktaoenighet"


def _disagreement_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"felavvikelse": 0, "gränsfall": 0, "faktaoenighet": 0}
    for item in items:
        counts[item["type"]] += 1
    return {"total": len(items), "by_type": counts, "posts": items}


def _agreement_gates(per_axis: dict[str, AxisResult]) -> dict[str, Any]:
    gates: dict[str, Any] = {}
    for axis, limits in AGREEMENT_GATES.items():
        result = per_axis[axis]
        reasons: list[str] = []
        agreement = result.agreement
        kappa = result.kappa
        if result.agreement_n == 0:
            gates[axis] = {"applicable": False, "passed": None, "reasons": ["inga parvisa koder"]}
            continue
        if agreement is None or agreement < limits["agreement"]:
            reasons.append(
                f"enighet {agreement:.1%} < {limits['agreement']:.0%}"
                if agreement is not None
                else "enighet saknas"
            )
        if kappa is None or kappa < limits["kappa"]:
            reasons.append(
                f"kappa {kappa:.2f} < {limits['kappa']:.2f}"
                if kappa is not None
                else "kappa kan inte beräknas"
            )
        gates[axis] = {
            "applicable": True,
            "passed": not reasons,
            "agreement": agreement,
            "kappa": kappa,
            "reasons": reasons,
        }
    return gates


def _quality_gates(
    per_axis: dict[str, AxisResult], original_pairs: dict[str, list[tuple[str, str]]]
) -> dict[str, Any]:
    gates: dict[str, Any] = {}
    for axis in ("Q2", "Q3"):
        limits = GATES[axis]
        result = per_axis[axis]
        reasons: list[str] = []
        if result.n == 0:
            gates[axis] = {"applicable": False, "passed": None, "reasons": ["inga poster"]}
            continue
        proportion = result.proportion or 0.0
        if proportion < limits["point"]:
            reasons.append(f"punkt {proportion:.1%} < {limits['point']:.0%}")
        if result.wilson_lower is not None and result.wilson_lower < limits["wilson_lower"]:
            reasons.append(
                f"Wilson nedre {result.wilson_lower:.1%} < {limits['wilson_lower']:.0%}"
                f" (n={result.n})"
            )
        gates[axis] = {
            "applicable": True,
            "passed": not reasons,
            "point": proportion,
            "wilson_lower": result.wilson_lower,
            "reasons": reasons,
        }
    q1 = per_axis["Q1"]
    q1_reasons: list[str] = []
    if q1.n and (q1.proportion or 0.0) < GATES["Q1"]["point"]:
        q1_reasons.append(f"Q1 ja {q1.proportion:.1%} < 95 %")
    gates["Q1"] = {
        "applicable": q1.n > 0,
        "passed": (not q1_reasons) if q1.n else None,
        "point": q1.proportion,
        "reasons": q1_reasons if q1.n else ["inga poster"],
    }
    q4 = per_axis["Q4"]
    if q4.n < MIN_GROUP_N:
        gates["Q4"] = {
            "applicable": False,
            "passed": None,
            "reasons": [f"för få tillämpliga poster (n={q4.n} < {MIN_GROUP_N})"],
        }
    else:
        proportion = q4.proportion or 0.0
        gates["Q4"] = {
            "applicable": True,
            "passed": proportion >= 0.85,
            "point": proportion,
            "reasons": [] if proportion >= 0.85 else [f"andel ja {proportion:.1%} < 85 %"],
        }
    return gates


def _read_causes(path: Path, questions: dict[str, Any]) -> dict[str, str]:
    """Read ``sample_id;cause`` rows (CSV or JSON list of objects)."""

    if not path.is_file():
        raise UsageError(f"felorsaksfilen saknas: {path}", details={"path": str(path)})
    text = path.read_text(encoding="utf-8-sig")
    causes: dict[str, str] = {}
    if text.lstrip().startswith("["):
        payload = json.loads(text)
        for record in payload:
            sample_id = str(record.get("sample_id", "")).strip()
            cause = str(record.get("cause", "")).strip()
            if sample_id:
                causes[sample_id] = cause
    else:
        for row in csv.DictReader(io.StringIO(text), delimiter=";"):
            sample_id = (row.get("sample_id") or "").strip()
            cause = (row.get("cause") or "").strip()
            if sample_id:
                causes[sample_id] = cause
    unknown = set(causes) - set(questions)
    if unknown:
        raise ValidationError(
            "felorsaksfilen pekar på okända sample_id:n", details={"unknown": sorted(unknown)[:10]}
        )
    invalid = {sid: cause for sid, cause in causes.items() if cause not in CAUSES}
    if invalid:
        raise ValidationError(
            f"ogiltiga felorsaker: {sorted(set(invalid.values()))}; tillåtna är {list(CAUSES)}",
            details={"invalid": dict(list(invalid.items())[:10])},
        )
    return causes


def _read_consensus(path: Path, questions: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Read ``sample_id;Q1;Q2;Q3;Q4`` rows from a third rater / consensus sheet."""

    if not path.is_file():
        raise UsageError(f"konsensusfilen saknas: {path}", details={"path": str(path)})
    text = path.read_text(encoding="utf-8-sig")
    posts: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(io.StringIO(text), delimiter=";"):
        sample_id = (row.get("sample_id") or "").strip()
        if not sample_id or sample_id not in questions:
            continue
        codes: dict[str, str] = {}
        for axis, column in AXIS_COLUMNS.items():
            raw = (row.get(column) or "").strip()
            if raw:
                codes[axis] = _normalise(raw)
        posts[sample_id] = codes
    return posts


def _error_posts(
    per_axis: dict[str, AxisResult],
    questions: dict[str, Any],
    sheet_a: dict[str, dict[str, str]],
    sheet_b: dict[str, dict[str, str]],
    consensus: dict[str, dict[str, str]],
    disagreements: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """Posts where the final code (after consensus) is not ``ja``/not applicable."""

    with_error: set[str] = set()
    for items in disagreements.values():
        for item in items:
            if item["final"] in (NO, UNSURE):
                with_error.add(item["sample_id"])
    for sample_id, codes in consensus.items():
        if any(code in (NO, UNSURE) for code in codes.values()):
            with_error.add(sample_id)
    # A post delivered with only one raters's code and a non-ja code is an error too.
    for sheet in (sheet_a, sheet_b):
        for sample_id, row in sheet.items():
            codes = _codes(row)
            for code in codes.values():
                if code in (NO, UNSURE):
                    with_error.add(sample_id)
                    break
    return sorted(with_error)


def _cause_counts(causes: dict[str, str]) -> dict[str, Any]:
    counts = dict.fromkeys(CAUSES, 0)
    for cause in causes.values():
        counts[cause] += 1
    return {
        "by_cause": counts,
        "total_classified": len(causes),
        "roles": {
            "facitfel": counts["facitvärde"] + counts["evidensspann"],
            "mätbarhetsproblem": counts["frågeformulering"] + counts["juridisk_tolkning"],
            "systemfel": counts["genereringsfel"] + counts["validatorlucka"],
        },
    }


def _population_estimate(
    final_codes: dict[str, str],
    population_path: Path | None,
    questions: dict[str, Any],
) -> dict[str, Any]:
    """Stratified estimate of Q2 correctness for the whole question population.

    Only computed when the caller supplies stratum sizes for the full population
    (``--population``) and every stratum is represented in the sample. The result
    is reported separately and never used for the gates (protocol 5.4).
    """

    if population_path is None:
        return {
            "status": "not_computed",
            "reason": (
                "Ingen populationsfil angavs (--population). En viktad skattning kräver "
                "stratumstorlekarna i hela populationen; urvalsvärdet gäller urvalet."
            ),
            "source": "manifest.json (bara urvalets strata)",
        }
    payload = read_json(population_path)
    counts = payload.get("strata") if isinstance(payload, dict) else payload
    if not isinstance(counts, dict) or not counts:
        raise ValidationError(
            f"populationsfilen måste innehålla {{stratum: antal}}: {population_path}"
        )
    sizes = {str(key): int(value) for key, value in counts.items()}
    total = float(sum(sizes.values()))
    if total <= 0:
        raise ValidationError(f"populationsfilen anger noll frågor: {population_path}")
    missing = [
        key for key, size in sizes.items() if size > 0 and not _has_sample(key, questions)
    ]
    if missing:
        return {
            "status": "not_computed",
            "reason": f"stratum saknas i urvalet: {missing[:5]}",
            "source": str(population_path),
        }
    weighted = 0.0
    variance = 0.0
    rows: list[dict[str, Any]] = []
    for key, size in sizes.items():
        weight = size / total
        stratum_posts = [
            post for post in questions if _stratum_key(questions[post]) == key and post in final_codes
        ]
        if not stratum_posts:
            continue
        n_s = len(stratum_posts)
        p_hat = sum(1 for post in stratum_posts if final_codes[post] == YES) / n_s
        weighted += weight * p_hat
        variance += weight * weight * p_hat * (1 - p_hat) / n_s
        rows.append(
            {"stratum": key, "N_s": size, "n_s": n_s, "p_hat_s": p_hat, "w_s": weight}
        )
    se = math.sqrt(variance)
    return {
        "status": "estimated",
        "source": str(population_path),
        "axis": "Q2",
        "population_size": int(total),
        "weighted_proportion": weighted,
        "standard_error": se,
        "ci95": [max(0.0, weighted - Z_95 * se), min(1.0, weighted + Z_95 * se)],
        "rows": rows,
        "note": "Populationsskattningen används inte för grindarna (5.4).",
    }


def _stratum_key(question: dict[str, Any]) -> str:
    return (
        f"{question.get('category')}/d{question.get('difficulty')}/"
        f"{question.get('answerability')}"
    )


def _stratum_sizes_from_manifest(questions: dict[str, Any]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for question in questions.values():
        key = _stratum_key(question)
        sizes[key] = sizes.get(key, 0) + 1
    return dict(sorted(sizes.items()))


def _has_sample(stratum_key: str, questions: dict[str, Any]) -> bool:
    return any(_stratum_key(question) == stratum_key for question in questions.values())


def render_markdown(payload: dict[str, Any]) -> str:
    axes = payload["axes"]
    quality = payload["gates"]["quality"]
    agreement = payload["gates"]["agreement"]
    reviewer = payload["reviewer"]
    lines = [
        "# Kvalitetsbedömning av ContractForge-facit",
        "",
        f"- **Datum:** {payload['created_at']}",
        f"- **Bedömningens art:** {reviewer['assessment_type']}",
        f"- **Källa:** `{payload['source']['ratings_dir']}`",
        f"- **Urval:** {payload['source']['sample_size']} av "
        f"{payload['source']['pool_size']} frågor, `selection_sha256` "
        f"`{payload['source']['selection_sha256']}`",
        f"- **Dataset:** `source_sha256` `{payload['source']['source_sha256']}`",
        f"- **Ratare:** A = {reviewer['rater_a']['kind']} "
        f"({reviewer['rater_a']['name']}), B = {reviewer['rater_b']['kind']} "
        f"({reviewer['rater_b']['name']})",
        f"- **Blanketter olika ordnade:** {payload['source']['raters'].get('orders_identical') is False}",
        f"- **Status:** {payload['status']}",
        "",
        reviewer["caveat"],
        "",
        "## Resultat per axel",
        "",
        "| Axel | N | ja | nej | osäker | andel ja | Wilson 95 % | Grind |",
        "|------|--:|---:|----:|-------:|---------:|-------------|-------|",
    ]
    for axis in AXES:
        result = axes[axis]
        interval = result["wilson_95"]
        interval_text = (
            f"{interval[0]:.1%} – {interval[1]:.1%}" if interval else "–"
        )
        share = f"{result['proportion_yes']:.1%}" if result["proportion_yes"] is not None else "–"
        gate = quality.get(axis)
        gate_text = (
            "uppfylld"
            if gate and gate.get("passed") is True
            else ("ej uppfylld" if gate and gate.get("passed") is False else "ej bedömd")
        )
        lines.append(
            f"| {axis} | {result['n']} | {result['yes']} | {result['no']} | "
            f"{result['unsure']} | {share} | {interval_text} | {gate_text} |"
        )
    completeness = payload["completeness"]
    lines += [
        "",
        "## Fullständighet (G7)",
        "",
        f"- Urval: {completeness['sampled']} frågor; okodade: {completeness['uncoded_posts']}",
        f"- Alla poster kodade av båda raterna: {'ja' if completeness['complete'] else 'nej'}",
        "",
        "## Överensstämmelse per axel (ursprungliga individuella koder)",
        "",
        "| Axel | Parvisa koder | Eniga | P₀ | κ | Grind |",
        "|------|--------------:|------:|---:|--:|-------|",
    ]
    for axis in AXES:
        result = axes[axis]
        agreement_data = result["agreement"]
        p0 = (
            f"{agreement_data['p0']:.1%}"
            if agreement_data["p0"] is not None
            else "–"
        )
        kappa = f"{agreement_data['kappa']:.2f}" if agreement_data["kappa"] is not None else "–"
        gate = agreement.get(axis)
        gate_text = (
            "uppfylld"
            if gate and gate.get("passed") is True
            else ("ej uppfylld" if gate and gate.get("passed") is False else "ej bedömd")
        )
        lines.append(
            f"| {axis} | {agreement_data['n']} | {agreement_data['same']} | {p0} | "
            f"{kappa} | {gate_text} |"
        )
    lines += [
        "",
        "## Grindar",
        "",
        f"- **Alla grindar uppfyllda:** {'ja' if payload['gates_all_met'] else 'nej'}",
    ]
    for axis, gate in quality.items():
        reasons = "; ".join(gate.get("reasons") or []) or "–"
        lines.append(f"- G-{axis}: {'ja' if gate.get('passed') else 'nej'} — {reasons}")
    for axis, gate in agreement.items():
        reasons = "; ".join(gate.get("reasons") or []) or "–"
        lines.append(f"- G5-{axis}: {'ja' if gate.get('passed') else 'nej'} — {reasons}")
    disagreement = payload["disagreements"]
    lines += [
        "",
        "## Oenighet",
        "",
        f"- Oeniga poster totalt: {sum(v['total'] for v in disagreement['per_axis'].values())}",
        f"- Poster där alla tre svar olika: {disagreement['unresolved_posts']}",
    ]
    for summary in disagreement["per_axis"].values():
        by_type = summary["by_type"]
        lines.append(
            f"- {axis}: felavvikelse {by_type['felavvikelse']}, gränsfall "
            f"{by_type['gränsfall']}, faktaoenighet {by_type['faktaoenighet']}"
        )
    causes = payload["errors"]["causes"]
    lines += [
        "",
        "## Felorsaker",
        "",
        "| Orsak | Antal |",
        "|-------|------:|",
    ]
    for cause, count in causes["by_cause"].items():
        lines.append(f"| `{cause}` | {count} |")
    lines += [
        "",
        f"- Klassificerade poster: {causes['total_classified']}",
        f"- Oklassificerade felposter: {len(payload['errors']['unclassified_posts'])}",
        "",
        "## Populationsskattning",
        "",
    ]
    population = payload["population_estimate"]
    if population["status"] == "estimated":
        ci = population["ci95"]
        lines.append(
            f"Viktad Q2-skattning för populationen: "
            f"{population['weighted_proportion']:.1%} (95 % KI {ci[0]:.1%} – {ci[1]:.1%}); "
            "används inte för grindarna."
        )
    else:
        lines.append(f"Ej beräknad: {population['reason']}")
    lines += [
        "",
        "## Vad mätningen inte bevisar",
        "",
        "- Resultatet gäller det publicerade urvalet, inte alla avtalstyper eller nivåer.",
        "- " + reviewer["caveat"],
        "",
    ]
    return "\n".join(lines)
