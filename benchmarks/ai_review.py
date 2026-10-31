"""Two-pass blind review of a frozen question sample by two LLM raters.

This is a *measurement script*, not a generator feature. It implements the
review design agreed for ContractForge: two independent model raters, each
working in two passes on the frozen sample from ``publish-questions``:

**Pass 1 – independent answering.** The rater sees only the question and the
document package (no expected answer, no evidence). It answers from the
documents and cites the exact quote it relied on. This is the basis for
detecting "correct answer for the wrong reason" and for measuring how often the
model reaches the same answer as the gold independently.

**Pass 2 – gold review.** The rater is shown its own pass-1 answer plus
ContractForge's expected answer and evidence, and codes Q1–Q4 with the same
codes the human protocol uses. Seeing the gold only *after* its own answer is
the point of the two-pass design.

Rules the script enforces:

* raters never see each other's outputs (separate prompts, separate files);
* every call, prompt hash, usage figure and latency is recorded so the run can
  be frozen and audited;
* the run is deterministically reproducible (temperature 0, seed, fixed prompt
  versions) except for provider-side nondeterminism, which is documented;
* the output is a **preliminary** rating round: it can never be cited as human
  legal review, and the reservation that the raters are the same provider family
  (and one of them generated the corpus) is written into the run manifest.

Run it with::

    python benchmarks/ai_review.py \\
        --packet examples/qsample_live_100 \\
        --output benchmarks/results/ai_review_2026-10 \\
        --rater-a gpt-oss-120b --rater-b qwen-3.8-27b
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contractforge.config import Settings
from contractforge.models import LLMRequest, utc_now
from contractforge.providers import create_provider
from contractforge.utils import atomic_write_json, atomic_write_text, read_json, sha256_text

PASS1_PROMPT_VERSION = "ai-review-answer-v1"
PASS2_PROMPT_VERSION = "ai-review-verdict-v2"

PASS1_SYSTEM = """Du är en svensk avtalsjurist som besvarar en fråga om ett avtal.

Regler som alltid gäller:
1. Basera svaret ENDAST på dokumenttexten som ges. Använd ingen kunskap utanför dokumenten.
2. Läs HELA dokumentpaketet innan du svarar. Ett undantag, en motsägelse eller ett
   tillägg kan finnas i en annan klausul eller i en bilaga och kan ändra svaret.
3. Citera ordagrant den text du lutar dig mot (ett eller flera korta citat, exakt
   som de står i dokumentet).
4. Om dokumenten inte ger ett entydigt svar: sätt "answered" till false och förklara
   i "reason" varför (information saknas, eller flera värden motsäger varandra).
5. Svara ENDAST med ett JSON-objekt:
   {"answered": true|false, "answer": "<ditt svar>", "evidence_quote": "<ordagrant citat>", "reason": "<kort motivering>"}
"""

PASS2_SYSTEM = """Du granskar ett facit för en kontraktsfråga. Svara direkt med JSON.

Koda fyra axlar (en kod var):
Q1_answerable: kan frågan besvaras entydigt med enbart dokumenttexten? ja|nej|osäker
Q2_answer_correct: är facits svar det en noggrann jurist accepterar? ja|nej|osäker
Q3_evidence_sufficient: räcker facits citat för att motivera HELA svaret? ja|nej|osäker
Q4_anomaly_handling: hanterar facit undantag/motsägelser korrekt? ja|nej|ej_tillamplig
  (ej_tillamplig när dokumentet inte innehåller några undantag/motsägelser för frågan)

Regler: bedöm utifrån dokumenten, inte utifrån vad som verkar troligt. "osäker" endast
när du inte kan avgöra. Håll kommentaren till en mening.

Svara ENDAST med:
{"Q1_answerable": "...", "Q2_answer_correct": "...", "Q3_evidence_sufficient": "...",
 "Q4_anomaly_handling": "...", "comment": "..."}
