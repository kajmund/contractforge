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
| Tid | 9,4 s |
| Dokument/minut | 1 367 |
| Klausuler/minut | 8 336 |
| Varav LLM-väntan | 0 s (mock rapporterar ingen latens) |
| Varav export (DOCX/PDF/TXT) | ~6,4 s |
| Varav datasetvalidering | 0,3 s |
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
| SHA-256 för arkivet | `4344845099a39dbdc7d09c54f6d50cf03e74d1f831c78eda34cb67182893ae7e` |
| `details.dataset_manifest_sha256` | `7ddd32c544a21f60…` (fullständigt värde i rapporten) |
| Godkända avtal | 100 av 100 |
| Dokument godkända på första försöket | 89 |
| Reparerade dokument | 11 |
| Kasserade avtal | 0 |
| Golden-frågor | 1 723 (alla validerade, 0 kasserade) |
| LLM-anrop | 112 |
| Token (in/ut/totalt) | 522 351 / 153 523 / 675 874 |
| Latens (viktat medel per anrop) | 4 781 ms |
| Tid | 137,9 s |
| Genomströmning | 43,5 dokument/min |
| Effektiv samtidighet | 3,91 (LLM-väntan = 99 % av väggtiden) |
| Kostnad | okänd utan `pricing.json` |

**Genomströmning redovisas med LLM-väntan separerad** (`details.throughput`):
i livekörningen var cirka 137 av 138 s väntan på Cerebras (536 s summerad
anropstid fördelad på 4 samtidiga anrop), medan lokal verifiering/rendering/export
tog 0,70 s. En dokument/min-siffra för ett LLM-baserat system mäter därför i
praktiken leverantörens latens – den mock-körda pipeline-siffran (1 367
dokument/min) mäter ContractForge självt.

### Bevarad tidigare mätning (legacy-referens)

Den första 100-avtalskörningen (`benchmarks/results/cerebras_consulting_20.json` är
20-avtalskörningen; 100-avtalskörningen från samma generation är ersatt) visade
**174 dokument/min** mot nuvarande **48,5**. Den siffran var verklig för sin
konfiguration men **inte jämförbar**: den körningen körde bara text- och
markdown-export, `max_repair_attempts=1`, och gjordes före F3/F7/F8-fixarna, dvs.
med en svagare verifiering och utan den slutliga datasetvalideringen i
`generate`. Nuvarande körning exporterar TXT+Markdown, tillåter två
reparationsförsök, verifierar allt igen och kör datasetvalidering efter export.

| Körning | Datum | Konfiguration | Dokument/min | Anrop | Token | Verifiering före/efter |
|---------|-------|---------------|--------------|-------|-------|------------------------|
| v0.1.0 (legacy) | 2026-10-31 10:54 | 20 avtal, `max_repairs=1`, txt+md | **174** | 23 | 137 820 | svagare (F3/F7/F8 ej fixade) |
| v0.1.1 | 2026-10-31 13:34 | 100 avtal, `max_repairs=2`, txt+md | **48,5** | 112 | 675 874 | starkare + datasetvalidering |

Legacy-filen har `details.legacy = true` och kan inte bindas till ett corpus. Den
bevaras för att hastighetsdiskussionen ska kunna följas bakåt, inte som bevis.


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
