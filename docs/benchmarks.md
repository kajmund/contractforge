# Mätningar

Denna fil sammanfattar de senaste benchmark-körningarna. Fullständiga rapporter
ligger i `benchmarks/results/*.json`.

## Offline: pipeline-benchmark med mock-provider

Mäter hela kedjan (scenario → generering → rendering → verifiering → facit →
frågor → export) men **inte** LLM-kvalitet. Providern är deterministisk.

| Fält | Värde |
|------|-------|
| Kommando | `contractforge benchmark --type consulting --count 100 --clauses 10 --difficulty 3 --language sv --provider mock --seed 1234` |
| Avtal | 100 |
| Dokument | 200 |
| Godkända avtal | 100 |
| Reparerade dokument | 0 |
| Kasserade avtal | 0 |
| Golden-frågor | 2 200 (alla validerade) |
| Token | okänd (mock rapporterar ingen usage) |
| Kostnad | okänd (mock; ingen prisuppgift) |
| Tid | 9,6 s |
| Dokument/minut | 1 253 |
| Klausuler/minut | 8 143 |
| Verifiering | 100 % godkända |

**Tolkning:** siffrorna visar att pipeline, verifiering och export klarar
100 avtal utan nätverk. De säger ingenting om Cerebras-modellens kvalitet,
latens eller kostnad.

## Live: Cerebras-benchmark (20 avtal)

En live-körning mot Cerebras (`gpt-oss-120b`) med 20 konsultavtal på nivå 2,
8 klausuler, 4 parallella anrop och seed 777. Ett separat smoke-test (1 avtal)
körs med `@pytest.mark.live` när `CEREBRAS_API_KEY` finns; resultatet
dokumenteras med modell, promptversion, tokenförbrukning, latens och
verifieringsutfall. Kostnad redovisas endast om en explicit prisuppgift angetts.

| Fält | Värde |
|------|-------|
| Modell | `gpt-oss-120b` via Cerebras |
| Promptversion | `v1` |
| Avtal | 20 (konsultavtal, nivå 2, 8 klausuler) |
| Godkända | 20 (17 direkt, 3 reparerade efter första försöket) |
| Kasserade | 0 |
| Golden-frågor | 354 (alla validerade) |
| Token (in/ut) | 107 310 / 30 510 (totalt 137 820) |
| Latens | 1 117 ms genomsnitt per anrop |
| Tid | 6,9 s |
| Genomströmning | 174 dokument/min (4 parallella) |
| Kostnad | okänd utan `pricing.json` (inga priser hårdkodas) |

Se `benchmarks/results/cerebras_consulting_20.json` för fullständig rapport.

## Reproducerbarhet

- Benchmarken sparar seed, konfiguration, promptversion och prompt-hash i
  rapporten.
- Scenarier och konfiguration är deterministiska givet seed.
- LLM-text är inte garanterat bit-identisk mellan körningar.
