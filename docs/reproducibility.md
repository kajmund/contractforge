# Reproducerbarhet

Detta dokument beskriver exakt vad som kan reproduceras i en ContractForge-körning,
vilka fält som bevisar det, och hur du verifierar ett publicerat dataset utan att
köra om genereringen.

## Vad "reproducerbar" betyder här

ContractForge skiljer på två saker som lätt blandas ihop:

1. **Reproducerbart (deterministiskt).** Scenarier, rendering, verifiering, facit,
   golden-frågor och export är rena funktioner av scenariot. Samma
   `(seed, request, definitions, promptversion)` ger samma scenario, samma
   kanoniska text och samma facit – utan nätverk. Detta gäller för alla
   provbrytare, även `provider=cerebras`.
2. **Inte reproducerbart.** Den genererade *texten* från en live-modell är inte
   garanterat bit-identisk mellan två anrop, ens med samma seed. En live-körning
   reproduceras därför genom att **verifiera det publicerade datasetet**, inte
   genom att köra om generationen.

Ett `provider=mock`-resultat är en pipeline-mätning, inte ett modellresultat.

## Fälten som krävs för att reproducera scenarioingångarna

Alla ligger i rapportens `config` (`GenerationConfigSnapshot`) och kopieras in i
`dataset_manifest.json`:

| Fält | Reproducerar | Krävs för |
|------|--------------|-----------|
| `seed` | Scenarioingångarna. Scenario `index` använder `seed + index`; alla val dras ur en enda seedad RNG | Alla scenarier |
| `contract_type` | Avtalstyp (definition som styr fakta, klausulplan, dokumentpaket, anomalies) | Alla scenarier |
| `count` | Antal avtal i körningen | Korpusen |
| `clauses` | Antal klausuler i huvudavtalet | Scenarioform |
| `difficulty` | Nivå 1–6: explicit/parafras, distraktorer, faktadistribution, anomalies, saknad information | Scenarioinnehåll |
| `language` | `sv` eller `en` (frågemallar, rubriker, kanonisk text) | Text och frågor |
| `jurisdiction` | Jurisdiktion i scenariot och dokumenthuvudet | Kanonisk text |
| `package` | Om dokumentpaket (huvudavtal + bilaga + tillägg) ska byggas | Antal dokument |
| `variation_overrides` | Eventuella överstyrningar av variationsprofilen (stil, directness, struktur, kvalitet) | Textvariation |
| `scenario_definitions` | `[{contract_type, path, sha256}]` – SHA-256 för varje använd definitionsfil | Att bevisa *vilken* data som styrde scenariot |
| `prompt_version` + `prompt_sha256` | Exakt prompttext som skickades till modellen (`prompt_sha256` är en digest av system- och användarmallen) | LLM-körningar |
| `temperature`, `top_p`, `max_tokens` | Genereringsparametrar | LLM-körningar |
| `max_repair_attempts` | Reparationsloopen (hur många försök ett underkänt avtal får) | Statusar och `repair_rate` |
| `concurrency` | Parallellitet (påverkar latens, inte innehåll) | Tid/latens |
| `runtime` | `contractforge_version`, `python_version`, `python_implementation`, `platform`, `git_commit`, `git_dirty`, `dependencies` | Att bevisa *vilken kod* som körde |

`runtime.git_commit` sätts endast när körningen sker i ett git-arbetsträd; annars
är den `null`. Ett paketerat dataset utan commit kan fortfarande verifieras, men
kan inte bindas till en exakt källrevision.

## Kravfil: `constraints.txt`

Beroendeversionerna som påverkade en körning finns i `config.runtime.dependencies`
(`pydantic`, `httpx`, `python-docx`, `reportlab`). För att frysa en miljö:

```bash
.venv/bin/python -m pip freeze > constraints.txt
.venv/bin/python -m pip install -r constraints.txt
```

Att installera om med samma `constraints.txt` ger samma biblioteksversioner, men
det är `runtime`-fältet i rapporten som är beviset för vilka versioner som
faktiskt användes.

## Verifiera ett publicerat dataset

Ett publicerat dataset är en zip med samma träd som körkatalogen:

```
dataset_manifest.json
stats.json
validation_report.json
contract_0001/
    main_agreement.txt
    scenario.json
    gold_facts.json
    golden_questions.json
    verification.json
    rendered.json
    manifest.json
rejected/<contract_id>.json     # endast om avtal kasserades
```

Verifiering kräver **inte** samma modell, samma seed eller nätverk:

```bash
# 1. Packa upp och kontrollera att arkivets SHA-256 matchar det publicerade värdet
shasum -a 256 dataset.zip

# 2. Verifiera hela datasetet (hashar, .txt mot kanonisk text, spann, facit,
#    frågor, manifestets totals) – exit 0 endast vid PASS
contractforge validate ./dataset

# 3. Maskinläsbart resultat
contractforge validate ./dataset --json
```

`contractforge validate` räknar om varje manifest-hash, jämför varje `.txt` med
`rendered.json:s canonical_text`, kontrollerar att varje evidensspan är giltig
och att dess `quote` är exakt slice, verifierar att varje frågas förväntade svar
stöds av den kanoniska texten, och kontrollerar att dataset-manifestets totals
stämmer med katalogerna.

### Verifiera att rapporten beskriver datasetet

Rapportens `details.dataset_manifest_sha256` ska vara lika med SHA-256 för det
uppackade datasetets `dataset_manifest.json`, och
`details.dataset_contracts_hashes` ska matcha varje avtals `manifest.json`:

```bash
shasum -a 256 dataset/dataset_manifest.json
shasum -a 256 dataset/contract_0001/manifest.json
```

Om dessa inte matchar är rapporten inte bunden till datasetet och får inte
citeras som verifierad.

## Frågekassationer är en del av resultatet

Frågor som frågeverifieringen underkänner exporteras **aldrig** som validerade
facit, men de är synlig statistik:

- `stats.questions_rejected` i dataset-manifestet och i benchmarkrapporten,
- `details.rejected_question_ids` i benchmarkrapporten (id:n),
- en rad i CLI-utskriften (`Avvisade golden-frågor: N`).

En fråga som accepteras av frågegeneratorn accepteras alltid av
`contractforge validate` för oförändrade data: båda använder samma regelverk
(`contractforge.validation.dataset.question_support_problems`). Det grinden
testas i `tests/test_questions.py`.

## Samma kommando, samma scenario

För att återkörda en offline-körning exakt:

```bash
contractforge generate --type consulting --count 100 --clauses 10 \
  --difficulty 3 --language sv --provider mock --seed 1234 --output ./repro
```

`./repro` innehåller samma scenarier, texter, facit och frågor som en tidigare
mock-körning med samma fält. `runtime.git_commit` och `generated_at` skiljer sig,
eftersom de beskriver körningen, inte innehållet.
