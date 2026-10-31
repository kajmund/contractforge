#!/usr/bin/env python3
"""Reproduce the twelve review findings (F1–F12) against the current commit.

The quality measurement says whether the gold is right. It does **not** say
whether the defects found in the v0.1.0 review are fixed – that has to be
re-tested separately, on the code that will be used.

This script is deliberately self-contained: one check per finding, each with a
short reproduction, a pass/fail verdict and the evidence. It can be run against
a clean checkout with::

    python benchmarks/review_checks.py            # human summary
    python benchmarks/review_checks.py --json     # machine-readable

Exit code 0 means every check passed. A failing check prints what failed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ZIP = REPO_ROOT / "examples" / "qsample_live_100"
LIVE_CORPUS = REPO_ROOT / "benchmarks" / "results" / "cerebras_consulting_100" / "dataset"


class Check:
    def __init__(self, finding: str, title: str) -> None:
        self.finding = finding
        self.title = title
        self.passed = False
        self.detail = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "finding": self.finding,
            "title": self.title,
            "passed": self.passed,
            "detail": self.detail,
        }


def _mock_settings(formats: str = "txt", **extra: str):
    from contractforge.config import Settings

    env = {
        "CONTRACTFORGE_PROVIDER": "mock",
        "CONTRACTFORGE_FORMATS": formats,
        "CONTRACTFORGE_CONCURRENCY": "1",
    }
    env.update(extra)
    return Settings.from_env(env)


def check_f1(tmp: Path) -> Check:
    """Level 5/6 datasets must validate; generate must fail if they do not."""
    from contractforge.models import ContractType, Language, ScenarioRequest
    from contractforge.pipeline import generate_dataset

    check = Check("F1", "Shared question validator + final dataset validation")
    request = ScenarioRequest(
        contract_type=ContractType.CONSULTING,
        count=1,
        clauses=10,
        difficulty=5,
        language=Language.SV,
        seed=77,
    )
    outcome = generate_dataset(_mock_settings(), request, tmp / "f1")
    validated = outcome.validation_report is not None and outcome.validation_report.status.value == "PASS"
    exported = sum(len(result.questions) for result in outcome.results)
    same = exported == outcome.stats.questions_validated
    check.passed = validated and same
    check.detail = (
        f"generate validation={outcome.validation_report.status.value if outcome.validation_report else 'none'}, "
        f"exported={exported}, validated={outcome.stats.questions_validated}"
    )
    return check


def check_f2(tmp: Path) -> Check:
    """Every reference with evidence must be a tight span naming the target."""
    from contractforge.config import Settings as _S  # noqa: F401  (documentation only)
    from contractforge.generation.engine import DocumentGenerator
    from contractforge.gold import build_gold_set
    from contractforge.models import ContractType, Language, ScenarioRequest
    from contractforge.providers import create_provider
    from contractforge.rendering.canonical import render_package
    from contractforge.scenarios.generator import ScenarioGenerator
    from contractforge.scenarios.registry import load_registry

    check = Check("F2", "Cross-references need textual support; no fallback spans")
    settings = _mock_settings()
    generator = ScenarioGenerator(load_registry())
    provider = create_provider(settings)
    named = not_named = unsupported_in_package = 0
    for contract_type in ContractType:
        for difficulty in (1, 3, 5, 6):
            scenario = generator.generate(
                ScenarioRequest(
                    contract_type=contract_type,
                    count=1,
                    clauses=10,
                    difficulty=difficulty,
                    language=Language.SV,
                    seed=42,
                ),
                0,
            )
            outcome = DocumentGenerator(provider, settings).generate(scenario)
            rendered = render_package(scenario, outcome.package)
            gold = build_gold_set(scenario, outcome.package, rendered)
            titles = {doc.document_id: doc.title(Language.SV) for doc in scenario.documents}
            for reference in gold.references:
                if not reference.evidence:
                    if reference.target_document_id not in scenario.missing_documents:
                        unsupported_in_package += 1
                    continue
                for span in reference.evidence:
                    text = rendered[span.document_id].canonical_text[span.start : span.end]
                    target = reference.target_document_id
                    if target in text or (titles.get(target) and titles[target] in text):
                        named += 1
                    else:
                        not_named += 1
    provider.close()
    check.passed = not_named == 0 and unsupported_in_package == 0 and named > 0
    check.detail = (
        f"named={named}, not_named={not_named}, "
        f"in-package references without evidence={unsupported_in_package}"
    )
    return check


def check_f3() -> Check:
    """A replacement value must FAIL; a non-binding distractor only WARNs."""
    from contractforge.validation import validate_package

    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from factories import make_consulting_scenario, make_generated_package, make_rendered_documents

    def set_text(package, clause_id, text):
        document = package.document("main_agreement")
        clauses = [
            clause.model_copy(update={"text": text}) if clause.clause_id == clause_id else clause
            for clause in document.clauses
        ]
        updated = document.model_copy(update={"clauses": clauses})
        return package.model_copy(update={"documents": [updated]})

    scenario = make_consulting_scenario()
    base = make_generated_package(scenario)
    party_text = "Parterna Nordvik Teknik AB och Sundberg Konsult AB ingår avtalet."
    base = set_text(base, "clause_01", party_text)
    binding = set_text(
        base,
        "clause_02",
        "Timarvodet är 950 SEK. Detta villkor ersätts härmed: det avtalade beloppet är 9 999 SEK.",
    )
    distractor = set_text(
        base,
        "clause_02",
        "Enligt ett tidigare utkast diskuterades ett alternativt timarvode om 9 999 SEK; "
        "det avtalade timarvodet är 950 SEK.",
    )
    binding_status = validate_package(
        scenario, binding, make_rendered_documents(scenario, binding)
    ).status.value
    distractor_status = validate_package(
        scenario, distractor, make_rendered_documents(scenario, distractor)
    ).status.value
    check = Check("F3", "Operator-aware verification (replacement vs distractor)")
    check.passed = binding_status == "REJECTED" and distractor_status in ("ACCEPTED", "REPAIRED")
    check.detail = f"binding (ersätts härmed)={binding_status}, non-binding distractor={distractor_status}"
    return check


def check_f4() -> Check:
    """Failed attempts must be booked; unknown usage must not cost 0.0."""
    from contractforge.models import (
        LLMCallRecord,
        LLMRequest,
        ModelPrice,
        PricingTable,
        Usage,
    )
    from contractforge.pipeline import summarize_cost

    pricing = PricingTable(
        providers={"mock": {"mock-1": ModelPrice(input_usd_per_million=1.0, output_usd_per_million=2.0)}}
    )
    settings = _mock_settings().with_overrides(pricing=pricing)
    request = LLMRequest(
        provider="mock", model="mock-1", prompt_version="v1", system="s", user="u"
    )
    unknown = LLMCallRecord(
        call_id="c1", purpose="generate_document", contract_id="c", request=request,
        response_text="x", usage=Usage(), provider="mock", model="mock-1",
    )
    known = LLMCallRecord(
        call_id="c2", purpose="generate_document", contract_id="c", request=request,
        response_text="x",
        usage=Usage(input_tokens=1_000_000, output_tokens=0, total_tokens=1_000_000),
        provider="mock", model="mock-1",
    )
    unknown_cost = summarize_cost([unknown], settings)
    known_cost = summarize_cost([known], settings)
    check = Check("F4", "Call accounting before parsing; honest unknown usage")
    check.passed = (
        unknown_cost.cost_usd is None
        and unknown_cost.cost_basis == "unknown"
        and known_cost.cost_usd is not None
    )
    check.detail = (
        f"unknown usage -> cost_usd={unknown_cost.cost_usd}, basis={unknown_cost.cost_basis}; "
        f"known usage -> cost_usd={known_cost.cost_usd}"
    )
    return check


def check_f5() -> Check:
    """Credentials echoed by the provider must never reach message/details/files."""
    import httpx

    from contractforge.errors import ProviderError
    from contractforge.models import LLMRequest
    from contractforge.providers.cerebras import CerebrasProvider

    secret = "csk-review-secret-1234567890abcdef"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.dumps({"error": "bad request", "echo": f"Authorization: Bearer {secret}"})
        return httpx.Response(400, text=body)

    provider = CerebrasProvider(
        api_key="csk-unit-test", base_url="https://api.cerebras.ai/v1", model="m",
        timeout_s=5, max_retries=0, backoff_s=0.0, temperature=0.0, top_p=None,
        max_tokens=10, transport=httpx.MockTransport(handler), sleep=lambda _s: None,
    )
    leaked = False
    redacted = False
    try:
        provider.complete(
            LLMRequest(provider="cerebras", model="m", prompt_version="v1", system="s", user="u")
        )
    except ProviderError as exc:
        blob = exc.message + json.dumps(exc.details) + json.dumps(exc.to_dict())
        leaked = secret in blob
        redacted = "[REDACTED]" in blob
    finally:
        provider.close()
    check = Check("F5", "Provider error bodies are scrubbed everywhere")
    check.passed = not leaked and redacted
    check.detail = f"secret in message/details/envelope={leaked}, redaction marker present={redacted}"
    return check


def check_f6(tmp: Path) -> Check:
    """Foreign directories are never deleted; a changed prefix is refused."""
    from contractforge.errors import UsageError
    from contractforge.models import ContractType, Language, ScenarioRequest
    from contractforge.pipeline import generate_dataset

    settings = _mock_settings()

    def request(**extra):
        base = {
            "contract_type": ContractType.CONSULTING,
            "count": 1,
            "clauses": 6,
            "difficulty": 2,
            "language": Language.SV,
            "seed": 11,
        }
        base.update(extra)
        return ScenarioRequest(**base)

    foreign = tmp / "foreign"
    (foreign / "contract_notes").mkdir(parents=True)
    keep = foreign / "contract_notes" / "keep.txt"
    keep.write_text("mine", encoding="utf-8")
    refused = False
    try:
        generate_dataset(settings, request(), foreign)
    except UsageError:
        refused = True
    survived = keep.is_file() and keep.read_text(encoding="utf-8") == "mine"

    dataset = tmp / "dataset"
    generate_dataset(settings, request(count=2), dataset)
    prefix_refused = False
    try:
        generate_dataset(settings, request(count=1, contract_id_prefix="other"), dataset)
    except UsageError:
        prefix_refused = True
    remaining = sorted(entry.name for entry in dataset.iterdir() if entry.is_dir())
    check = Check("F6", "Output safety: no deletion of user data")
    check.passed = refused and survived and prefix_refused
    check.detail = (
        f"foreign dir refused={refused}, user file survived={survived}, "
        f"prefix change refused={prefix_refused}, dataset dirs={remaining}"
    )
    return check


def check_f7(tmp: Path) -> Check:
    """An exact evidence span must equal its quote; widening it must fail."""
    import shutil

    from contractforge.models import ContractType, Language, ScenarioRequest
    from contractforge.pipeline import generate_dataset
    from contractforge.utils import sha256_file
    from contractforge.validation.dataset import validate_dataset

    source = tmp / "live_corpus"
    if LIVE_CORPUS.is_dir():
        shutil.copytree(LIVE_CORPUS, source)
    else:  # pragma: no cover - corpus directory is gitignored
        request = ScenarioRequest(
            contract_type=ContractType.CONSULTING, count=2, clauses=8, difficulty=3,
            language=Language.SV, seed=7,
        )
        generate_dataset(_mock_settings(), request, source)
    baseline = validate_dataset(source).status.value
    contract = sorted(p for p in source.iterdir() if p.is_dir() and p.name.startswith("contract_"))[0]
    gold = json.loads((contract / "gold_facts.json").read_text(encoding="utf-8"))
    fact = next(item for item in gold["facts"] if item["evidence"] and item["precision"] == "exact")
    span = fact["evidence"][0]
    span["start"] = max(0, span["start"] - 60)
    span["end"] = span["end"] + 60
    (contract / "gold_facts.json").write_text(json.dumps(gold), encoding="utf-8")
    manifest = json.loads((contract / "manifest.json").read_text(encoding="utf-8"))
    for artifact in manifest["artifacts"]:
        path = contract / artifact["path"]
        if path.exists():
            artifact["sha256"] = sha256_file(path)
            artifact["size_bytes"] = path.stat().st_size
    (contract / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    dataset_manifest = json.loads((source / "dataset_manifest.json").read_text(encoding="utf-8"))
    for record in dataset_manifest["contracts"]:
        record["manifest_sha256"] = sha256_file(source / record["path"] / "manifest.json")
    (source / "dataset_manifest.json").write_text(json.dumps(dataset_manifest), encoding="utf-8")
    widened = validate_dataset(source).status.value
    shutil.rmtree(source, ignore_errors=True)
    check = Check("F7", "Exact evidence spans must equal the canonical slice")
    check.passed = baseline == "PASS" and widened == "FAIL"
    check.detail = f"baseline={baseline}, after widening an exact span={widened}"
    return check


def check_f8(tmp: Path) -> Check:
    """Only missing_information facts may lack evidence."""
    from contractforge.generation.engine import DocumentGenerator
    from contractforge.gold import build_gold_set
    from contractforge.models import Answerability, ContractType, Language, ScenarioRequest
    from contractforge.providers import create_provider
    from contractforge.rendering.canonical import render_package
    from contractforge.scenarios.generator import ScenarioGenerator
    from contractforge.scenarios.registry import load_registry

    settings = _mock_settings()
    generator = ScenarioGenerator(load_registry())
    provider = create_provider(settings)
    conditional = 0
    missing_ok = 0
    problems = 0
    for contract_type in ContractType:
        scenario = generator.generate(
            ScenarioRequest(
                contract_type=contract_type, count=1, clauses=10, difficulty=6,
                language=Language.SV, seed=3,
            ),
            0,
        )
        outcome = DocumentGenerator(provider, settings).generate(scenario)
        rendered = render_package(scenario, outcome.package)
        gold = build_gold_set(scenario, outcome.package, rendered)
        by_id = {fact.fact_id: fact for fact in gold.facts}
        for anomaly in scenario.anomalies:
            for fact_id in anomaly.fact_ids:
                gold_fact = by_id.get(fact_id)
                if gold_fact is None:
                    continue
                if anomaly.answerability is Answerability.CONDITIONAL:
                    conditional += 1
                    if not gold_fact.evidence:
                        problems += 1
                elif anomaly.kind.value == "missing_information":
                    missing_ok += 1
    provider.close()
    check = Check("F8", "Conditional facts keep evidence; only missing info is exempt")
    check.passed = problems == 0 and conditional > 0
    check.detail = f"conditional facts checked={conditional}, without evidence={problems}, missing_info facts={missing_ok}"
    return check


def check_f9() -> Check:
    """Synthetic organisation numbers must be Luhn-invalid."""
    from contractforge.models import ContractType, Language, ScenarioRequest
    from contractforge.scenarios.generator import ScenarioGenerator
    from contractforge.scenarios.registry import load_registry

    def luhn_valid(number: str) -> bool:
        digits = [int(char) for char in number if char.isdigit()][::-1]
        total = 0
        for index, digit in enumerate(digits):
            if index % 2 == 1:
                digit *= 2
                if digit > 9:
                    digit -= 9
            total += digit
        return total % 10 == 0

    generator = ScenarioGenerator(load_registry())
    total = valid = 0
    for seed in range(60):
        scenario = generator.generate(
            ScenarioRequest(
                contract_type=ContractType.CONSULTING, count=1, clauses=8, difficulty=1,
                language=Language.SV, seed=seed,
            ),
            0,
        )
        for party in scenario.parties:
            if party.org_number:
                total += 1
                valid += luhn_valid(party.org_number)
    check = Check("F9", "Synthetic org numbers are Luhn-invalid")
    check.passed = valid == 0 and total > 0
    check.detail = f"Luhn-valid {valid}/{total}"
    return check


def check_f10() -> Check:
    """Benchmark reports must carry hashes, runtime and validation evidence."""
    report_path = REPO_ROOT / "benchmarks" / "results" / "cerebras_consulting_100.json"
    if not report_path.is_file():
        check = Check("F10", "Benchmark reports are verifiable")
        check.passed = False
        check.detail = "benchmarks/results/cerebras_consulting_100.json saknas"
        return check
    report = json.loads(report_path.read_text(encoding="utf-8"))
    details = report.get("details") or {}
    required = ("dataset_manifest_sha256", "validation", "questions_accepted", "runtime", "throughput")
    missing = [key for key in required if key not in details]
    check = Check("F10", "Benchmark reports are verifiable")
    check.passed = not missing and bool(details.get("dataset_manifest_sha256"))
    check.detail = f"missing details fields={missing}"
    return check


def check_f11(tmp: Path) -> Check:
    """Two runs differing only in scenario inputs must not share a snapshot."""
    from contractforge.models import ContractType, Language, ScenarioRequest
    from contractforge.pipeline import generate_dataset

    settings = _mock_settings()
    first = tmp / "f11a"
    second = tmp / "f11b"
    base = {
        "contract_type": ContractType.CONSULTING,
        "count": 1,
        "clauses": 8,
        "difficulty": 4,
        "language": Language.SV,
        "seed": 9,
    }
    generate_dataset(settings, ScenarioRequest(**base, package=True), first)
    generate_dataset(settings, ScenarioRequest(**base, package=False), second)
    cfg_a = json.loads((first / "dataset_manifest.json").read_text(encoding="utf-8"))["config"]
    cfg_b = json.loads((second / "dataset_manifest.json").read_text(encoding="utf-8"))["config"]
    definitions = cfg_a.get("scenario_definitions") or []
    runtime = cfg_a.get("runtime") or {}
    check = Check("F11", "Reproducibility inputs and runtime are recorded")
    check.passed = (
        cfg_a != cfg_b
        and bool(definitions)
        and bool(definitions[0].get("sha256"))
        and bool(runtime.get("python_version"))
    )
    check.detail = (
        f"snapshots differ={cfg_a != cfg_b}, package={cfg_a.get('package')}/{cfg_b.get('package')}, "
        f"definition sha present={bool(definitions)}, runtime python={runtime.get('python_version')}"
    )
    return check


def check_f12() -> Check:
    """Retry-After jitter/cap and a shared cooldown, no hanging sleeps."""
    import httpx

    from contractforge.models import LLMRequest
    from contractforge.providers.cerebras import RETRY_AFTER_CAP_S, CerebrasProvider

    class FakeTime:
        def __init__(self) -> None:
            self.now = 0.0
            self.sleeps: list[float] = []

        def clock(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

    fake = FakeTime()
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"retry-after": "1000"}, text="slow down")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "{}"}, "index": 0}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    provider = CerebrasProvider(
        api_key="csk-unit-test", base_url="https://api.cerebras.ai/v1", model="m",
        timeout_s=30, max_retries=1, backoff_s=0.0, temperature=0.0, top_p=None,
        max_tokens=10, transport=httpx.MockTransport(handler),
        sleep=fake.sleep, clock=fake.clock,
    )
    provider.complete(
        LLMRequest(provider="cerebras", model="m", prompt_version="v1", system="s", user="u")
    )
    provider.close()
    capped = bool(fake.sleeps) and max(fake.sleeps) <= RETRY_AFTER_CAP_S + 1e-6
    check = Check("F12", "Retry-After jitter/cap with a shared cooldown (no real sleeps)")
    check.passed = capped and calls["count"] == 2
    check.detail = f"sleeps={[round(value, 2) for value in fake.sleeps]}, cap={RETRY_AFTER_CAP_S}, attempts={calls['count']}"
    return check


def run_checks() -> list[Check]:
    checks: list[Check] = []
    with tempfile.TemporaryDirectory(prefix="cf_review_") as temp:
        tmp = Path(temp)
        checks.append(check_f1(tmp))
        checks.append(check_f2(tmp))
        checks.append(check_f3())
        checks.append(check_f4())
        checks.append(check_f5())
        checks.append(check_f6(tmp))
        checks.append(check_f7(tmp))
        checks.append(check_f8(tmp))
        checks.append(check_f9())
        checks.append(check_f10())
        checks.append(check_f11(tmp))
        checks.append(check_f12())
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="maskinläsbart resultat")
    args = parser.parse_args(argv)
    commit = "okänd"
    with contextlib.suppress(OSError):
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False,
        ).stdout.strip() or "okänd"
    checks = run_checks()
    failed = [check for check in checks if not check.passed]
    payload = {
        "commit": commit,
        "checks": [check.to_dict() for check in checks],
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "all_passed": not failed,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Granskningsfynd F1–F12 mot commit {commit}")
        for check in checks:
            print(f"  {'OK  ' if check.passed else 'FEL '} {check.finding}: {check.title}")
            print(f"       {check.detail}")
        print(f"\n{payload['passed']}/{len(checks)} kontroller passerade")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
