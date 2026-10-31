"""Publish a deterministic sample of golden questions for blind rating.

An independent quality experiment needs raters who judge whether facit is
legally and semantically correct *without* seeing the scenario or the gold
file. This module writes such a packet:

* ``manifest.json`` – the sample itself, with the canonical document text and
  the exact evidence slice for every question, and no scenario/gold values.
* ``ratings.csv``  – an empty, fillable rating form (one row per question).
* ``README.md``    – Swedish instructions for the raters.

The sample is a function of the *source* (ZIP archive or unpacked dataset
directory), ``count`` and ``seed`` only: the pool is sorted by
``(contract_id, question_id)`` and drawn with :class:`random.Random` seeded by
``seed``, so the same inputs always select the same questions. The packet is
bound to the source through ``source_sha256`` (and ``corpus_manifest_sha256``
when the source carries a dataset manifest).

Questions whose evidence cannot be sliced from the published document text
(missing document, no canonical text, span beyond the text) are excluded from
the pool: the packet never shows a quote that is not an exact slice of the
text it ships with.
"""

from __future__ import annotations

import csv
import hashlib
import io
import random
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ExportError, UsageError, ValidationError
from .export.loader import (
    DATASET_MANIFEST_FILENAME,
    find_contract_dirs,
    load_contract_dir,
)
from .models import EvidenceSpan, ExpectedAnswer, GoldenQuestion, utc_now
from .utils import (
    atomic_write_json,
    atomic_write_text,
    sha256_bytes,
    sha256_file,
    sha256_text,
)

PUBLISH_SCHEMA_VERSION = "1.0"
SAMPLE_VERSION = "v1"
SAMPLE_KIND = "question_sample"

SAMPLE_ALGORITHM = "random.Random(seed).sample(pool, min(count, pool_size))"
SORTED_BY = "(contract_id, question_id)"
#: Stable, random-looking rater id: contract and question identity are replaced
#: before a rater sees the packet (blind rating, see docs/quality-protocol.md).
SAMPLE_ID_PREFIX = "cfq-"
BLINDING = "sha256(contract_id:question_id)[:12]"

#: Questions are stratified over (category, difficulty, answerability) so that
#: rare categories are represented instead of vanishing in a uniform draw.
STRATUM_KEY = "(category, difficulty, answerability)"
STRATUM_ALGORITHM = (
    "deterministic largest-remainder allocation over strata of "
    "(category, difficulty, answerability); within each stratum the same "
    "random.Random(seed) draws from a sorted stratum of "
    "sha256(contract_id:question_id) values"
)

MANIFEST_FILENAME = "manifest.json"
RATINGS_FILENAME = "ratings.csv"
README_FILENAME = "README.md"
SELECTION_FILENAME = "selection.json"
RATERS_FILENAME = "raters.json"
#: Neutral rater ids: they identify a sheet, never a person or a model.
RATER_IDS = ("A", "B")