"""

#: Free-text spellings models use, mapped to the protocol's codes.
VERDICT_ALIASES = {
    "ej tillämplig": "ej_tillamplig",
    "ej tillämpligt": "ej_tillamplig",
    "ej tillamplig": "ej_tillamplig",
    "ej tillämplig i detta fall": "ej_tillamplig",
    "inte tillämplig": "ej_tillamplig",
    "not applicable": "ej_tillamplig",
    "n/a": "ej_tillamplig",
}
VERDICT_CODES = {
    "Q1_answerable": {"ja", "nej", "osäker"},
    "Q2_answer_correct": {"ja", "nej", "osäker"},
    "Q3_evidence_sufficient": {"ja", "nej", "osäker"},
    "Q4_anomaly_handling": {"ja", "nej", "ej_tillamplig"},
}


@dataclass
class RaterConfig:
    rater_id: str
    model: str
    temperature: float = 0.0
    seed: int = 0
    #: qwen-3.8-27b spends completion budget on reasoning tokens; with 2 000 the
    #: pass-2 answers were sometimes cut off before any content was produced.
    max_tokens: int = 8000
    timeout_s: float = 240.0
    max_retries: int = 3
    concurrency: int = 3


@dataclass
class PassResult:
    sample_id: str
    rater_id: str
    model: str
    pass_name: str
    prompt_version: str
    response_text: str = ""
    parsed: dict[str, Any] = field(default_factory=dict)
    usage_total_tokens: int | None = None
    latency_ms: float = 0.0
    calls: int = 1
    attempts: int = 1
    error: str | None = None
    timestamp: str = field(default_factory=lambda: utc_now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.__dict__)
        payload.pop("calls", None)
        return payload


def _document_package(question: dict[str, Any]) -> str:
    parts = []
    for document_id, text in question["document_texts"].items():
        parts.append(f'=== DOKUMENT: "{document_id}" ===\n{text}')
    return "\n\n".join(parts)


def _prompt_hash(system: str, user: str) -> str:
    return sha256_text(system + "\n\x1f\n" + user)


def build_pass1_request(rater: RaterConfig, question: dict[str, Any]) -> LLMRequest:
    user = (
        f"FRÅGA: {question['question']}\n\n"
        f"DOKUMENTPAKET (avtalets samtliga dokument):\n{_document_package(question)}\n"
    )
    return LLMRequest(
        provider="cerebras",
        model=rater.model,
        prompt_version=PASS1_PROMPT_VERSION,
        system=PASS1_SYSTEM,
        user=user,
        temperature=rater.temperature,
        top_p=None,
        max_tokens=rater.max_tokens,
        seed=rater.seed,
        response_format="json",
        metadata={"sample_id": question["sample_id"], "pass": "answer", "prompt_sha256": _prompt_hash(PASS1_SYSTEM, user)},
    )


def build_pass2_request(
    rater: RaterConfig,
    question: dict[str, Any],
    own_answer: dict[str, Any],
) -> LLMRequest:
    expected = json.dumps(question["expected_answer_structured"], ensure_ascii=False)
    user = (
        f"FRÅGA: {question['question']}\n\n"
        f"DOKUMENTPAKET (avtalets samtliga dokument):\n{_document_package(question)}\n\n"
        f"FRÅGANS KATEGORI (för kontext): {question['category']}\n\n"
        f"DITT EGEN SVAR (utan att ha sett facit): {json.dumps(own_answer, ensure_ascii=False)}\n\n"
        f"FACITS FÖRVÄNTADE SVAR: {expected}\n"
        f"FACITS EVIDENS (ordagranna citat): {json.dumps(question['evidence_quotes'], ensure_ascii=False)}\n"
    )
    return LLMRequest(
        provider="cerebras",
        model=rater.model,
        prompt_version=PASS2_PROMPT_VERSION,
        system=PASS2_SYSTEM,
        user=user,
        temperature=rater.temperature,
        top_p=None,
        max_tokens=rater.max_tokens,
        seed=rater.seed,
        response_format="json",
        metadata={"sample_id": question["sample_id"], "pass": "verdict", "prompt_sha256": _prompt_hash(PASS2_SYSTEM, user)},
    )


def _normalise_verdict(parsed: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Extract the four codes, also when the model wraps them in prose."""

    problems: list[str] = []
    codes: dict[str, str] = {}
    for column, allowed in VERDICT_CODES.items():
        raw = str(parsed.get(column, parsed.get(column.lower(), ""))).strip().lower()
        if raw in VERDICT_ALIASES and VERDICT_ALIASES[raw] in allowed:
            codes[column] = VERDICT_ALIASES[raw]
            continue
        if raw in allowed:
            codes[column] = raw
            continue
        match = next((code for code in sorted(allowed, key=len, reverse=True) if code in raw), None)
        if match is not None:
            codes[column] = match
        else:
            codes[column] = ""
            problems.append(f"{column}={raw!r} (tillåtna: {sorted(allowed)})")
    return codes, problems


