"""Tests for the quality scoring engine (``contractforge.quality``).

These tests exercise the statistical and procedural rules of
``docs/quality-protocol.md`` on synthetic rating sheets: per-axis agreement on
the original codes, Wilson intervals, the post-hoc separation, the error-cause
gate and the reviewer-type caveat.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from contractforge.errors import ValidationError
from contractforge.quality import (
    AXES,
    AXIS_COLUMNS,
    ScoreInput,
    cohen_kappa,
    score_round,
    wilson_interval,
)


def _wilson_reference(successes: int, n: int) -> tuple[float, float]:
    import math

    p = successes / n
    z = 1.959964
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((centre - margin) / denom, (centre + margin) / denom)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def test_wilson_interval_matches_protocol_reference_values():
    lower, upper = wilson_interval(95, 100)
    assert lower == pytest.approx(0.8878, abs=5e-4)
    assert upper == pytest.approx(0.9788, abs=5e-4)
    # Point estimate alone is not enough: 95/100 misses the G1 lower bound.
    assert lower < 0.90
    assert wilson_interval(0, 0) is None
    for successes in (0, 1, 7, 42, 100):
        lower, upper = wilson_interval(successes, 100)
        assert 0.0 <= lower <= successes / 100 <= upper <= 1.0


def test_cohen_kappa_is_one_when_raters_always_agree():
    pairs = [("ja", "ja")] * 30 + [("nej", "nej")] * 70
    kappa, _p_e, n = cohen_kappa(pairs)
    assert n == 100
    assert kappa == pytest.approx(1.0)


def test_cohen_kappa_is_zero_for_chance_level_agreement():
    # Raters alternate independently: observed agreement equals what chance gives.
    pairs = [("ja", "ja")] + [("nej", "ja")] + [("ja", "nej")] + [("nej", "nej")]
    kappa, _p_e, _n = cohen_kappa(pairs)
    assert kappa == pytest.approx(0.0, abs=1e-9)


def test_cohen_kappa_is_negative_when_raters_disagree_systematically():
    pairs = [("ja", "nej")] * 50 + [("nej", "ja")] * 50
    kappa, _p_e, _n = cohen_kappa(pairs)
    assert kappa is not None and kappa < 0


# ---------------------------------------------------------------------------
# Synthetic rating rounds
# ---------------------------------------------------------------------------


def _write_packet(tmp_path: Path, n: int = 12, stratified_categories: bool = True) -> Path:
    packet = tmp_path / "packet"
    packet.mkdir()
    questions = []
    for index in range(n):
        category = ["fact_extraction", "comparison", "entity_identification"][
            index % 3 if stratified_categories else 0
        ]
        questions.append(
            {
                "sample_id": f"cfq-{index:012d}",
                "contract_id": f"contract_{index:04d}",
                "question_id": "q_0001",
                "category": category,
                "difficulty": 2,
                "answerability": "ANSWERABLE",
                "question": f"Fråga {index}?",
                "expected_answer": f"svar {index}",
            }
        )
    manifest = {
        "schema_version": "1.0",
        "kind": "question_sample",
        "source_sha256": "a" * 64,
        "selection_sha256": "b" * 64,
        "sampling": {"sample_size": n, "pool_size": n * 10},
        "questions": questions,
    }
    (packet / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    order_a = [row["sample_id"] for row in questions]
    order_b = list(reversed(order_a))
    (packet / "raters.json").write_text(
        json.dumps(
            {
                "raters": {"A": {"sample_ids": order_a}, "B": {"sample_ids": order_b}},
                "orders_identical": order_a == order_b,
            }
        ),
        encoding="utf-8",
    )
    return packet


def _write_sheet(path: Path, rows: list[tuple[str, str, str, str, str]]) -> Path:
    header = [
        "sample_id",
        *AXIS_COLUMNS.values(),
        "comment",
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(header)
    for row in rows:
        writer.writerow(list(row) + [""])
    path.write_text(buffer.getvalue(), encoding="utf-8-sig")
    return path


def _perfect_round(packet: Path) -> tuple[Path, Path]:
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    ids = [question["sample_id"] for question in manifest["questions"]]
    sheet_a = _write_sheet(
        packet / "a.csv", [(sid, "ja", "ja", "ja", "ej_tillamplig") for sid in ids]
    )
    sheet_b = _write_sheet(
        packet / "b.csv", [(sid, "ja", "ja", "ja", "ej_tillamplig") for sid in reversed(ids)]
    )
    return sheet_a, sheet_b


def test_perfect_round_passes_every_applicable_gate(tmp_path):
    packet = _write_packet(tmp_path)
    sheet_a, sheet_b = _perfect_round(packet)

    payload = score_round(ScoreInput(ratings_dir=packet, sheet_a=sheet_a, sheet_b=sheet_b))

    assert payload["axes"]["Q2"]["proportion_yes"] == 1.0
    assert payload["axes"]["Q2"]["wilson_95"][0] == pytest.approx(0.70, abs=0.1)
    # A small sample cannot clear G1 (lower bound < 90 %), which is expected:
    # the report says so instead of pretending the round passed.
    assert payload["gates"]["quality"]["Q2"]["passed"] is False
    assert any("Wilson nedre" in reason for reason in payload["gates"]["quality"]["Q2"]["reasons"])
    assert payload["gates_all_met"] is False
    # Q4 is not applicable often enough (n < 10) and must not fail the round.
    assert payload["gates"]["quality"]["Q4"]["applicable"] is False
    assert (packet / "quality_score.json").is_file()
    assert (packet / "quality_report_draft.md").is_file()


def test_agreement_is_computed_before_consensus_resolution(tmp_path):
    """A later consensus must never change the reported kappa (protocol 7.5)."""

    packet = _write_packet(tmp_path)
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    ids = [question["sample_id"] for question in manifest["questions"]]
    # Rater A says ja everywhere; rater B is unsure on the first three posts.
    rows_a = [(sid, "ja", "ja", "ja", "ej_tillamplig") for sid in ids]
    rows_b = [
        (sid, "osäker", "ja", "ja", "ej_tillamplig") if index < 3 else (sid, "ja", "ja", "ja", "ej_tillamplig")
        for index, sid in enumerate(ids)
    ]
    sheet_a = _write_sheet(packet / "a.csv", rows_a)
    sheet_b = _write_sheet(packet / "b.csv", rows_b)
    consensus = packet / "consensus.csv"
    _write_sheet(consensus, [(sid, "ja", "ja", "ja", "ej_tillamplig") for sid in ids])

    base = score_round(ScoreInput(ratings_dir=packet, sheet_a=sheet_a, sheet_b=sheet_b))
    kappa_before = base["axes"]["Q1"]["agreement"]["kappa"]
    assert kappa_before < 1.0

    resolved = score_round(
        ScoreInput(
            ratings_dir=packet,
            sheet_a=sheet_a,
            sheet_b=sheet_b,
            consensus=consensus,
        )
    )
    # Quality may improve once the third rater decides, but agreement may not.
    assert resolved["axes"]["Q1"]["agreement"]["kappa"] == kappa_before
    assert resolved["axes"]["Q1"]["agreement"]["n"] == base["axes"]["Q1"]["agreement"]["n"]
    assert resolved["axes"]["Q1"]["unsure"] == 0


def test_disagreements_are_typed_per_axis(tmp_path):
    packet = _write_packet(tmp_path)
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    ids = [question["sample_id"] for question in manifest["questions"]]
    # Post 0: rater A says the answer is correct but the evidence is weak,
    # while rater B says the answer is wrong (Q2 differs as well as Q3) =>
    # the raters disagree about different kinds of judgement => felavvikelse.
    # Post 1: one rater unsure => gränsfall.
    # Post 2: different hard Q3 codes while Q1/Q2 agree => faktaoenighet.
    rows_a = [
        (ids[0], "ja", "ja", "ja", "ej_tillamplig"),
        (ids[1], "ja", "ja", "ja", "ej_tillamplig"),
        (ids[2], "ja", "ja", "ja", "ej_tillamplig"),
    ]
    rows_b = [
        (ids[0], "ja", "nej", "nej", "ej_tillamplig"),
        (ids[1], "ja", "ja", "osäker", "ej_tillamplig"),
        (ids[2], "ja", "ja", "nej", "ej_tillamplig"),
    ]
    for sid in ids[3:]:
        rows_a.append((sid, "ja", "ja", "ja", "ej_tillamplig"))
        rows_b.append((sid, "ja", "ja", "ja", "ej_tillamplig"))
    sheet_a = _write_sheet(packet / "a.csv", rows_a)
    sheet_b = _write_sheet(packet / "b.csv", rows_b)

    payload = score_round(ScoreInput(ratings_dir=packet, sheet_a=sheet_a, sheet_b=sheet_b))

    q3 = payload["disagreements"]["per_axis"]["Q3"]
    q2 = payload["disagreements"]["per_axis"]["Q2"]
    assert q3["by_type"]["felavvikelse"] == 1  # post 0: two kinds at once
    assert q3["by_type"]["gränsfall"] == 1  # post 1: unsure vs ja
    assert q3["by_type"]["faktaoenighet"] == 1  # post 2: one axis, hard codes
    assert q2["by_type"]["felavvikelse"] == 1  # post 0's Q2 differs alongside Q3


def test_failing_quality_gate_is_reported_with_reasons(tmp_path):
    packet = _write_packet(tmp_path, n=40)
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    ids = [question["sample_id"] for question in manifest["questions"]]
    # 6 of 40 Q2 answers wrong => 85 % point estimate, below the 95 % gate.
    rows = [
        (sid, "ja", "nej" if index < 6 else "ja", "ja", "ej_tillamplig")
        for index, sid in enumerate(ids)
    ]
    sheet_a = _write_sheet(packet / "a.csv", rows)
    sheet_b = _write_sheet(packet / "b.csv", rows)

    payload = score_round(ScoreInput(ratings_dir=packet, sheet_a=sheet_a, sheet_b=sheet_b))

    gate = payload["gates"]["quality"]["Q2"]
    assert gate["passed"] is False
    assert any("punkt" in reason for reason in gate["reasons"])
    assert payload["gates_all_met"] is False
    assert payload["status"] != "measured"


def test_error_causes_must_be_complete_and_valid(tmp_path):
    packet = _write_packet(tmp_path, n=12)
    ids = [
        question["sample_id"]
        for question in json.loads((packet / "manifest.json").read_text())["questions"]
    ]
    rows = [
        (sid, "ja", "nej" if index < 2 else "ja", "ja", "ej_tillamplig")
        for index, sid in enumerate(ids)
    ]
    sheet_a = _write_sheet(packet / "a.csv", rows)
    sheet_b = _write_sheet(packet / "b.csv", rows)

    incomplete = score_round(ScoreInput(ratings_dir=packet, sheet_a=sheet_a, sheet_b=sheet_b))
    assert incomplete["errors"]["unclassified_posts"] == ids[:2]
    assert incomplete["errors"]["gate_passed"] is False

    causes = packet / "causes.csv"
    causes.write_text(
        "sample_id;cause\n" + "".join(f"{sid};facitvärde\n" for sid in ids[:2]),
        encoding="utf-8",
    )
    complete = score_round(
        ScoreInput(ratings_dir=packet, sheet_a=sheet_a, sheet_b=sheet_b, causes=causes)
    )
    assert complete["errors"]["unclassified_posts"] == []
    assert complete["errors"]["causes"]["by_cause"]["facitvärde"] == 2

    bad = packet / "bad_causes.csv"
    bad.write_text("sample_id;cause\n" + f"{ids[0]};typo\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        score_round(ScoreInput(ratings_dir=packet, sheet_a=sheet_a, sheet_b=sheet_b, causes=bad))


def test_model_based_assessment_is_labelled_and_caveated(tmp_path):
    packet = _write_packet(tmp_path)
    sheet_a, sheet_b = _perfect_round(packet)

    payload = score_round(
        ScoreInput(
            ratings_dir=packet,
            sheet_a=sheet_a,
            sheet_b=sheet_b,
            rater_a_kind="model",
            rater_a_name="modell X",
            rater_b_kind="model",
            rater_b_name="modell Y",
        )
    )

    assert payload["reviewer"]["assessment_type"] == "modellbaserad oberoende bedömning"
    assert "mänsklig juridisk granskning" in payload["reviewer"]["caveat"]
    report = (packet / "quality_report_draft.md").read_text(encoding="utf-8")
    assert "modellbaserad" in report
    assert "mänsklig juridisk granskning" in report


def test_human_round_is_labelled_as_human_review(tmp_path):
    packet = _write_packet(tmp_path)
    sheet_a, sheet_b = _perfect_round(packet)

    payload = score_round(
        ScoreInput(
            ratings_dir=packet,
            sheet_a=sheet_a,
            sheet_b=sheet_b,
            rater_a_kind="human",
            rater_b_kind="human",
        )
    )

    assert payload["reviewer"]["assessment_type"] == "mänsklig juridisk bedömning"
    assert payload["reviewer"]["is_model_based"] is False


def test_population_estimate_requires_a_population_file(tmp_path):
    packet = _write_packet(tmp_path)
    sheet_a, sheet_b = _perfect_round(packet)

    payload = score_round(ScoreInput(ratings_dir=packet, sheet_a=sheet_a, sheet_b=sheet_b))
    assert payload["population_estimate"]["status"] == "not_computed"

    population = tmp_path / "population.json"
    population.write_text(
        json.dumps(
            {
                "strata": {
                    "fact_extraction/d2/ANSWERABLE": 4,
                    "comparison/d2/ANSWERABLE": 4,
                    "entity_identification/d2/ANSWERABLE": 4,
                }
            }
        ),
        encoding="utf-8",
    )
    estimated = score_round(
        ScoreInput(
            ratings_dir=packet, sheet_a=sheet_a, sheet_b=sheet_b, population=population
        )
    )
    assert estimated["population_estimate"]["status"] == "estimated"
    assert estimated["population_estimate"]["weighted_proportion"] == pytest.approx(1.0)

    missing = tmp_path / "missing.json"
    missing.write_text(
        json.dumps({"strata": {"lack_of_information/d1/INSUFFICIENT_EVIDENCE": 3}}),
        encoding="utf-8",
    )
    refused = score_round(
        ScoreInput(ratings_dir=packet, sheet_a=sheet_a, sheet_b=sheet_b, population=missing)
    )
    assert refused["population_estimate"]["status"] == "not_computed"
    assert "saknas i urvalet" in refused["population_estimate"]["reason"]


def test_unknown_sample_ids_and_invalid_codes_are_rejected(tmp_path):
    packet = _write_packet(tmp_path)
    sheet_a, sheet_b = _perfect_round(packet)
    bad = packet / "bad.csv"
    bad.write_text("sample_id;Q1_answerable;Q2_answer_correct\nfraud;ja;ja\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        score_round(ScoreInput(ratings_dir=packet, sheet_a=bad, sheet_b=sheet_b))

    invalid = packet / "invalid.csv"
    _write_sheet(invalid, [("cfq-000000000000", "ja", "kanske", "ja", "ej_tillamplig")])
    with pytest.raises(ValidationError):
        score_round(ScoreInput(ratings_dir=packet, sheet_a=invalid, sheet_b=sheet_b))


def test_reference_wilson_helper_stays_in_sync():
    """The engine and the protocol's worked examples must agree."""

    for successes, n in ((95, 100), (98, 100), (90, 100), (96, 98)):
        assert wilson_interval(successes, n) == pytest.approx(
            _wilson_reference(successes, n), abs=1e-12
        )