# Semicolon-separated, UTF-8 with BOM, because Excel opens the file in the
# rater's locale and would otherwise merge all columns into one.
RATINGS_ENCODING = "utf-8-sig"
RATINGS_DELIMITER = ";"
RATINGS_ALLOWED_VALUES = (
    "ja",
    "nej",
    "osäker",
    "ej_tillamplig",
)
#: The rater's sheet is deliberately blind: no contract id, no category and no
#: difficulty. Those live in manifest.json (coordinator copy) only.
RATINGS_HEADER = [
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

# Scenario/gold vocabulary that must never appear as a key in a published
# question. Checked recursively so a nested leak cannot slip through.
FORBIDDEN_QUESTION_FIELDS = frozenset(
    {
        "fact_id",
        "fact_ids",
        "required_facts",
        "governing_fact_id",
        "validated",
        "validation_notes",
        "parties",
        "relations",
        "anomalies",
        "anomaly_id",
        "missing_documents",
        "explicit_surfaces",
        "paraphrase_surfaces",
        "superseded_by",
        "seed",
    }
)

EVIDENCE_JOINER = "\n---\n"


@dataclass(frozen=True)
class _PoolQuestion:
    """One ratable question plus the material needed to publish it."""

    contract_id: str
    question: GoldenQuestion
    canonical: dict[str, str]
    document_titles: dict[str, str]

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.contract_id, self.question.question_id)

    @property
    def document_id(self) -> str:
        return self.question.evidence[0].document_id

    @property
    def document_title(self) -> str:
        return self.document_titles.get(self.document_id, "")

    @property
    def document_text(self) -> str:
        return self.canonical[self.document_id]

    def document_package(self) -> dict[str, str]:
        """Every document of the contract, so a rater can check other clauses.

        A question is judged against the whole package: an amendment or an
        appendix can contain the exception that decides the answer
        (docs/quality-protocol.md 3.0).
        """

        return dict(sorted(self.canonical.items()))

    def evidence_quotes(self) -> list[str]:
        """The exact canonical slice for every evidence span, in order."""

        return [
            self.canonical[span.document_id][span.start : span.end]
            for span in self.question.evidence
        ]

    def evidence_quote(self) -> str:
        """Display form for the rater sheet; each part is an exact slice."""

        return EVIDENCE_JOINER.join(self.evidence_quotes())


def sample_id(contract_id: str, question_id: str) -> str:
    """Stable, opaque id of one published question.

    Hashing keeps the rater blind to which contract a question came from while
    staying reproducible: same source and question, same id.
    """

    digest = sha256_text(f"{contract_id}:{question_id}")[:12]
    return f"{SAMPLE_ID_PREFIX}{digest}"


def _stratum(item: _PoolQuestion) -> tuple[str, int, str]:
    question = item.question
    return (question.category.value, int(question.difficulty), question.answerability.value)


def stratified_sample(
    pool: list[_PoolQuestion], count: int, seed: int
) -> list[_PoolQuestion]:
    """Deterministic stratified draw over (category, difficulty, answerability).

    Every stratum gets ``floor(count * size / pool)`` slots plus the remainder
    apportioned by largest fractional part (ties broken by stratum key, so the
    result never depends on dict ordering). A stratum smaller than its quota
    contributes all of its questions and the remainder is re-apportioned. The
    draw inside a stratum uses a seeded RNG over a sorted stratum, so the same
    ``(pool, count, seed)`` always yields the same questions.
    """

    if count >= len(pool):
        return list(pool)
    strata: dict[tuple[str, int, str], list[_PoolQuestion]] = {}
    for item in pool:
        strata.setdefault(_stratum(item), []).append(item)
    keys = sorted(strata)
    sizes = {key: len(strata[key]) for key in keys}

    def apportion(budget: int, available: dict[tuple[str, int, str], int]) -> dict[tuple[str, int, str], int]:
        total = sum(available.values()) or 1
        quotas = {key: budget * available[key] / total for key in keys if available[key]}
        allocation = {key: min(int(quotas[key]), available[key]) for key in quotas}
        remaining = budget - sum(allocation.values())
        while remaining > 0:
            candidates = sorted(
                (key for key in keys if available[key] > allocation[key]),
                key=lambda key: (-(quotas[key] - int(quotas[key])), key),
            )
            for key in candidates:
                if remaining == 0:
                    break
                allocation[key] += 1
                remaining -= 1
            if not candidates:  # pragma: no cover - only if pool < count
                break
        return allocation

    allocation = apportion(count, sizes)
    chosen: list[_PoolQuestion] = []
    for key in keys:
        take = allocation.get(key, 0)
        if not take:
            continue
        members = sorted(strata[key], key=lambda item: sha256_text(item.sort_key[1]))
        rng = random.Random(f"{seed}:{key}")
        chosen.extend(rng.sample(members, take))
    chosen.sort(key=lambda item: item.sort_key)
    return chosen


