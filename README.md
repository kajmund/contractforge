# ContractForge

[![CI](https://github.com/kajmund/contractforge/actions/workflows/ci.yml/badge.svg)](https://github.com/kajmund/contractforge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Generator för syntetiska avtal och juridiska testdata med verifierbara facit.**

ContractForge producerar stora mängder realistiska, varierade och juridiskt
sammanhängande avtal – och ett separat, maskinläsbart facit för varje dokument.
Facit kan användas för att utvärdera AI-system, informationssökning,
dokumentanalys och juridisk informationsutvinning.

Projektet är fristående: det kräver ingen annan applikation, ingen databas och
ingen specifik testmotor. Kör det som CLI eller importera kärnan som ett
Python-bibliotek.

> **Syntetisk testdata.** Alla parter, organisationsnummer, adresser och belopp
> är påhittade. Dokumenten är märkta som syntetiska i metadata och manifest.
> Inget i projektet påstår att ett genererat villkor är juridiskt giltigt eller
> granskat av jurist.

---

## Innehåll

- [Varför ContractForge](#varför-contractforge)
- [Snabbstart](#snabbstart)
- [Cerebras](#cerebras)
- [CLI-referens](#cli-referens)
- [Så fungerar det](#så-fungerar-det)
- [Verifiering och facit](#verifiering-och-facit)
- [Svårighetsgrader](#svårighetsgrader)
- [Avtalstyper](#avtalstyper)
- [Datasetlayout](#datasetlayout)
- [Export](#export)
- [Benchmark och mätetal](#benchmark-och-mätetal)
- [Reproducerbarhet och kostnad](#reproducerbarhet-och-kostnad)
- [Säkerhet och datakvalitet](#säkerhet-och-datakvalitet)
- [Begränsningar](#begränsningar)
- [Projektstruktur](#projektstruktur)
- [Utveckling](#utveckling)
- [Licens](#licens)

---

## Varför ContractForge

Att generera en avtalstext är lätt. Att avgöra **vilka fakta texten faktiskt
innehåller**, var i texten de står och om svaret går att lita på – det är svårt.

ContractForge vänder på processen:

1. **Scenario först.** Ett strukturerat scenario beskriver parter, villkor,
   undantag, relationer och avsiktliga motsägelser med stabila id:n.
2. **Generering sedan.** En LLM (Cerebras) formulerar avtalstexten utifrån
   scenariot. Modellen får inte ändra materiella villkor.
3. **Oberoende verifiering.** Deterministiska och semantiska kontroller avgör om
   texten motsvarar scenariot. Underkända dokument repareras eller kasseras.
4. **Facit med evidens.** Först efter godkännande byggs `gold_facts.json` och
   `golden_questions.json` – med exakta teckenoffsett i den kanoniska texten.

Ett scenario blir alltså **inte** automatiskt facit. Facit är det som kan
bevisas mot den färdiga texten.

## Snabbstart

Krav: Python 3.10+.

```bash
git clone https://github.com/kajmund/contractforge.git
cd contractforge
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Generera 10 konsultavtal helt offline med den deterministiska mock-providern
(ingen API-nyckel, inga nätverksanrop):

```bash
.venv/bin/contractforge generate \
  --type consulting \
  --count 10 \
  --clauses 8 \
  --difficulty 2 \
  --language sv \
  --provider mock \
  --seed 7 \
  --output ./dataset
```

Verifiera datasetet (hashar, kanonisk text, evidenspann, frågestöd):

```bash
.venv/bin/contractforge validate ./dataset
```

Statistik och ZIP-export:

```bash
.venv/bin/contractforge stats ./dataset
.venv/bin/contractforge export ./dataset --format zip --output ./dataset.zip
```

> `provider=mock` är en **explicit vald offline-provider** för tester, CI och
> pipeline-benchmarks. Den är aldrig en tyst fallback, och dess siffror säger
> inget om en LLM:s kvalitet.

## Cerebras

Sätt API-nyckeln i miljön (den läses bara av providern, loggas aldrig och sparas
aldrig i dataset):

```bash
export CEREBRAS_API_KEY="csk-..."
# valfritt:
export CONTRACTFORGE_MODEL="gpt-oss-120b"     # default
export CEREBRAS_BASE_URL="https://api.cerebras.ai/v1"
```

Kör sedan riktig generering:

```bash
.venv/bin/contractforge generate \
  --type consulting \
  --count 100 \
  --clauses 10 \
  --difficulty 4 \
  --language sv \
  --provider cerebras \
  --seed 42 \
  --concurrency 4 \
  --output ./dataset
```

Adaptern stödjer modellval, genereringsparametrar, strukturerade JSON-instruktioner,
begränsad parallellitet (på contract-nivå), retry med exponentiell backoff och
`Retry-After`, timeout, token-/latensmätning, promptversionering och sparade
råsvar (`raw/` i varje avtalskatalog). Se [docs/cerebras.md](docs/cerebras.md).

## CLI-referens

```
contractforge --help                    # översikt
contractforge list-types                # alla avtalstyper
contractforge generate --help
contractforge validate ./dataset
contractforge generate-questions ./dataset
contractforge export ./dataset --format zip
contractforge export ./dataset --format jsonl
contractforge export ./dataset --format json
contractforge stats ./dataset
contractforge benchmark --type consulting --count 100 --provider mock --seed 1234
```

Alla kommandon stödjer `--json` för maskinläsbar utdata. Exit-koder:

| Kod | Betydelse |
|-----|-----------|
| 0 | lyckat |
| 1 | oväntat internt fel |
| 2 | felaktig användning/argument |
| 3 | valideringsfel (dataset underkänt) |
| 4 | providerfel (API, nätverk, kvot) |
| 5 | konfigurationsfel (t.ex. saknad API-nyckel) |
| 6 | scenariofel |
| 7 | genereringsfel (oanalyserbar modellutdata) |
| 8 | exportfel |

Exempel på maskinläsbart fel:

```json
{
  "status": "error",
  "command": "generate",
  "error": {
    "type": "config_error",
    "message": "CEREBRAS_API_KEY saknas ...",
    "details": {},
    "exit_code": 5
  }
}
```

## Så fungerar det

```
Scenario (strukturerad sanning, stabila id:n)
    │
    ├──> Prompt (versionerad) ──> Cerebras ──> strukturerad JSON (klausuler)
    │                                              │
    │                                              v
    │                                     Rendering ──> kanonisk text + spann
    │                                              │
    │                                              v
    │                              Verifiering (deterministisk + semantisk)
    │                                   │ PASS             │ FAIL
    │                                   v                  v
    │                             GoldSet (facit)     Reparation (max N)
    │                                   │                  │ uttömd
    │                                   v                  v
    │                          GoldenQuestions         Kasseras (rejected/)
    │                                   │
    └───────────────────────────────────┴──> Export: TXT/MD/DOCX/PDF + JSON/JSONL + ZIP
```

Reparationsloopen skickar verifieringens felcheckar tillbaka till modellen som
konkret feedback (t.ex. *"fact_notice_period: explicit surface 'tre (3)
månader' saknas i clause_03"*). Först när alla dokument är godkända byggs facit
och frågor. Annars hamnar avtalet i `rejected/` och exporteras aldrig som
godkänt.

## Verifiering och facit

**Deterministiska kontroller** (alltid):

- explicita värden finns ordagrant i rätt klausul,
- dokument- och klausulstruktur, ordning och unika id:n,
- referenser till existerande dokument/bilagor (eller deklarerad saknad bilaga),
- entiteter, parter och roller finns i texten,
- förväntade antal dokument/klausuler/fakta,
- inga oavsiktliga dupliceringar,
- alla evidenspann är giltiga och deras slice stämmer mot kanonisk text,
- avsiktliga motsägelser/undantag finns där scenariot säger.

**Semantiska kontroller**: värde- och enhetsnormalisering (siffror, svenska
talord, `ett kvartal` → 3 månader, `halvår` → 6 månader, `kr`/`SEK`,
`%`/`procent`) kopplad till faktats nyckelord. Verifieringen är deterministisk
och oberoende av generatorns modell. En LLM-domare får bara ge `WARN` och
räknas aldrig som oberoende facit – att köra samma modell i ett separat anrop
gör den inte oberoende.

**Statusar**: `ACCEPTED`, `REPAIRED`, `REJECTED`. Datasetet redovisar antal
godkända, reparerade och kasserade dokument samt kostnad per godkänt dokument.

Facit innehåller atomära fakta med normaliserade värden, entiteter och roller,
referenser, anomalies, evidenspann (med `precision`: `exact`, `paraphrase` eller
`clause`), samt vilka fakta som krävs för varje fråga.

### Facitens kvalitet

Facit är **strukturellt verifierat** – men inte semantiskt bevisat. Struktur,
exakta evidensspann, faktavärden mot scenariot och frågestöd enligt en
dokumenterad lexikal regel är bevisat; att en komplex juridisk parafras behållit
avsedd betydelse är antaget. Felprocenten mäts separat enligt
[docs/quality-protocol.md](docs/quality-protocol.md) – oberoende av den
deterministiska valideraren (rapportmall:
[docs/quality-report-mall.md](docs/quality-report-mall.md)). Status: **antagen
men ej mätt.** v0.1.1 är fryst som referens.

**Innan raterna börjar** (protokollet 4.3, 5.4 och 3.0): överensstämmelse mäts
och rapporteras **per axel** (Q1–Q4 var för sig; ett sammanslaget kappa är
endast information), raterna ska läsa **hela dokumentpaketet** – ett undantag i
en annan klausul kan ändra svaret – och en eventuell
populationsskattning för de 1 723 frågorna viktas separat med stratumvikter
(urvalsvärdet är standard och det som grindarna bedöms på).

Verktyget stödjer hela flödet: `publish-questions` skriver två **separata,
slumpmässigt ordnade** blanketter (`ratings_A.csv`, `ratings_B.csv`) med egna
hashar, och `score-ratings` beräknar felprocent, Wilson-KI, överensstämmelse
**per axel**, oenighetstyper och en rapportutkast. Överensstämmelsen beräknas
alltid på de **ursprungliga individuella** koderna; en efterhandsbedömning får
bara avgöra postens kvalitet, aldrig kappavärdet.

Ett färdigt mätpaket finns: [`examples/qsample_live_100/`](examples/qsample_live_100/)
– 100 blinda frågor från live-datasetet (källa `cerebras_consulting_100.zip`,
SHA-256 `43448450…ae7e`, urvals-seed 2026, `selection_sha256`
`1272bd8a…f650` i `selection.json`), med `ratings.csv` för två oberoende rater. Skapa ett nytt
urval med:

```bash
contractforge publish-questions <dataset.zip> --count 100 --seed 2026 --output <dir>
```

## Svårighetsgrader

| Nivå | Namn | Innehåll |
|------|------|----------|
| 1 | Explicit | Enkla villkor, tydliga värden |
| 2 | Parafraser | Samma fakta i alternativa juridiska formuleringar |
| 3 | Distraktorer | Flera närliggande värden; rätt operativ betydelse måste identifieras |
| 4 | Relationer | Fakta fördelade mellan klausuler, definitioner och bilagor |
| 5 | Motsägelser och undantag | Motstridiga villkor eller särskilda undantag; facit skiljer avsiktligt från oavsiktligt |
| 6 | Ofullständig information | Saknade bilagor, okända omständigheter, externa rättskällor; svar kan vara `INSUFFICIENT_EVIDENCE`, `CONDITIONAL` eller `CONFLICTING_EVIDENCE` |

## Avtalstyper

`employment`, `consulting`, `shareholders`, `nda`, `services`, `supply`,
`license`, `lease`, `cooperation`, `sale`.

Typdefinitionerna är data (JSON) i `src/contractforge/scenarios/definitions/`.
Nya avtalstyper läggs till genom nya definitioner – ingen specialkod.

Standardjurisdiktion är Sverige (`SE`) och standardspråk svenska (`sv`). Båda är
konfigurerbara per körning (`--jurisdiction`, `--language sv|en`).

## Datasetlayout

```
dataset/
├── contract_0001/
│   ├── main_agreement.txt        # kanonisk text (UTF-8) – källan för offsets
│   ├── main_agreement.md
│   ├── main_agreement.docx
│   ├── main_agreement.pdf
│   ├── appendix_a_prices.txt|.md|.docx|.pdf   # när paketet har bilagor
│   ├── amendment_01.txt|.md|.docx|.pdf
│   ├── scenario.json             # strukturerad sanning
│   ├── gold_facts.json           # facit med evidenspann
│   ├── golden_questions.json     # frågor + förväntade svar + evidens
│   ├── verification.json         # alla checkar + status
│   ├── rendered.json             # kanonisk text + clause_spans per dokument
│   ├── llm_calls.json            # saniterade anrop (prompt, usage, latens)
│   ├── raw/<call_id>.json        # råsvar för felsökning
│   └── manifest.json             # konfiguration, modell, promptversion, hashar, kostnad
├── dataset_manifest.json         # alla avtal, status, hashar, totals, run-identitet
├── stats.json                    # aggregerad statistik
├── validation_report.json        # resultatet av den slutliga datasetvalideringen
├── run.json                      # vilka filer körningen äger (säker återkörning)
├── .contractforge                # markör: katalogen är ett ContractForge-dataset
└── rejected/<contract_id>.json   # kasserade avtal – aldrig godkända (med
                                  # saniterade anrop, usage, kostnad och raw/)
```

**Skrivsäkerhet.** `generate` vägrar skriva i en katalog som inte är tom och som
inte är ett ContractForge-dataset (exit-kod 2) – ingenting raderas. Använd en ny
`--output`, eller `--force` om du uttryckligen accepterar att ersätta katalogen.
En tidigare ContractForge-körning i samma katalog städas endast för de filer som
listas i dess manifest; andra filer lämnas orörda, och ett byte av
`contract_id`-prefix kräver en ny katalog.

**Slutlig validering.** Efter export kör `generate` samma validering som
`contractforge validate`. Blir resultatet inte `PASS` avslutas körningen med
exit-kod 3 – ett inkonsekvent dataset kan aldrig rapporteras som lyckat.

## Export

- **Dokument**: `.txt`, `.md`, `.docx`, `.pdf`
- **Strukturerad data**: JSON per avtal samt `--format jsonl` för en rad per avtal
- **Testpaket**: ZIP med dokument, scenario, facit, frågor, manifest och statistik

Exporten är versionssatt (`schema_version`), innehåller konfiguration,
scenario-id, faktum-id, modell, promptversion, genereringsparametrar, seed,
SHA-256-hashar för dokument och facit, verifieringsresultat och kostnad.

## Benchmark och mätetal

```bash
# offline pipeline-benchmark (deterministisk, ingen API-nyckel)
.venv/bin/contractforge benchmark \
  --type consulting --count 100 --clauses 10 --difficulty 3 \
  --language sv --provider mock --seed 1234 \
  --output benchmarks/results
```

```bash
# live-benchmark mot Cerebras (kräver CEREBRAS_API_KEY + ev. pricing.json)
CEREBRAS_API_KEY=... .venv/bin/contractforge benchmark \
  --type consulting --count 100 --clauses 10 --difficulty 4 \
  --language sv --provider cerebras --seed 1234 \
  --concurrency 4 --pricing pricing.json \
  --output benchmarks/results
```

Rapporten redovisar: dokument/minut, klausuler/minut, genomsnittlig
genereringslatens (viktat över alla anrop), tokenförbrukning, faktisk kostnad
(endast om en explicit prisuppgift angetts), andel godkända/reparerade/kasserade
dokument, kostnad per godkänt dokument och kostnad per validerad golden-fråga.

Rapporten är **verifierbar**: `details` innehåller SHA-256 för
`dataset_manifest.json`, alla avtals manifest-hashar, valideringsresultatet,
antal accepterade/avvisade golden-frågor, körmiljö (`python_version`, `platform`,
`contractforge_version`, `git_commit`, beroenden) och latensens
beräkningsmetod. En rapport utan motsvarande corpus kan inte bindas till sin
data.

Inga priser eller prestandaantaganden är hårdkodade för en viss
Cerebras-modell. Utan `pricing.json` rapporteras kostnad som `unknown`.
Se [`docs/reproducibility.md`](docs/reproducibility.md) för exakt vilka fält som
gör en körning återskapbar och hur ett publicerat dataset verifieras.

Senaste mätningarna (fullständig rapport i
[`docs/benchmarks.md`](docs/benchmarks.md) och
[`benchmarks/results/`](benchmarks/results/)):

| Körning | Provider | Avtal | Godkända | Reparerade dokument | Dokument/min | Token | Kostnad |
|---------|----------|-------|----------|---------------------|--------------|-------|---------|
| Offline pipeline | mock | 100 | 100 | 0 | 1 367 | okänd | okänd |
| ~~Live (v0.1.0, legacy)~~ | `gpt-oss-120b` | 20 | 20 | 3 | **174** | 137 820 | okänd² |
| Live Cerebras | `gpt-oss-120b` | 100 | 100 | 11 | 43,5 | 675 874 | okänd¹ |

² **Legacy-referens:** 20-avtalskörningen gjordes före v0.1.1-fixarna med `max_repairs=1` och svagare verifiering; den är bevarad för jämförelse men inte verifierbar mot ett corpus. Nuvarande 43,5 dokument/min kör med två reparationsförsök, full verifiering och datasetvalidering – se [`docs/benchmarks.md`](docs/benchmarks.md).

¹ Ingen prisuppgift angavs. Med `pricing.json` beräknas faktisk kostnad per
godkänt dokument och per validerad golden-fråga. Live-körningen tog 137,9 s med
4 parallella anrop (effektiv samtidighet 3,91) och 4 781 ms genomsnittlig
anropstid (viktat över alla 112 anrop); 99 % av väggtiden var LLM-väntan.
89 dokument godkändes på första försöket, 11 reparerades.

**Livekörningens dataset-arkiv** publiceras som release-asset:
`cerebras_consulting_100.zip`, SHA-256
`4344845099a39dbdc7d09c54f6d50cf03e74d1f831c78eda34cb67182893ae7e`.
Packa upp och kör `contractforge validate <katalog>` – resultatet ska vara
`PASS` för alla 100 avtal.

**Live-körningens dataset-arkiv publiceras separat** som en ZIP-release-asset med
publicerad SHA-256; en livebenchmark får bara citeras som verifierad när arkivet
är tillgängligt och `contractforge validate` ger `PASS` på det.

Ett litet live-genererat exempeldataset finns i
[`examples/sample_live_consulting/`](examples/sample_live_consulting/) och kan
valideras med `contractforge validate examples/sample_live_consulting`.

> Rapporterna under `benchmarks/results/` som skapades före
> verifierbarhetsändringen saknar `details`; de kan inte bindas till en corpus.
> Se [`docs/benchmarks.md`](docs/benchmarks.md) för vilka körningar som gäller.

## Reproducerbarhet och kostnad

- En seed reproducerar **scenario och experimentkonfiguration**.
- LLM-text är **inte** garanterat bit-identisk mellan körningar, eftersom
  leverantören inte garanterar deterministisk inferens. Det dokumenteras i
  manifestet.
- Promptar är versionshanterade (`prompt_version` + `prompt_sha256` i manifestet).
- Alla scenarioingångar sparas: typ, antal, klausuler, svårighetsgrad, språk,
  jurisdiktion, `package`, variationsöverskridanden, seed och SHA-256 för
  avtalstypens definition.
- Körmiljön sparas (`runtime`): ContractForge-version, Python, plattform,
  Git-commit, dirty-flagga och beroendeversioner.
- Beroenden kan låsas exakt: `pip install -c constraints.txt -e ".[dev]"`.
- Kostnad beräknas endast från en explicit `PricingTable` (JSON). Exempelformat:
  [`examples/pricing.example.json`](examples/pricing.example.json). Fyll i
  aktuella priser från leverantörens prislista – inga priser gissas av
  ContractForge.

## Säkerhet och datakvalitet

- Syntetiska parter och uppgifter som standard; inga verkliga personuppgifter
  eller identifierande organisationsnummer.
- Dokumenten märks som syntetisk testdata i metadata/manifest och i den
  kanoniska textens huvud – utan att störa klausultexterna.
- Inga externa dokumentkorpusar krävs.
- Syntetiska organisationsnummer använder serien `5590xx` och är medvetet
  **Luhn-ogiltiga**, så de kan inte vara en verklig organisations identitet.
- API-nycklar sparas aldrig i dataset, manifest, loggar eller felmeddelanden:
  providersvars, undantag och progress-rader redigeras (`[REDACTED]`) innan de
  kan skrivas till disk.
- Generatorn påstår inte att syntetiska villkor är rättsligt giltiga enbart för
  att formuleringen är realistisk.

## Begränsningar

- Första versionen är en vertikal implementation: scenario → generering →
  rendering → verifiering → facit → frågor → export.
- `provider=mock` mäter pipeline och verifiering, inte språklig kvalitet.
- Semantisk verifiering är deterministisk men heuristisk; osäkra fall flaggas
  (`WARN`) eller kasseras. Den ersätter inte mänsklig juridisk granskning.
- Frågegenereringen är mallbaserad och deterministisk, inte en LLM-domare.
- Ingen databas: filer, manifest och hashar räcker för första versionen.
- API-lagret är förberett via samma kärna men inte byggt ännu.

## Projektstruktur

```
contractforge/
├── pyproject.toml
├── README.md
├── LICENSE
├── Makefile
├── .github/workflows/ci.yml
├── src/contractforge/
│   ├── cli/              # argparse-CLI med JSON-envelope och exit-koder
│   ├── scenarios/        # scenariomotor + 10 datadrivna avtalstyper
│   ├── generation/       # promptversioner, JSON-parsning, genereringsmotor
│   ├── providers/        # Provider-protokoll, Cerebras-adapter, mock-provider
│   ├── validation/       # deterministisk + semantisk verifiering, datasetvalidering
│   ├── gold/             # facit med evidensspann
│   ├── questions/        # golden-frågor + verifiering
│   ├── rendering/        # kanonisk text, TXT/MD/DOCX/PDF
│   ├── export/           # datasetlayout, manifest, ZIP/JSONL, statistik
│   ├── models.py         # delad domänmodell (pydantic, strict)
│   ├── config.py         # inställningar via env + CLI
│   ├── runtime.py        # körmiljö i manifest/rapport (Python, commit, beroenden)
│   ├── sanitize.py       # sekretessredigering av fel, loggar och råsvar
│   ├── pipeline.py       # orkestrering, reparationsloop, slutlig datasetvalidering
│   └── benchmark.py      # reproducerbar, verifierbar benchmark
├── tests/                # 1 754 tester: enhet, integration (mock) och live
├── constraints.txt       # exakt låsta beroenden
├── examples/
├── docs/
└── benchmarks/
```

## Utveckling

```bash
make install        # venv + dependencies
make lint           # ruff
make test           # pytest, utan live/slow
make benchmark-offline
```

CI (GitHub Actions) kör på Python 3.10, 3.11 och 3.12:

- `ruff` och hela testsviten utan nätverksanrop (1 754 tester, ~12 s),
- ett offline end-to-end-smoke-test som genererar och validerar alla 10
  avtalstyper × nivå 1–6,
- en integrationsgrind som kräver att varje dataset får `PASS` och att antalet
  exporterade frågor är lika med antalet validerade frågor – ingen fråga får
  accepteras av generatorn men underkännas av datasetvalideringen.

Live-tester mot Cerebras är markerade `@pytest.mark.live` och hoppas över när
`CEREBRAS_API_KEY` saknas:

```bash
.venv/bin/python -m pytest -m live -q
```

## Licens

MIT – se [LICENSE](LICENSE).
