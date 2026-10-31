# Benchmarks

Reproducerbara mätningar av hela kedjan (scenario → generering → rendering →
verifiering → facit → frågor → export → datasetvalidering).

En körning skriver:

```
benchmarks/results/bench_<timestamp>/dataset/   # genererat dataset
benchmarks/results/bench_<timestamp>.json       # rapport med mätetal + verifierbarhetsdata
```

Körkatalogerna är gitignorerade (de kan bli stora). Den sammanfattande rapporten
kan kopieras till `benchmarks/results/<namn>.json` och checkas in.

**Ett live-datasets arkiv publiceras separat.** Benchmarkrapporten innehåller
hashar (`details.dataset_manifest_sha256` och
`details.dataset_contracts_hashes`) men inte själva datasetet. **Det dataset som
en live-körning genererar publiceras separat som en release-asset (zip)
tillsammans med sitt SHA-256; en rapport utan motsvarande arkiv är inte
reproducerbar och får inte citeras som verifierad.** Se
[docs/reproducibility.md](../docs/reproducibility.md).

## Kör

```bash
# offline: deterministisk mock-provider (CI-vänlig, ingen nyckel)
.venv/bin/python benchmarks/run_benchmark.py --count 100 --seed 1234

# live: Cerebras, kräver CEREBRAS_API_KEY och en explicit prisuppgift för kostnad
.venv/bin/python benchmarks/run_benchmark.py \
  --provider cerebras --count 100 --difficulty 4 --seed 1234 \
  --concurrency 4 --pricing pricing.json
```

Samma sak via CLI:

```bash
.venv/bin/contractforge benchmark --type consulting --count 100 \
  --clauses 10 --difficulty 3 --provider mock --seed 1234 \
  --output benchmarks/results
```

Katalogen `--output/<benchmark-id>/` skrivs av `generate_dataset`, som vägrar en
katalog som inte är tom och saknar ContractForge-markör. Använd en ny
`--benchmark-id` per körning (standard: tidsstämpel) eller `--force` om du
medvetet vill ersätta en tidigare körning.

## Mätetal

### `stats` (härledda, aritmetiskt kontrollerbara)

| Fält | Betydelse |
|------|-----------|
| `documents_total` | Antal genererade och exporterade dokument |
| `documents_per_minute` | Genomströmning inkl. verifiering och export |
| `clauses_per_minute` | Klausuler per minut |
| `mean_document_latency_ms` | **Viktat** medel över alla LLM-anrop: `Σ(mean_latency × calls) / Σ(calls)` |
| `input_tokens` / `output_tokens` / `total_tokens` | Tokenförbrukning |
| `cost_usd` / `cost_basis` | Faktisk kostnad endast med `PricingTable`; annars `null`/`unknown` |
| `verification_pass_rate` | **Dokumentnivå, första försöket:** `documents_accepted / documents_total`. Reparerade dokument är godkända avtal men räknas inte som första-försöks-godkända |
| `repair_rate` | Andel dokument som behövde reparation |
| `rejection_rate` | Andel avtal som kasserades |
| `questions_total` / `questions_validated` | Exporterade (och därmed validerade) golden-frågor |
| `questions_rejected` | Frågor som frågeverifieringen kasserade; exporteras aldrig |
| `cost_per_accepted_document_usd` | Kostnad per godkänt dokument (om kostnad känd) |
| `cost_per_validated_question_usd` | Kostnad per validerad golden-fråga (om kostnad känd) |

### `details` (verifierbarhet)

| Fält | Betydelse |
|------|-----------|
| `dataset_manifest_sha256` | SHA-256 för `dataset/dataset_manifest.json` |
| `dataset_contracts_hashes` | `contract_id → sha256` för varje avtals `manifest.json` |
| `dataset_contract_count` | Antal avtal i det beskrivna datasetet |
| `validation` | Slutlig `validate_dataset`-körning: `status`, `contracts_checked`, failures |
| `questions_accepted` | Frågor exporterade som validerade facit |
| `questions_rejected` / `rejected_question_ids` | Kasserade frågor och deras id:n |
| `latency` | `basis: "weighted_per_call"`, `calls`, `total_latency_ms`, `mean_latency_ms` |
| `runtime` | Python-version, implementation, plattform, contractforge-version, `git_commit`, `git_dirty`, beroendeversioner |
| `wall_clock_seconds` | Total väggtid inklusive export och verifiering |

`config` bär de fält som behövs för att reproducera scenarioingångarna: `seed`,
`contract_type`, `count`, `clauses`, `difficulty`, `language`, `jurisdiction`,
`package`, `variation_overrides`, `scenario_definitions` (hash per
avtalstyp-definition), `prompt_version`, `prompt_sha256`, genereringsparametrar
och `runtime`.

## Tolkning

- `provider=mock` mäter **pipeline, verifiering och export**, inte språklig
  kvalitet eller Cerebras-latens. Sådana siffror får aldrig citeras som
  modellresultat.
- `provider=cerebras` mäter den riktiga modellen. En seed reproducerar scenarier
  och konfiguration; LLM-texten är inte garanterat bit-identisk.
- Kostnadssiffror kräver att `pricing.json` matchar provider + modell. Utan
  prisuppgift redovisas ingen kostnad.
- En rapport är bara verifierbar om den har `details.dataset_manifest_sha256`,
  `details.runtime` och `details.validation` – äldre rapporter saknar dem och kan
  inte bindas till en korpus.

## Resultat

Se `benchmarks/results/*.json` för committade sammanfattningar och
[docs/benchmarks.md](../docs/benchmarks.md) för de senaste mätningarna.

## Verifierbara körningar

| Körning | Rapport | Corpus |
|---------|---------|--------|
| Offline pipeline (mock), 100 avtal | `results/offline_consulting_100.json` | återskapas med kommandot ovan |
| Live Cerebras `gpt-oss-120b`, 100 avtal | `results/cerebras_consulting_100.json` | release-asset `cerebras_consulting_100.zip` |

**Live-körningens dataset publiceras som release-asset** med publicerad SHA-256.
En livebenchmark får bara citeras som verifierad när arkivet är tillgängligt och
`contractforge validate` ger `PASS` på det uppackade innehållet. Rapporten
innehåller `details.dataset_manifest_sha256` och
`details.dataset_contracts_hashes`, så rapporten kan bindas till exakt det
arkiv som publicerades.

Rapporter som skapades före verifierbarhetsändringen saknar `details` och kan
inte bindas till någon corpus – de är borttagna.