def format_expected_answer(answer: ExpectedAnswer) -> str:
    """Render the expected answer as the flat text a rater judges."""

    if answer.conditions:
        return " | ".join(answer.conditions)
    if answer.text:
        return answer.text
    parts = [str(value) for value in answer.values]
    if answer.value is not None:
        parts.append(str(answer.value))
    rendered = " | ".join(parts)
    for suffix in (answer.unit, answer.currency):
        if suffix:
            rendered = f"{rendered} ({suffix})" if rendered else suffix
    return rendered


def _evidence_problem(evidence: list[EvidenceSpan], canonical: dict[str, str]) -> str | None:
    if not evidence:
        return "no evidence span"
    for span in evidence:
        text = canonical.get(span.document_id)
        if text is None:
            return f"evidence document {span.document_id!r} has no canonical text"
        if span.end > len(text):
            return (
                f"evidence span {span.document_id}:{span.start}:{span.end} exceeds "
                f"text length {len(text)}"
            )
    return None


def _is_ratable(question: GoldenQuestion) -> bool:
    answer = question.expected_answer
    return bool(
        answer.value is not None or answer.values or answer.text or answer.conditions
    )


def _canonical_texts(
    rendered: dict[str, Any], texts: dict[str, str]
) -> dict[str, str]:
    """Canonical text per document: ``rendered.json`` first, the ``.txt`` file as fallback."""

    canonical = {document_id: document.canonical_text for document_id, document in rendered.items()}
    for document_id, text in texts.items():
        canonical.setdefault(document_id, text)
    return canonical


def _sha256_tree(root: Path) -> str:
    """SHA-256 over every file of a dataset directory, in sorted relative-path order.

    Content-derived on purpose: stable across copies and platforms, unlike a
    single archive hash of a directory that has no archive.
    """

    digest = hashlib.sha256()
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _corpus_manifest_sha256(root: Path) -> str | None:
    path = root / DATASET_MANIFEST_FILENAME
    return sha256_file(path) if path.is_file() else None


@contextmanager
def _source_dataset(source: Path) -> Iterator[tuple[Path, str, str | None]]:
    """Yield ``(dataset_root, source_sha256, corpus_manifest_sha256)`` for a source."""

    if source.is_dir():
        yield source, _sha256_tree(source), _corpus_manifest_sha256(source)
        return
    if not source.is_file():
        raise UsageError(f"källan finns inte: {source}", details={"source": str(source)})
    if not zipfile.is_zipfile(source):
        raise UsageError(
            f"källan är varken en katalog eller ett ZIP-arkiv: {source}",
            details={"source": str(source)},
        )
    with tempfile.TemporaryDirectory(prefix="contractforge-questions-") as temporary:
        try:
            with zipfile.ZipFile(source) as archive:
                archive.extractall(temporary)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ExportError(f"kunde inte packa upp {source}: {exc}") from exc
        root = Path(temporary)
        yield root, sha256_file(source), _corpus_manifest_sha256(root)


def build_question_pool(root: Path) -> tuple[list[_PoolQuestion], list[str]]:
    """Load every contract under ``root`` and return the ratable questions, sorted.

    The second element lists excluded questions with the reason, so a packet
    never silently loses questions.
    """

    contract_dirs = find_contract_dirs(root)
    if not contract_dirs:
        raise ValidationError(
            f"källan innehåller inga avtalskataloger: {root}", details={"source": str(root)}
        )

    pool: list[_PoolQuestion] = []
    excluded: list[str] = []
    for contract_dir in contract_dirs:
        loaded = load_contract_dir(contract_dir)
        canonical = _canonical_texts(loaded.rendered, loaded.texts)
        titles = {document_id: document.title for document_id, document in loaded.rendered.items()}
        for question in loaded.questions:
            reason = None if _is_ratable(question) else "no expected answer"
            reason = reason or _evidence_problem(question.evidence, canonical)
            if reason is not None:
                excluded.append(
                    f"{loaded.contract_id}/{question.question_id}: {reason}"
                )
                continue
            pool.append(
                _PoolQuestion(
                    contract_id=loaded.contract_id,
                    question=question,
                    canonical=canonical,
                    document_titles=titles,
                )
            )
    pool.sort(key=lambda item: item.sort_key)
    return pool, excluded