def _parse_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def run_pass(
    provider,
    rater: RaterConfig,
    request_builder,
    questions: list[dict[str, Any]],
    *,
    pass_name: str,
    progress,
    previous: dict[str, dict[str, Any]] | None = None,
) -> list[PassResult]:
    results: list[PassResult] = []
    total = len(questions)
    for index, question in enumerate(questions, start=1):
        sample_id = question["sample_id"]
        request = request_builder(rater, question, previous) if previous is not None else request_builder(rater, question)
        result = PassResult(
            sample_id=sample_id,
            rater_id=rater.rater_id,
            model=rater.model,
            pass_name=pass_name,
            prompt_version=request.prompt_version,
        )
        started = time.perf_counter()
        try:
            response = provider.complete(request)
            result.response_text = response.text
            result.latency_ms = response.latency_ms
            result.usage_total_tokens = response.usage.total_tokens
            parsed = _parse_json(response.text)
            if parsed is None:
                result.error = "svaret var inte ett JSON-objekt"
            else:
                result.parsed = parsed
        except Exception as exc:  # noqa: BLE001 - the run records every failure
            result.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            result.latency_ms = (time.perf_counter() - started) * 1000
        results.append(result)
        progress(f"{rater.rater_id} {pass_name}: {index}/{total} {sample_id} "
                 f"{'OK' if result.error is None else 'FEL ' + str(result.error)[:60]}")
    return results