def test_cause_list_is_the_documented_six():
    """Code, protocol and report template must agree on the error causes."""

    from contractforge.quality import CAUSE_COUNT, CAUSES

    assert len(CAUSES) == CAUSE_COUNT == 6
    assert set(CAUSES) == {
        "frågeformulering",
        "facitvärde",
        "evidensspann",
        "validatorlucka",
        "genereringsfel",
        "juridisk_tolkning",
    }
    protocol = (Path(__file__).resolve().parents[1] / "docs" / "quality-protocol.md").read_text(
        encoding="utf-8"
    )
    template = (
        Path(__file__).resolve().parents[1] / "docs" / "quality-report-mall.md"
    ).read_text(encoding="utf-8")
    for cause in CAUSES:
        assert f"`{cause}`" in protocol, cause
        assert f"`{cause}`" in template, cause
    assert "sju orsaker" not in protocol
    assert "sju orsaker" not in template


def test_rater_models_are_recorded_verbatim(tmp_path):
    """Two different models must appear as such in the score and the report."""

    packet = _write_packet(tmp_path)
    sheet_a, sheet_b = _perfect_round(packet)

    payload = score_round(
        ScoreInput(
            ratings_dir=packet,
            sheet_a=sheet_a,
            sheet_b=sheet_b,
            rater_a_kind="model",
            rater_a_name="gpt-oss-120b",
            rater_b_kind="model",
            rater_b_name="qwen-3.8-27b",
        )
    )

    assert payload["reviewer"]["rater_a"] == {"kind": "model", "name": "gpt-oss-120b"}
    assert payload["reviewer"]["rater_b"] == {"kind": "model", "name": "qwen-3.8-27b"}
    report = (packet / "quality_report_draft.md").read_text(encoding="utf-8")
    assert "gpt-oss-120b" in report and "qwen-3.8-27b" in report
    assert "modellbaserad" in report