def _forbidden_fields(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in FORBIDDEN_QUESTION_FIELDS:
                found.add(key)
            found |= _forbidden_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            found |= _forbidden_fields(nested)
    return found


def _question_record(row: int, item: _PoolQuestion) -> dict[str, Any]:
    question = item.question
    record: dict[str, Any] = {
        "sample_id": sample_id(item.contract_id, question.question_id),
        "row": row,
        "contract_id": item.contract_id,
        "question_id": question.question_id,
        "category": question.category.value,
        "difficulty": question.difficulty,
        "answerability": question.answerability.value,
        "language": question.language.value,
        "question": question.question,
        "expected_answer": format_expected_answer(question.expected_answer),
        "expected_answer_structured": question.expected_answer.model_dump(mode="json"),
        "evidence": [
            {
                "document_id": span.document_id,
                "clause_id": span.clause_id,
                "start": span.start,
                "end": span.end,
            }
            for span in question.evidence
        ],
        "evidence_quote": item.evidence_quote(),
        "evidence_quotes": item.evidence_quotes(),
        "document_title": item.document_title,
        "document_text": item.document_text,
        "document_texts": item.document_package(),
    }
    leaked = _forbidden_fields(record)
    if leaked:
        # Defensive: _question_record is the only writer of published fields,
        # but a future edit must not be able to ship scenario data silently.
        raise ExportError(
            f"publicerad fråga skulle innehålla scenariofält {sorted(leaked)}",
            details={"sample_id": record["sample_id"]},
        )
    return record


def _ratings_csv(questions: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=RATINGS_DELIMITER, lineterminator="\r\n")
    writer.writerow(RATINGS_HEADER)
    for record in questions:
        writer.writerow(
            [
                record["sample_id"],
                record["question"],
                record["expected_answer"],
                record["evidence_quote"],
                "",
                "",
                "",
                "",
                "",
            ]
        )
    return buffer.getvalue()


def _ensure_output_dir(out_dir: Path, *, force: bool) -> None:
    if not out_dir.exists():
        return
    if not out_dir.is_dir():
        raise UsageError(
            f"utdata är en fil, inte en katalog: {out_dir}", details={"output": str(out_dir)}
        )
    if any(out_dir.iterdir()) and not force:
        raise UsageError(
            f"utdatakatalogen är inte tom: {out_dir} (använd --force för att skriva över)",
            details={"output": str(out_dir)},
        )


def publish_question_sample(
    source: Path,
    out_dir: Path,
    *,
    count: int = 100,
    seed: int = 0,
    corpus_sha256: str | None = None,
    force: bool = False,
    stratified: bool = True,
    blind_sheets: bool = True,
) -> Path:
    """Write a blind rater packet for a deterministic question sample.

    Files: ``manifest.json`` (coordinator copy: questions, expected answers,
    evidence, document text, stratum metadata), ``ratings.csv`` (the rater's
    blind, fillable sheet), ``selection.json`` (only what is needed to prove
    which questions were drawn) and ``README.md`` (instructions).

    ``source`` is a dataset ZIP or an unpacked dataset directory. Selection is
    deterministic for ``(source, count, seed)``; when ``count`` exceeds the
    pool the whole pool is published. ``corpus_sha256``, when given, must match
    the source hash (``sha256_file`` for a ZIP, a content hash for a
    directory). Returns the output directory.
    """

    source = Path(source)
    if count < 1:
        raise UsageError("count måste vara >= 1", details={"count": count})
    output = Path(out_dir)
    _ensure_output_dir(output, force=force)

    with _source_dataset(source) as (root, source_sha256, corpus_manifest_sha256):
        expected_hash = corpus_sha256.strip().lower() if corpus_sha256 is not None else None
        if expected_hash is not None and expected_hash != source_sha256:
            raise ValidationError(
                "källans SHA-256 stämmer inte med --corpus-sha256: "
                f"förväntade {expected_hash}, fick {source_sha256}",
                details={"source_sha256": source_sha256},
            )
        pool, excluded = build_question_pool(root)
        if not pool:
            raise ValidationError(
                f"källan innehåller inga publicerbara golden-frågor: {source}",
                details={"source": str(source), "excluded": excluded[:20]},
            )
        sample_size = min(count, len(pool))
        if stratified:
            chosen = stratified_sample(pool, sample_size, seed)
        else:
            chosen = random.Random(seed).sample(pool, sample_size)
        chosen.sort(key=lambda item: item.sort_key)
        questions = [_question_record(row, item) for row, item in enumerate(chosen, start=1)]

    manifest: dict[str, Any] = {
        "schema_version": PUBLISH_SCHEMA_VERSION,
        "kind": SAMPLE_KIND,
        "created_at": utc_now().isoformat(),
        "source_name": source.name,
        "source_sha256": source_sha256,
        "corpus_manifest_sha256": corpus_manifest_sha256,
        "sample_version": SAMPLE_VERSION,
        "sampling": {
            "seed": seed,
            "count": count,
            "sample_size": sample_size,
            "pool_size": len(pool),
            "algorithm": STRATUM_ALGORITHM if stratified else SAMPLE_ALGORITHM,
            "stratified": stratified,
            "stratum_key": STRATUM_KEY if stratified else None,
            "sorted_by": SORTED_BY,
            "blinding": BLINDING,
        },
        "selection_sha256": selection_sha256(questions),
        "ratings_columns": RATINGS_HEADER,
        "excluded_questions": excluded,
        "questions": questions,
    }

    selection = {
        "schema_version": PUBLISH_SCHEMA_VERSION,
        "kind": "question_sample_selection",
        "source_sha256": source_sha256,
        "corpus_manifest_sha256": corpus_manifest_sha256,
        "sample_version": SAMPLE_VERSION,
        "sampling": manifest["sampling"],
        "selection_sha256": manifest["selection_sha256"],
        "questions": [
            {
                "sample_id": question["sample_id"],
                "contract_id": question["contract_id"],
                "question_id": question["question_id"],
                "category": question["category"],
                "difficulty": question["difficulty"],
                "answerability": question["answerability"],
            }
            for question in questions
        ],
    }

    output.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output / MANIFEST_FILENAME, manifest)
    atomic_write_json(output / SELECTION_FILENAME, selection)
    atomic_write_text(
        output / RATINGS_FILENAME, _ratings_csv(questions), encoding=RATINGS_ENCODING
    )
    atomic_write_text(output / README_FILENAME, _readme(manifest))
    if blind_sheets:
        write_blind_sheets(questions, output, seed=seed)
    return output