def run_ai_review(
    packet_dir: Path,
    output_dir: Path,
    raters: list[RaterConfig],
    *,
    settings: Settings,
    resume: bool = False,
    progress=print,
) -> dict[str, Any]:
    """Run both passes for every rater and write the frozen artefacts."""

    packet_dir = Path(packet_dir)
    output_dir = Path(output_dir)
    manifest = read_json(packet_dir / "manifest.json")
    questions = manifest["questions"]
    output_dir.mkdir(parents=True, exist_ok=True)

    run_meta: dict[str, Any] = {
        "schema_version": "1.0",
        "kind": "ai_review_run",
        "created_at": utc_now().isoformat(),
        "packet": {
            "path": str(packet_dir),
            "source_sha256": manifest.get("source_sha256"),
            "selection_sha256": manifest.get("selection_sha256"),
            "sample_size": len(questions),
        },
        "prompts": {
            "pass1": {"version": PASS1_PROMPT_VERSION, "sha256": sha256_text(PASS1_SYSTEM)},
            "pass2": {"version": PASS2_PROMPT_VERSION, "sha256": sha256_text(PASS2_SYSTEM)},
        },
        "raters": [
            {
                "rater_id": rater.rater_id,
                "model": rater.model,
                "temperature": rater.temperature,
                "seed": rater.seed,
                "max_tokens": rater.max_tokens,
                "provider": "cerebras",
            }
            for rater in raters
        ],
        "reservation": (
            "PRELIMINÄR MODELLBASERAD GRANSKNING. Raterna är språkmodeller via Cerebras; "
            "en av dem (gpt-oss-120b) genererade dessutom korpusen. Deras enighet är därför "
            "inget bevis för juridisk korrekthet och får inte ersätta mänsklig granskning."
        ),
        "protocol": "docs/quality-protocol.md (7.4–7.6): separat, blind, två pass",
    }

    all_results: dict[str, list[PassResult]] = {}
    for rater in raters:
        provider = create_provider(
            settings.with_overrides(model=rater.model, max_retries=rater.max_retries)
        )
        try:
            progress(f"== Rater {rater.rater_id}: {rater.model} – pass 1 (självständigt svar)")
            pass1_path = output_dir / f"{rater.rater_id}_pass1.json"
            if resume and pass1_path.is_file():
                raw1 = read_json(pass1_path)
                pass1 = [PassResult(**item) for item in raw1["results"]]
                failed = [item for item in pass1 if item.error]
                progress(
                    f"   återanvänder {len(pass1) - len(failed)} pass-1-svar"
                    + (f", försöker om {len(failed)} misslyckade" if failed else "")
                )
                if failed:
                    by_id = {item.sample_id: item for item in failed}
                    retried = run_pass(
                        provider, rater, build_pass1_request,
                        [q for q in questions if q["sample_id"] in by_id],
                        pass_name="pass1_answer", progress=progress,
                    )
                    retried_by_id = {item.sample_id: item for item in retried}
                    pass1 = [
                        retried_by_id.get(item.sample_id, item)
                        if item.error
                        else item
                        for item in pass1
                    ]
                    atomic_write_json(pass1_path, {
                        "rater": rater.__dict__, "pass": "pass1_answer",
                        "prompt_version": PASS1_PROMPT_VERSION,
                        "prompt_sha256": sha256_text(PASS1_SYSTEM),
                        "results": [item.to_dict() for item in pass1],
                    })
            else:
                pass1 = run_pass(
                    provider, rater, build_pass1_request, questions,
                    pass_name="pass1_answer", progress=progress,
                )
                atomic_write_json(pass1_path, {
                    "rater": rater.__dict__, "pass": "pass1_answer",
                    "prompt_version": PASS1_PROMPT_VERSION, "prompt_sha256": sha256_text(PASS1_SYSTEM),
                    "results": [item.to_dict() for item in pass1],
                })
            own_answers = {
                item.sample_id: item.parsed for item in pass1 if item.error is None
            }

            progress(f"== Rater {rater.rater_id}: {rater.model} – pass 2 (facitgranskning)")
            pass2_path = output_dir / f"{rater.rater_id}_pass2.json"
            if resume and pass2_path.is_file():
                raw2 = read_json(pass2_path)
                pass2 = [PassResult(**item) for item in raw2["results"]]
                failed = [item for item in pass2 if item.error]
                progress(
                    f"   återanvänder {len(pass2) - len(failed)} pass-2-bedömningar"
                    + (f", försöker om {len(failed)} misslyckade" if failed else "")
                )
                if failed:
                    by_id = {item.sample_id for item in failed}
                    retried = run_pass(
                        provider, rater,
                        lambda rat, q, prev: build_pass2_request(
                            rat, q, prev.get(q["sample_id"], {})
                        ),
                        [q for q in questions if q["sample_id"] in by_id],
                        pass_name="pass2_verdict",
                        progress=progress,
                        previous=own_answers,
                    )
                    retried_by_id = {item.sample_id: item for item in retried}
                    pass2 = [
                        retried_by_id.get(item.sample_id, item)
                        if item.error
                        else item
                        for item in pass2
                    ]
                    atomic_write_json(pass2_path, {
                        "rater": rater.__dict__, "pass": "pass2_verdict",
                        "prompt_version": PASS2_PROMPT_VERSION,
                        "prompt_sha256": sha256_text(PASS2_SYSTEM),
                        "results": [item.to_dict() for item in pass2],
                    })
            else:
                pass2 = run_pass(
                    provider, rater,
                    lambda rat, q, prev: build_pass2_request(rat, q, prev.get(q["sample_id"], {})),
                    questions,
                    pass_name="pass2_verdict",
                    progress=progress,
                    previous=own_answers,
                )
                atomic_write_json(pass2_path, {
                    "rater": rater.__dict__, "pass": "pass2_verdict",
                    "prompt_version": PASS2_PROMPT_VERSION, "prompt_sha256": sha256_text(PASS2_SYSTEM),
                    "results": [item.to_dict() for item in pass2],
                })
            all_results[rater.rater_id] = pass1 + pass2
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()

    _write_sheets(output_dir, questions, raters, all_results)
    comparison = _compare_own_answers(questions, raters, all_results)
    run_meta["comparison"] = comparison
    run_meta["cost"] = _usage_summary(all_results)
    atomic_write_json(output_dir / "run.json", run_meta)
    return run_meta


