# Benchmarks

Reproducerbara mätningar av hela kedjan. En körning skriver:

```
benchmarks/results/bench_<timestamp>/dataset/   # genererat dataset
benchmarks/results/bench_<timestamp>.json       # rapport med mätetal
```

Körkatalogerna är gitignorerade (de kan bli stora). Den sammanfattande
rapporten kan kopieras till `benchmarks/results/<namn>.json` och checkas in.

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

## Mätetal

| Fält | Betydelse |
|------|-----------|
| `documents_total` | Antal genererade och exporterade dokument |
| `documents_per_minute` | Genomströmning inkl. verifiering och export |
| `clauses_per_minute` | Klausuler per minut |
| `mean_document_latency_ms` | Genomsnittlig latens per LLM-anrop |
| `input_tokens` / `output_tokens` / `total_tokens` | Tokenförbrukning |
| `cost_usd` / `cost_basis` | Faktisk kostnad endast med `PricingTable`; annars `null`/`unknown` |
| `verification_pass_rate` | Andel avtal som godkändes (inkl. reparerade) |
| `repair_rate` | Andel dokument som behövde reparation |
| `rejection_rate` | Andel avtal som kasserades |
| `cost_per_accepted_document_usd` | Kostnad per godkänt dokument (om kostnad känd) |
| `cost_per_validated_question_usd` | Kostnad per validerad golden-fråga (om kostnad känd) |

## Tolkning

- `provider=mock` mäter **pipeline, verifiering och export**, inte språklig
  kvalitet eller Cerebras-latens.
- `provider=cerebras` mäter den riktiga modellen. En seed reproducerar scenarier
  och konfiguration; LLM-texten är inte garanterat bit-identisk.
- Kostnadssiffror kräver att `pricing.json` matchar provider + modell. Utan
  prisuppgift redovisas ingen kostnad.

## Resultat

Se `benchmarks/results/*.json` för committade sammanfattningar och
[docs/benchmarks.md](../docs/benchmarks.md) för den senaste mätningen.