def write_blind_sheets(
    questions: list[dict[str, Any]], out_dir: Path, *, seed: int
) -> dict[str, Any]:
    """Write one randomly ordered blind sheet per rater (protocol 7.4).

    Each rater gets an independent order derived from the selection seed and a
    neutral rater id, so the two raters cannot infer which questions belong to
    the same contract. The sheet hashes are written to ``raters.json`` so a
    report can prove which version each rater coded.
    """

    import random as _random

    sheets: dict[str, Any] = {
        "schema_version": PUBLISH_SCHEMA_VERSION,
        "kind": "rater_sheets",
        "created_at": utc_now().isoformat(),
        "order_seed": seed,
        "shuffle": "random.Random('<selection-seed>:<rater-id>').shuffle",
        "current": RATINGS_FILENAME,
        "raters": {},
    }
    orders: list[list[str]] = []
    for rater_id in RATER_IDS:
        ordered = list(questions)
        _random.Random(f"{seed}:{rater_id}").shuffle(ordered)
        orders.append([record["sample_id"] for record in ordered])
        filename = rater_sheet_filename(rater_id)
        text = _ratings_csv(ordered)
        atomic_write_text(out_dir / filename, text, encoding=RATINGS_ENCODING)
        sheets["raters"][rater_id] = {
            "file": filename,
            "sha256": sha256_bytes(text.encode(RATINGS_ENCODING)),
            "sample_ids": orders[-1],
        }
    sheets["orders_identical"] = len(orders) == 2 and orders[0] == orders[1]
    atomic_write_json(out_dir / RATERS_FILENAME, sheets)
    return sheets