def _write_sheets(
    output_dir: Path,
    questions: list[dict[str, Any]],
    raters: list[RaterConfig],
    results: dict[str, list[PassResult]],
) -> None:
    """Write the Q1–Q4 sheet per rater in the format ``score-ratings`` reads."""

    header = [
        "sample_id",
        "question",
        "expected_answer",
        "evidence_quote",
        "Q1_answerable",
        "Q2_answer_correct",
        "Q3_evidence_sufficient",
        "Q4_anomaly_handling",
        "comment",
    ]
    for rater in raters:
        rows: list[list[str]] = [header]
        verdicts = {
            item.sample_id: item for item in results[rater.rater_id] if item.pass_name == "pass2_verdict"
        }
        for question in questions:
            verdict = verdicts.get(question["sample_id"])
            codes, problems = _normalise_verdict(verdict.parsed if verdict else {})
            comment = str((verdict.parsed or {}).get("comment", "")) if verdict else ""
            if verdict and verdict.error:
                codes = dict.fromkeys(VERDICT_CODES, "")
                comment = f"FEL: {verdict.error[:200]}"
            elif problems:
                comment = (comment + " | " + "; ".join(problems)).strip(" |")
            rows.append(
                [
                    question["sample_id"],
                    question["question"],
                    question["expected_answer"],
                    question["evidence_quote"],
                    codes["Q1_answerable"],
                    codes["Q2_answer_correct"],
                    codes["Q3_evidence_sufficient"],
                    codes["Q4_anomaly_handling"],
                    comment,
                ]
            )
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerows(rows)
        atomic_write_text(
            output_dir / f"ratings_{rater.rater_id}_ai.csv",
            buffer.getvalue(),
            encoding="utf-8-sig",
        )


def _normalise_answer(text: str) -> str:
    return " ".join(str(text).lower().replace("\u00a0", " ").split())


def own_answer_matches_gold(own_answer: str, gold_structured: dict[str, Any]) -> tuple[bool, str]:
    """Does the rater's independent answer carry the same facts as the gold?

    A plain substring test is far too strict: the gold stores normalised values
    (``1150 (SEK)``, ``2026-01-15``) while a model answers in natural language
    (``1 150 kr per timme``, ``15 januari 2026``). The comparison uses the
    project's own deterministic normaliser (the same one the verifier uses) and
    falls back to text containment for entities and lists.

    Returns ``(matches, method)``.
    """

    from contractforge.validation import semantic

    answer = _normalise_answer(own_answer)
    if not answer:
        return False, "tomt svar"
    values: list[Any] = []
    if gold_structured.get("value") is not None:
        values.append(gold_structured["value"])
    values.extend(gold_structured.get("values") or [])
    if gold_structured.get("text"):
        values.append(gold_structured["text"])
    if not values:
        return False, "facit saknar jämförbart värde"

    kind = str(gold_structured.get("kind") or "")
    # Dates are strings in the gold ("2026-01-15") but raters answer in prose
    # ("15 januari 2026"): reuse the verifier's localised date parser instead of
    # comparing strings.
    try:
        from contractforge.models import Fact, FactKind

        gold_fact = Fact(
            fact_id="gold",
            kind=FactKind(kind) if kind else _infer_kind(gold_structured),
            value=gold_structured.get("value"),
            unit=gold_structured.get("unit"),
            currency=gold_structured.get("currency"),
            label_sv="gold",
        )
        analysis = semantic.analyze_fact(gold_fact, own_answer, "sv")
        if analysis.semantic_span is not None or analysis.explicit_span is not None:
            return True, analysis.keyword or "semantisk träff"
    except Exception:  # noqa: BLE001 - comparison must never crash the run
        pass

    unit = gold_structured.get("unit")
    currency = gold_structured.get("currency")
    if unit or currency:
        expected = _gold_quantity(gold_structured, semantic)
        if expected is not None:
            found = semantic.extract_quantities(own_answer, "sv")
            if any(item.unit == expected.unit and item.value == expected.value for item in found):
                return True, "numerisk normalisering"
            return False, "numerisk normalisering"
    for value in values:
        needle = _normalise_answer(str(value))
        if needle and (needle in answer or answer in needle):
            return True, "textinnehåll"
        if needle and all(token in answer for token in needle.split() if len(token) > 2):
            return True, "ordmängd"
    return False, "textinnehåll"


def _infer_kind(gold_structured: dict[str, Any]):
    from contractforge.models import FactKind

    if gold_structured.get("currency"):
        return FactKind.MONEY
    if gold_structured.get("unit") in _DURATION_UNITS:
        return FactKind.DURATION
    return FactKind.TEXT


def _gold_quantity(gold_structured: dict[str, Any], semantic):
    from contractforge.models import Fact, FactKind

    kind = FactKind.DURATION if gold_structured.get("unit") in _DURATION_UNITS else FactKind.MONEY
    fact = Fact(
        fact_id="gold",
        kind=kind,
        value=gold_structured.get("value"),
        unit=gold_structured.get("unit"),
        currency=gold_structured.get("currency"),
        label_sv="gold",
    )
    return semantic.canonical_quantity_for_fact(fact)