def test_axes_constant_is_complete():
    assert set(AXES) == {"Q1", "Q2", "Q3", "Q4"}
    assert set(AXIS_COLUMNS) == set(AXES)


def test_unfilled_packet_is_refused(tmp_path):
    """Scoring an untouched packet must fail instead of inventing a result."""

    packet = _write_packet(tmp_path)
    sheet_a = packet / "empty_a.csv"
    sheet_b = packet / "empty_b.csv"
    ids = [q["sample_id"] for q in json.loads((packet / "manifest.json").read_text())["questions"]]
    _write_sheet(sheet_a, [(sid, "", "", "", "") for sid in ids])
    _write_sheet(sheet_b, [(sid, "", "", "", "") for sid in ids])

    with pytest.raises(ValidationError, match="inget av urvalets"):
        score_round(ScoreInput(ratings_dir=packet, sheet_a=sheet_a, sheet_b=sheet_b))


def test_partially_coded_round_reports_completeness(tmp_path):
    packet = _write_packet(tmp_path)
    ids = [q["sample_id"] for q in json.loads((packet / "manifest.json").read_text())["questions"]]
    sheet_a = _write_sheet(
        packet / "a.csv", [(ids[0], "ja", "ja", "ja", "ej_tillamplig")]
    )
    sheet_b = _write_sheet(
        packet / "b.csv", [(ids[0], "ja", "ja", "ja", "ej_tillamplig")]
    )

    payload = score_round(ScoreInput(ratings_dir=packet, sheet_a=sheet_a, sheet_b=sheet_b))

    assert payload["completeness"]["complete"] is False
    assert payload["completeness"]["uncoded_posts"] == len(ids) - 1
    assert payload["gates_all_met"] is False
