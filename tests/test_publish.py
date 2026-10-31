"""Tests for the blind-rating question sample publisher and its CLI command."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile

import pytest

from contractforge.cli.main import main
from contractforge.config import Settings
from contractforge.errors import UsageError, ValidationError
from contractforge.export import export_zip, load_contract_dir, load_dataset_manifest
from contractforge.models import ContractType, DocumentFormat, ScenarioRequest
from contractforge.pipeline import generate_dataset
from contractforge.publish import (
    FORBIDDEN_QUESTION_FIELDS,
    RATINGS_HEADER,
    publish_question_sample,
)
from contractforge.utils import sha256_file

DATASET_MANIFEST = "dataset_manifest.json"


def _build_dataset(tmp_path, *, count: int = 2, seed: int = 77):
    """Generate a small mock dataset and export it as a ZIP."""

    settings = Settings(
        provider="mock", model="mock-1", formats=[DocumentFormat.TXT], max_repair_attempts=1
    )
    request = ScenarioRequest(
        contract_type=ContractType.CONSULTING,
        count=count,
        clauses=8,
        difficulty=1,
        seed=seed,
    )
    root = tmp_path / "dataset"
    outcome = generate_dataset(settings, request, root)
    archive = export_zip(outcome.dataset_root, tmp_path / "dataset.zip")
    return outcome.dataset_root, archive


@pytest.fixture()
def dataset(tmp_path):
    return _build_dataset(tmp_path)


def _read_manifest(out_dir):
    return json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))


def _source_questions(root) -> dict[tuple[str, str], dict]:
    """Every golden question in the source, keyed by (contract_id, question_id)."""

    questions: dict[tuple[str, str], dict] = {}
    for contract_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        loaded = load_contract_dir(contract_dir)
        for question in loaded.questions:
            questions[(loaded.contract_id, question.question_id)] = {
                "question": question,
                "rendered": loaded.rendered,
            }
    return questions


def _all_keys(value) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(key)
            keys |= _all_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            keys |= _all_keys(nested)
    return keys


def _run_json(capsys, argv: list[str]) -> tuple[int, dict]:
    code = main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else {}
    return code, payload


# ---------------------------------------------------------------------------
# publish_question_sample
# ---------------------------------------------------------------------------


def test_manifest_schema_and_content(dataset, tmp_path):
    root, archive = dataset
    out = publish_question_sample(archive, tmp_path / "sample", count=5, seed=3)

    manifest = _read_manifest(out)
    assert manifest["schema_version"] == "1.0"
    assert manifest["kind"] == "question_sample"
    assert manifest["sample_version"] == "v1"
    assert manifest["created_at"].endswith("+00:00")
    assert manifest["source_name"] == archive.name
    assert manifest["source_sha256"] == sha256_file(archive)
    assert manifest["corpus_manifest_sha256"] == sha256_file(root / DATASET_MANIFEST)
    assert manifest["sampling"]["seed"] == 3
    assert manifest["sampling"]["count"] == 5
    assert manifest["sampling"]["sorted_by"] == "(contract_id, question_id)"
    assert "random.Random" in manifest["sampling"]["algorithm"]

    questions = manifest["questions"]
    pool_size = manifest["sampling"]["pool_size"]
    assert pool_size > 0
    assert manifest["sampling"]["sample_size"] == len(questions) == min(5, pool_size)
    assert [(record["contract_id"], record["question_id"]) for record in questions] == sorted(
        (record["contract_id"], record["question_id"]) for record in questions
    )

    expected_keys = {
        "sample_id",
        "contract_id",
        "question_id",
        "category",
        "difficulty",
        "answerability",
        "question",
        "expected_answer",
        "evidence",
        "evidence_quote",
        "document_title",
        "document_text",
    }
    for record in questions:
        assert expected_keys <= set(record)
        digest = hashlib.sha256(
            f"{record['contract_id']}:{record['question_id']}".encode()
        ).hexdigest()[:12]
        assert record["sample_id"] == f"cfq-{digest}"
        assert record["evidence"], "every published question carries evidence"
        for span in record["evidence"]:
            assert set(span) == {"document_id", "clause_id", "start", "end"}


def test_questions_exist_in_source_and_quote_slices_text(dataset, tmp_path):
    root, archive = dataset
    out = publish_question_sample(archive, tmp_path / "sample", count=1000, seed=11)
    manifest = _read_manifest(out)
    source = _source_questions(root)

    assert manifest["sampling"]["sample_size"] == manifest["sampling"]["pool_size"]
    for record in manifest["questions"]:
        assert (record["contract_id"], record["question_id"]) in source
        origin = source[(record["contract_id"], record["question_id"])]
        question = origin["question"]
        assert record["question"] == question.question
        assert record["category"] == question.category.value
        assert record["difficulty"] == question.difficulty
        assert record["answerability"] == question.answerability.value
        assert record["expected_answer_structured"] == question.expected_answer.model_dump(
            mode="json"
        )

        document_id = record["evidence"][0]["document_id"]
        document = origin["rendered"][document_id]
        assert record["document_text"] == document.canonical_text
        assert record["document_title"] == document.title

        slices = [
            record["document_text"][span["start"] : span["end"]] for span in record["evidence"]
        ]
        assert record["evidence_quote"] == "\n---\n".join(slices)
        for sliced in slices:
            assert sliced
        assert record["expected_answer"]


def test_determinism_same_seed_same_ids(dataset, tmp_path):
    _, archive = dataset
    first = publish_question_sample(archive, tmp_path / "a", count=6, seed=42)
    second = publish_question_sample(archive, tmp_path / "b", count=6, seed=42)
    other = publish_question_sample(archive, tmp_path / "c", count=6, seed=43)

    def ids(out) -> list[str]:
        return [record["sample_id"] for record in _read_manifest(out)["questions"]]

    assert ids(first) == ids(second)
    first_manifest = _read_manifest(first)
    second_manifest = _read_manifest(second)
    first_manifest.pop("created_at")
    second_manifest.pop("created_at")
    assert first_manifest == second_manifest
    assert first_manifest["sampling"]["pool_size"] > 6
    assert ids(first) != ids(other)


def test_reruns_into_separate_dirs_are_identical(dataset, tmp_path):
    _, archive = dataset
    run_one = publish_question_sample(archive, tmp_path / "one", count=4, seed=7)
    run_two = publish_question_sample(archive, tmp_path / "two", count=4, seed=7)
    assert [r["sample_id"] for r in _read_manifest(run_one)["questions"]] == [
        r["sample_id"] for r in _read_manifest(run_two)["questions"]
    ]


def test_ratings_csv_header_rows_and_empty_columns(dataset, tmp_path):
    _, archive = dataset
    out = publish_question_sample(archive, tmp_path / "sample", count=4, seed=1)

    raw = (out / "ratings.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "BOM keeps Excel from mangling Swedish text"
    text = raw.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    assert rows[0] == RATINGS_HEADER
    # The rater's sheet is blind: no contract id, no category, no difficulty.
    assert RATINGS_HEADER == [
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
    rater_row = text.splitlines()[1]
    for hidden in ("contract_", "fact_extraction", "entity_identification"):
        assert hidden not in rater_row
    selection = json.loads((out / "selection.json").read_text(encoding="utf-8"))
    published = _read_manifest(out)
    assert selection["selection_sha256"] == published["selection_sha256"]
    assert [q["sample_id"] for q in selection["questions"]] == [
        r["sample_id"] for r in published["questions"]
    ]
    manifest = _read_manifest(out)
    assert len(rows) - 1 == manifest["sampling"]["sample_size"] == 4
    assert [row[0] for row in rows[1:]] == [r["sample_id"] for r in manifest["questions"]]
    for row in rows[1:]:
        assert len(row) == len(RATINGS_HEADER)
        assert row[-5:] == ["", "", "", "", ""], "rating columns ship empty"


def test_readme_is_swedish_and_states_binding(dataset, tmp_path):
    _, archive = dataset
    out = publish_question_sample(archive, tmp_path / "sample", count=4, seed=1)
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert "scenario.json" in readme and "gold_facts.json" in readme
    assert "inte representativt" in readme
    assert "Två oberoende rater".lower() in readme.lower()
    assert sha256_file(archive) in readme
    assert "seed" in readme and "1" in readme


def test_no_scenario_or_gold_leaks(dataset, tmp_path):
    root, archive = dataset
    out = publish_question_sample(archive, tmp_path / "sample", count=1000, seed=5)
    manifest = _read_manifest(out)
    # "seed" is a legitimate sampling parameter at the top level; inside a
    # question it would be a scenario leak, which _question_record guards.
    assert not (_all_keys(manifest) & (FORBIDDEN_QUESTION_FIELDS - {"seed"}))
    for record in manifest["questions"]:
        assert not (_all_keys(record) & FORBIDDEN_QUESTION_FIELDS)
    for forbidden in ("scenario", "gold", "gold_facts", "facts", "party"):
        assert forbidden not in _all_keys(manifest)

    dataset_manifest = load_dataset_manifest(root)
    assert dataset_manifest is not None
    # The sampling seed of the generating pipeline must not leak into the packet.
    assert manifest["sampling"]["seed"] == 5 != dataset_manifest.config.seed


def test_directory_source_and_corpus_hash_check(dataset, tmp_path):
    root, archive = dataset
    out = publish_question_sample(root, tmp_path / "from_dir", count=3, seed=2)
    manifest = _read_manifest(out)
    assert manifest["source_name"] == root.name
    assert len(manifest["source_sha256"]) == 64
    assert manifest["corpus_manifest_sha256"] == sha256_file(root / DATASET_MANIFEST)

    # Same content, same hash: ZIP and directory produce the same selection.
    zip_out = publish_question_sample(archive, tmp_path / "from_zip", count=3, seed=2)
    assert [r["sample_id"] for r in _read_manifest(zip_out)["questions"]] == [
        r["sample_id"] for r in manifest["questions"]
    ]

    matched = publish_question_sample(
        root, tmp_path / "matched", count=3, seed=2, corpus_sha256=manifest["source_sha256"]
    )
    assert matched.is_dir()
    with pytest.raises(ValidationError):
        publish_question_sample(
            root, tmp_path / "mismatched", count=3, seed=2, corpus_sha256="0" * 64
        )


def test_existing_non_empty_output_requires_force(dataset, tmp_path):
    _, archive = dataset
    out = publish_question_sample(archive, tmp_path / "sample", count=2, seed=0)
    sentinel = out / "keep.txt"
    sentinel.write_text("rater notes", encoding="utf-8")

    with pytest.raises(UsageError) as excinfo:
        publish_question_sample(archive, out, count=2, seed=0)
    assert excinfo.value.exit_code == 2
    assert sentinel.read_text(encoding="utf-8") == "rater notes"

    publish_question_sample(archive, out, count=2, seed=0, force=True)
    assert sentinel.exists(), "force overwrites the packet but never deletes foreign files"


def test_missing_source_and_empty_dataset(tmp_path):
    with pytest.raises(UsageError):
        publish_question_sample(tmp_path / "nope.zip", tmp_path / "out")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValidationError):
        publish_question_sample(empty, tmp_path / "out")


def test_dataset_without_questions_raises_validation_error(dataset, tmp_path):
    _, archive = dataset
    stripped = tmp_path / "no_questions"
    stripped.mkdir()
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(
        stripped / "no_questions.zip", "w"
    ) as target:
        for info in source.infolist():
            if info.filename.endswith("golden_questions.json"):
                continue
            target.writestr(info, source.read(info))
    with pytest.raises(ValidationError) as excinfo:
        publish_question_sample(stripped / "no_questions.zip", tmp_path / "out")
    assert excinfo.value.exit_code == 3


def test_pool_excludes_questions_that_are_not_ratable(dataset, tmp_path):
    _, archive = dataset
    out = publish_question_sample(archive, tmp_path / "sample", count=1000, seed=0)
    manifest = _read_manifest(out)
    assert manifest["excluded_questions"] == []
    assert manifest["sampling"]["pool_size"] == manifest["sampling"]["sample_size"]


# ---------------------------------------------------------------------------
# CLI: contractforge publish-questions
# ---------------------------------------------------------------------------


def test_cli_publish_questions_json(dataset, tmp_path, capsys):
    _, archive = dataset
    output = tmp_path / "cli_sample"
    code, payload = _run_json(
        capsys,
        [
            "publish-questions",
            str(archive),
            "--output",
            str(output),
            "--count",
            "3",
            "--seed",
            "9",
            "--json",
        ],
    )
    assert code == 0, payload
    assert payload["status"] == "ok"
    assert payload["command"] == "publish-questions"
    assert payload["output"] == str(output)
    assert payload["sample_size"] == 3
    assert payload["pool_size"] >= 3
    assert payload["source_sha256"] == sha256_file(archive)
    assert payload["seed"] == 9
    assert {"manifest.json", "ratings.csv", "README.md"} <= set(payload["files"])
    assert (output / "ratings.csv").is_file()


def test_cli_corpus_sha256_accepted(dataset, tmp_path, capsys):
    _, archive = dataset
    code, payload = _run_json(
        capsys,
        [
            "publish-questions",
            str(archive),
            "--output",
            str(tmp_path / "s"),
            "--count",
            "1",
            "--corpus-sha256",
            sha256_file(archive).upper(),
            "--json",
        ],
    )
    assert code == 0, payload
    assert payload["sample_size"] == 1


def test_cli_non_empty_output_exit_2(dataset, tmp_path, capsys):
    _, archive = dataset
    output = tmp_path / "occupied"
    output.mkdir()
    (output / "irrelevant.txt").write_text("x", encoding="utf-8")
    code, payload = _run_json(
        capsys,
        ["publish-questions", str(archive), "--output", str(output), "--json"],
    )
    assert code == 2
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "usage_error"
    assert not (output / "manifest.json").exists()

    code, _ = _run_json(
        capsys,
        ["publish-questions", str(archive), "--output", str(output), "--force", "--json"],
    )
    assert code == 0


def test_cli_missing_source_exit_2(tmp_path, capsys):
    code, payload = _run_json(
        capsys,
        [
            "publish-questions",
            str(tmp_path / "missing.zip"),
            "--output",
            str(tmp_path / "out"),
            "--json",
        ],
    )
    assert code == 2
    assert payload["error"]["type"] == "usage_error"


def test_cli_unknown_option_is_usage_error(dataset, tmp_path, capsys):
    _, archive = dataset
    code, payload = _run_json(
        capsys,
        ["publish-questions", str(archive), "--output", str(tmp_path / "o"), "--nope", "--json"],
    )
    assert code == 2


def test_cli_two_runs_same_ids(dataset, tmp_path, capsys):
    _, archive = dataset
    first = tmp_path / "first"
    second = tmp_path / "second"
    for output in (first, second):
        code, payload = _run_json(
            capsys,
            [
                "publish-questions",
                str(archive),
                "--output",
                str(output),
                "--count",
                "5",
                "--seed",
                "2026",
                "--json",
            ],
        )
        assert code == 0, payload
    ids = lambda out: [r["sample_id"] for r in _read_manifest(out)["questions"]]  # noqa: E731
    assert ids(first) == ids(second)
    assert (first / "ratings.csv").read_bytes() == (second / "ratings.csv").read_bytes()


def test_publish_question_sample_runs_without_side_channel(dataset, tmp_path):
    """Selection depends only on source/count/seed — not on dataset generation state."""

    _, archive = dataset
    out = publish_question_sample(archive, tmp_path / "x", count=2, seed=0)
    assert out.is_dir()


def test_stratified_sample_covers_rare_categories(dataset, tmp_path):
    """A uniform draw can miss rare categories; stratification must not."""

    _, archive = dataset
    stratified = publish_question_sample(archive, tmp_path / "s", count=6, seed=2)
    uniform = publish_question_sample(
        archive, tmp_path / "u", count=6, seed=2, stratified=False
    )

    def categories(out):
        return {q["category"] for q in _read_manifest(out)["questions"]}

    assert _read_manifest(stratified)["sampling"]["stratified"] is True
    assert _read_manifest(uniform)["sampling"]["stratified"] is False
    assert len(categories(stratified)) >= len(categories(uniform))


def test_selection_hash_binds_the_sample(dataset, tmp_path):
    _, archive = dataset
    out = publish_question_sample(archive, tmp_path / "s", count=5, seed=9)
    manifest = _read_manifest(out)
    from contractforge.publish import selection_sha256

    assert manifest["selection_sha256"] == selection_sha256(manifest["questions"])
    other = publish_question_sample(archive, tmp_path / "o", count=5, seed=10)
    assert _read_manifest(other)["selection_sha256"] != manifest["selection_sha256"]


def test_every_published_span_is_an_exact_slice_of_the_document_text(dataset, tmp_path):
    """A quote must never be a paraphrase of the document it claims to cite."""

    _, archive = dataset
    out = publish_question_sample(archive, tmp_path / "s", count=8, seed=4)

    for question in _read_manifest(out)["questions"]:
        assert question["evidence_quotes"]
        assert len(question["evidence_quotes"]) == len(question["evidence"])
        for span, quote in zip(question["evidence"], question["evidence_quotes"], strict=True):
            assert question["document_text"][span["start"] : span["end"]] == quote
            assert quote in question["document_text"]


def test_sample_ids_are_opaque_hashes_of_the_source_question():
    from contractforge.publish import sample_id

    first = sample_id("contract_0001", "q_0001")
    second = sample_id("contract_0002", "q_0001")
    assert first.startswith("cfq-") and len(first) == len("cfq-") + 12
    assert first != second
    assert sample_id("contract_0001", "q_0001") == first  # reproducible
    assert "contract_0001" not in first


def test_stratified_sample_is_deterministic_and_respects_strata():
    from contractforge.publish import stratified_sample

    class Q:
        def __init__(self, qid, category, difficulty):
            self.question_id = qid
            self.category = type("C", (), {"value": category})()
            self.difficulty = difficulty
            self.answerability = type("A", (), {"value": "ANSWERABLE"})()

    class Item:
        def __init__(self, contract, qid, category, difficulty):
            self.contract_id = contract
            self.question = Q(qid, category, difficulty)

        @property
        def sort_key(self):
            return (self.contract_id, self.question.question_id)

    pool = [
        Item(f"contract_{index:04d}", "q_0001", "rare" if index < 50 else "common", 1)
        for index in range(100)
    ]
    first = stratified_sample(pool, 10, seed=3)
    second = stratified_sample(pool, 10, seed=3)
    assert [item.sort_key for item in first] == [item.sort_key for item in second]
    # The rare half (50 % of the pool) must get its proportional share: 5 items.
    assert sum(1 for item in first if item.question.category.value == "rare") == 5