def rater_sheet_filename(rater_id: str) -> str:
    return f"ratings_{rater_id}.csv"


def selection_sha256(questions: list[dict[str, Any]]) -> str:
    """Hash of the chosen questions: proves which sample a report was made on."""

    payload = "\n".join(
        sorted(f"{question['contract_id']}:{question['question_id']}" for question in questions)
    )
    return sha256_text(payload)


def _readme(manifest: dict[str, Any]) -> str:
    sampling = manifest["sampling"]
    return _README_TEMPLATE.format(
        sample_size=sampling["sample_size"],
        pool_size=sampling["pool_size"],
        count=sampling["count"],
        seed=sampling["seed"],
        source_name=manifest["source_name"],
        source_sha256=manifest["source_sha256"],
        corpus_manifest_sha256=manifest["corpus_manifest_sha256"] or "saknas i källan",
        created_at=manifest["created_at"],
        sample_version=SAMPLE_VERSION,
    )


_README_TEMPLATE = """# Bedömning av ContractForge-facit

Det här paketet innehåller {sample_size} slumpade golden-frågor med förväntat
svar och evidenscitat från syntetiska svenska avtal. Uppgiften är att bedöma
om facit är korrekt – utan att se scenariot eller facitfilen som frågorna
genererades från.

## Filer

| Fil | Innehåll |
|-----|----------|
| `README.md` | Denna instruktion |
| `manifest.json` | Urvalet maskinläsbart: fråga, förväntat svar, evidensspann, dokumenttitel och dokumentets fulla kanoniska text |
| `ratings.csv` | Ifyllningsbar bedömningsblankett, en rad per fråga (UTF-8 med BOM, semikolon) |
| `selection.json` | Urvalet: vilka frågor som drogs (opaka id:n + härkomst) och `selection_sha256` |
| `manifest.json` | Koordinatorns kopia: förväntat svar, evidensspann och dokumentets fulla text |

**Läs hela dokumentet – och hela paketet.** Evidenscitatet är en ingång, inte
hela underlaget. Läs `document_texts` (alla dokument i avtalet: huvudavtal,
bilagor och tillägg) innan du kodar Q1–Q4. Ett undantag, en motsägelse eller ett
tillägg i en annan klausul kan ändra vad som gäller; om svaret beror på en sådan
bestämmelse ska du koda Q2 = `nej` när förväntat svar anger det överspelade
värdet och skriva i `comment` vilket dokument/klausul som avgjorde. Se
`docs/quality-protocol.md` avsnitt 3.0.

`evidence_quote` innehåller ett eller flera citat. När frågan har flera
evidensspann visas de i radavdelat format där varje del är en exakt avskrift ur
dokumentet (delarna är åtskilda av en rad med `---`); varje del kan slås upp
ordagrant i `manifest.json` → `evidence_quotes`.

## Så här bedömer du

Bedöm **endast** utifrån `document_text` i `manifest.json` (samma text som
`evidence_quote` är hämtat ur). Använd inte andra dokument eller egna
antaganden om vad parterna "brukar" avtala. Om något inte går att avgöra ur
texten svarar du `osäker` – gissa inte.

Bedömarkolumnerna i `ratings.csv` är **tomma** i paketet. Fyll i exakt ett av de
tillåtna värdena per fråga och rad:

| Kolumn | Fråga | Tillåtna värden |
|--------|-------|-----------------|
| `Q1_answerable` | Går frågan att besvara med dokumentet som finns? | `ja`, `nej`, `osäker` |
| `Q2_answer_correct` | Är det förväntade svaret korrekt enligt dokumenttexten? | `ja`, `nej`, `osäker` |
| `Q3_evidence_sufficient` | Räcker evidenscitatet för att styrka svaret? | `ja`, `nej`, `osäker` |
| `Q4_anomaly_handling` | Hanterar frågan och svaret en motsägelse, ett undantag eller en informationslucka i texten korrekt, när sådan finns? | `ja`, `nej`, `ej_tillamplig` |
| `comment` | Fri kommentar, t.ex. vad som är fel eller varför du är osäker | fri text |

`Q4_anomaly_handling` är `ej_tillamplig` när frågan inte rör någon motsägelse,
undantag, överskrivning eller informationslucka. Korta motiveringar i `comment`
uppskattas särskilt när du svarar `nej` eller `osäker`.

## Regler

- **Du får inte läsa** `scenario.json`, `gold_facts.json`, `verification.json`
  eller `llm_calls.json` ur källdatasetet. Paketet innehåller medvetet inga
  scenario- eller facitvärden utöver själva frågan, det förväntade svaret och
  evidenscitatet.
- **Urvalet är inte representativt** för hela korpusen. Frågorna är ett
  deterministiskt slumpurval (seed {seed}) ur {pool_size} frågor och säger
  ingenting om fördelningen av frågetyper, svårighetsgrader eller avtalstyper i
  datasetet.
- **Två oberoende rater rekommenderas.** Ingen av raterna ska se den andras
  svar innan båda är klara – använd varsin kopia av `ratings.csv`. Oenighet
  mellan raterna är i sig ett resultat och ska rapporteras, inte lösas genom
  samråd.
- Ändra inte `sample_id`, `contract_id` eller radordningen i `ratings.csv` – det
  är kopplingen till `manifest.json`.

## Resultatet binds till källan och urvalet

Citera alltid följande i resultatet, så att exakt samma urval kan återskapas:

| Fält | Värde |
|------|-------|
| `source_name` | `{source_name}` |
| `source_sha256` | `{source_sha256}` |
| `corpus_manifest_sha256` | `{corpus_manifest_sha256}` |
| urvals-seed | `{seed}` |
| antal begärda (`count`) | `{count}` |
| `sample_version` | `{sample_version}` |

Ett resultat utan dessa värden kan inte hänföras till en bestämd källa. Är
`count` större än antalet frågor i poolen ({pool_size}) publiceras hela poolen;
`sample_size` i `manifest.json` blir då {sample_size}.

Fält i `manifest.json`: `evidence[].start`/`end` är teckenoffsets i
`document_text` (start inklusiv, end exklusiv) och `evidence_quote` är exakt den
delsträngen. Har en fråga flera spann är de sammanfogade med en rad som bara
innehåller `---`. Urvalsmanifestet skapades {created_at}.
"""


__all__ = [
    "FORBIDDEN_QUESTION_FIELDS",
    "MANIFEST_FILENAME",
    "PUBLISH_SCHEMA_VERSION",
    "RATINGS_ALLOWED_VALUES",
    "RATINGS_ENCODING",
    "RATINGS_FILENAME",
    "RATINGS_HEADER",
    "README_FILENAME",
    "SAMPLE_ALGORITHM",
    "SAMPLE_KIND",
    "SAMPLE_VERSION",
    "build_question_pool",
    "rater_sheet_filename",
    "format_expected_answer",
    "publish_question_sample",
    "sample_id",
    "write_blind_sheets",
]
