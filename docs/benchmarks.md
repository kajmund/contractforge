# Mätningar

Denna fil sammanfattar de senaste benchmark-körningarna. Fullständiga rapporter
ligger i `benchmarks/results/*.json`.

## Rapportens fält (och vad som gör den verifierbar)

Varje rapport skriver dessa fält i `details` utöver `stats`:

| Fält | Betydelse |
|------|-----------|
| `details.dataset_manifest_sha256` | SHA-256 för datasetets `dataset_manifest.json` – binder rapporten till exakt den korpus den beskriver |
| `details.dataset_contracts_hashes` | `contract_id → sha256` för varje avtals `manifest.json` |
| `details.dataset_contract_count` | Antal avtal i det beskrivna datasetet |
| `details.validation` | Den slutliga `validate_dataset`-körningen (status, `contracts_checked`, failures) |
| `details.questions_accepted` | Antal frågor som exporterades som validerade facit |
| `details.questions_rejected` | Antal frågor som frågeverifieringen kasserade (exporteras aldrig) |
| `details.rejected_question_ids` | Id:n för de kasserade frågorna |
| `details.latency` | `basis: "weighted_per_call"`, `calls`, `total_latency_ms`, `mean_latency_ms` |
| `details.runtime` | `python_version`, `python_implementation`, `platform`, `contractforge_version`, `git_commit`, `git_dirty`, `dependencies`, `environment` |
| `details.wall_clock_seconds` | Total väggtid inklusive export och verifiering |

**Latens viktas över alla anrop:** `mean_latency_ms = Σ(latens) / Σ(antal anrop)`,
inte medelvärdet av per-avtals-medelvärden. Ett avtal med 20 anrop får inte väga
lika tungt som ett med 2 anrop.

**Datasettarkivet publiceras separat.** Benchmarkrapporten innehåller hashar men
inte själva datasetet. **Det dataset som en live-körning genererar publiceras
separat som en release-asset (zip) tillsammans med sitt SHA-256 och
manifest-hash; en rapport utan motsvarande arkiv är inte reproducerbar.** Se
[docs/reproducibility.md](reproducibility.md) för hur ett publicerat arkiv
verifieras.

## Offline: pipeline-benchmark med mock-provider

Mäter hela kedjan (scenario → generering → rendering → verifiering → facit →
frågor → export → datasetvalidering) men **inte** LLM-kvalitet. Providern är
deterministisk.

| Fält | Värde |
|------|-------|
| Kommando | `contractforge benchmark --type consulting --count 100 --clauses 10 --difficulty 3 --language sv --provider mock --seed 1234` |
| Rapport | `benchmarks/results/offline_consulting_100.json` |
| Avtal | 100 |
| Dokument | 200 |
| Godkända avtal | 100 |
| Reparerade dokument | 0 |
| Kasserade avtal | 0 |
| Golden-frågor | 2 300 (alla validerade) |
| LLM-anrop | 200 |
| Token | okänd (mock rapporterar ingen usage) |
| Kostnad | okänd (mock; ingen prisuppgift) |
| Tid | 12,2 s |
| Dokument/minut | 981 |
| Klausuler/minut | 6 374 |
| Verifiering (dokumentnivå, första försök) | 100 % godkända |

**Tolkning:** siffrorna visar att pipeline, verifiering, export och
datasetvalidering klarar 100 avtal utan nätverk. De säger ingenting om
Cerebras-modellens kvalitet, latens eller kostnad.

## Live: Cerebras-benchmark

En live-körning mot Cerebras (`gpt-oss-120b`, promptversion `v1`): 100
konsultavtal på nivå 2 med 8 klausuler, seed 4242, 4 parallella anrop och
`max_repair_attempts = 2`.

| Fält | Värde |
|------|-------|
| Rapport | `benchmarks/results/cerebras_consulting_100.json` |
| Dataset-arkiv (release-asset) | `cerebras_consulting_100.zip` |
| SHA-256 för arkivet | `51554ba465aabf6c4e319da4559fce5d82b330a9083f054c522fd3607442e390` |
| `details.dataset_manifest_sha256` | `542842796b4b6be2…` (fullständigt värde i rapporten) |
| Godkända avtal | 100 av 100 |
| Dokument godkända på första försöket | 89 |
| Reparerade dokument | 11 |
| Kasserade avtal | 0 |
| Golden-frågor | 1 723 (alla validerade, 0 kasserade) |
| LLM-anrop | 112 |
| Token (in/ut/totalt) | 522 351 / 153 523 / 675 874 |
| Latens (viktat medel per anrop) | 4 215 ms |
| Tid | 123,6 s |
| Genomströmning | 48,5 dokument/min |
| Kostnad | okänd utan `pricing.json` |

**`verification_pass_rate` är dokumentnivå och avser första försöket:**
`documents_accepted / documents_total`, där `documents_accepted` räknar dokument
som passerade verifieringen utan reparation. Ett dokument som reparerades är
fortsatt ett godkänt avtal, men räknas inte som första-försöks-godkänt. Därför
är värdet 0,89 = 89/100 – inte 1,0 trots att alla avtal exporterades.

**Verifiering av körningen:** ladda ner arkivet från release-sidan, packa upp och
kör

```bash
contractforge validate <uppackad katalog>   # ska ge PASS för 100 avtal
shasum -a 256 cerebras_consulting_100.zip   # ska matcha hashen ovan
```

Rapportens `details.dataset_manifest_sha256` ska matcha SHA-256 av
`dataset_manifest.json` i det uppackade arkivet, och
`details.dataset_contracts_hashes` ska matcha varje avtals `manifest.json`.

Kostnad redovisas endast om en explicit prisuppgift angetts; inga modellpriser
hårdkodas.

## Reproducerbarhet

Vad som faktiskt är reproducerbart (detaljer i
[docs/reproducibility.md](reproducibility.md)):

| Fält | Reproducerar |
|------|--------------|
| `config.seed` | Scenarioingångarna: varje scenario är `seed + index`, alla val dras ur samma seedade RNG |
| `config.contract_type`, `count`, `clauses`, `difficulty`, `language`, `jurisdiction`, `package` | Formen på varje scenario (avtalstyp, antal avtal, klausulantal, nivå, språk, dokumentpaket) |
| `config.variation_overrides` | Eventuella överstyrningar av variationsprofilen |
| `config.scenario_definitions[].sha256` | Exakt vilken avtalstyp-definition som användes (den data som styr fakta, klausulplan och anomalies) |
| `config.prompt_version` + `config.prompt_sha256` | Exakt prompttext som skickades till modellen |
| `config.temperature`, `top_p`, `max_tokens`, `max_repair_attempts`, `concurrency` | Genereringsparametrar och reparationsloop |
| `config.runtime` | Python-version, implementation, plattform, contractforge-version, git-commit, git-dirty och beroendeversioner |

Reproducerbart i strikt mening: scenarier, konfiguration och alla
verifierings- och facitsteg (de är deterministiska och kräver inga API-anrop).

**Inte** garanterat bit-identiskt: LLM-texten i en live-körning. Samma seed
reproducerar scenariot, men providern kan returnera annan text vid ett nytt
anrop – därför reproduceras en live-körning genom att verifiera det publicerade
arkivet, inte genom att köra om generationen.

Ett `provider=mock`-resultat är inte ett modellresultat. Det får aldrig citeras
som Cerebras kvalitet, latens eller kostnad.