_DURATION_UNITS = {
    "months", "month", "years", "year", "weeks", "week", "days", "day",
    "hours", "hour", "minuter", "minute", "månader", "år", "veckor", "dagar", "timmar",
}


def _compare_own_answers(
    questions: list[dict[str, Any]],
    raters: list[RaterConfig],
    results: dict[str, list[PassResult]],
) -> dict[str, Any]:
    """Compare the raters' independent answers with the gold and with each other."""

    by_rater = {
        rater.rater_id: {
            item.sample_id: item for item in results[rater.rater_id] if item.pass_name == "pass1_answer"
        }
        for rater in raters
    }
    gold_contains: dict[str, dict[str, bool]] = {}
    agree_between_raters = 0
    comparable = 0
    mismatch_cases = []
    for question in questions:
        sample_id = question["sample_id"]
        per_rater: dict[str, bool] = {}
        answers = {}
        for rater in raters:
            item = by_rater[rater.rater_id].get(sample_id)
            answer = _normalise_answer((item.parsed or {}).get("answer", "")) if item else ""
            answers[rater.rater_id] = answer
            matches, _method = own_answer_matches_gold(
                (item.parsed or {}).get("answer", "") if item else "",
                question["expected_answer_structured"],
            )
            per_rater[rater.rater_id] = matches
        gold_contains[sample_id] = per_rater
        if len(raters) == 2:
            ids = [rater.rater_id for rater in raters]
            if answers[ids[0]] and answers[ids[1]]:
                comparable += 1
                if answers[ids[0]] == answers[ids[1]]:
                    agree_between_raters += 1
            # Raters whose own answers differ from gold in the same way.
            if (
                all(not per_rater[rater_id] for rater_id in ids)
                and all(answers[rater_id] for rater_id in ids)
                and answers[ids[0]] == answers[ids[1]]
            ):
                mismatch_cases.append(sample_id)
    return {
        "per_sample": {sample_id: gold_contains[sample_id] for sample_id in gold_contains},
        "own_answer_matches_gold": {
            rater.rater_id: {
                "n": len(questions),
                "matches": sum(
                    1 for sample_id, per in gold_contains.items() if per.get(rater.rater_id)
                ),
            }
            for rater in raters
        },
        "raters_agree_on_own_answer": {
            "comparable": comparable,
            "agree": agree_between_raters,
            "rate": (agree_between_raters / comparable) if comparable else None,
        },
        "both_own_answers_differ_from_gold_but_agree_with_each_other": mismatch_cases,
    }


def _usage_summary(results: dict[str, list[PassResult]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for rater_id, items in results.items():
        tokens = sum(item.usage_total_tokens or 0 for item in items)
        latencies = [item.latency_ms for item in items if item.error is None]
        summary[rater_id] = {
            "calls": len(items),
            "errors": sum(1 for item in items if item.error is not None),
            "total_tokens": tokens,
            "mean_latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
        }
    summary["total_tokens"] = sum(v["total_tokens"] for v in summary.values())
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", required=True, help="mätpaket från publish-questions")
    parser.add_argument("--output", required=True, help="katalog för frysta resultat")
    parser.add_argument("--rater-a", default="gpt-oss-120b", help="modell för ratare A")
    parser.add_argument("--rater-b", default="qwen-3.8-27b", help="modell för ratare B")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--resume", action="store_true", help="återuppta en avbruten körning")
    args = parser.parse_args(argv)

    if not (Path(args.packet) / "manifest.json").is_file():
        print(f"Fel: {args.packet} saknar manifest.json")
        return 2
    settings = Settings.from_env()
    if not settings.api_key:
        print("Fel: CEREBRAS_API_KEY saknas")
        return 5
    raters = [
        RaterConfig("A", args.rater_a, temperature=args.temperature, seed=args.seed, max_tokens=args.max_tokens),
        RaterConfig("B", args.rater_b, temperature=args.temperature, seed=args.seed, max_tokens=args.max_tokens),
    ]
    run_meta = run_ai_review(
        Path(args.packet), Path(args.output), raters, settings=settings, resume=args.resume
    )
    print(json.dumps(run_meta["comparison"]["own_answer_matches_gold"], ensure_ascii=False, indent=2))
    print(f"Resultat: {Path(args.output) / 'run.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
