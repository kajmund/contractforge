# Arkitektur – ContractForge

Detta dokument är den bindande modul- och gränssnittsbeskrivningen. Alla
subsystem delar domänmodellen i `src/contractforge/models.py`.

## Grundprincip: facit före dokument

```
Scenario (sanning)  ->  Generering (LLM)  ->  Rendering (kanonisk text)
        |                                            |
        |                                            v
        |                                   Verifiering (deterministisk + semantisk)
        |                                            |
        v                                            v
   GoldSet (facit)   <-----------------------  Godkänt dokument
        |
        v
  GoldenQuestions (frågor med evidensspann)
        |
        v
  Export (filer + manifest + hashar + kostnad)
```

Ett scenario är aldrig automatiskt giltigt facit. Om modellen utelämnar,
förändrar eller motsäger ett faktum ska dokumentet flaggas, repareras och
verifieras på nytt eller kasseras.

## Modulägare och publika API:er

Varje modul får bara importera `models`, `errors`, `utils`, `config` samt egna
undermoduler och deklarerade tredjepartsberoenden. Inga cirkulära importer.
`pipeline` (Lead) är den enda modulen som känner till alla subsystem; `cli` får
bara importera `pipeline`, `export`, `validation.dataset` och `config`.

### scenarios (`scenarios/`) – ägare: scenario-dev

```
scenarios/registry.py
    class ContractTypeRegistry:
        @classmethod load(cls, definitions_dir: Path | None = None) -> ContractTypeRegistry
        def get(self, contract_type: ContractType | str) -> ContractTypeDefinition
        def types(self) -> list[str]
        def __len__(self) -> int
    def load_registry(definitions_dir: Path | None = None) -> ContractTypeRegistry

scenarios/generator.py
    class ScenarioGenerator:
        def __init__(self, registry: ContractTypeRegistry) -> None
        def generate(self, request: ScenarioRequest, index: int = 0) -> Scenario
```

* Avtalstyper definieras som data i `scenarios/definitions/*.json` – ingen
  specialkod per typ. Alla tio typer i specen ska finnas.
* Deterministisk givet `(request, index)`: `seed = request.seed + index` när
  `request.seed` är satt, annars slumpas ett fruset seed och sparas i scenariot.
* `contract_id = f"{request.contract_id_prefix}_{index + 1:04d}"`.
* Stödjer svårighetsgrad 1–6, dokumentpaket (huvudavtal + bilaga + tillägg när
  `request.package` och nivån kräver det), distraktorer, relationer, avsiktliga
  anomalies (nivå 5) och saknad information (nivå 6).

### providers (`providers/`) – ägare: provider-dev

```
providers/base.py
    class Provider(Protocol):
        name: str
        model: str
        def complete(self, request: LLMRequest) -> LLMResponse: ...

providers/cerebras.py
    class CerebrasProvider:  # OpenAI-kompatibel /chat/completions
        def __init__(self, *, api_key, base_url, model, timeout_s, max_retries,
                     backoff_s, temperature, top_p, max_tokens) -> None

providers/mock.py
    class MockProvider:  # deterministisk offline-provider för tester/CI/benchmark
        def __init__(self, *, model: str = "mock-1") -> None

providers/__init__.py
    def create_provider(settings: Settings) -> Provider
```

* `CerebrasProvider` ansvarar för HTTP, timeouts, retry med exponentiell backoff,
  `Retry-After`, JSON-läge (`response_format={"type": "json_object"}` när
  `LLMRequest.response_format == "json"`), token-/latensmätning och råsvar.
  Nyckeln får aldrig hamna i `LLMResponse`, loggar eller manifest.
* `MockProvider` är en **explicit vald** provider (`--provider mock`), aldrig en
  tyst fallback. Den läser `request.metadata["scenario"]` /
  `["document"]` och producerar deterministisk avtalstext. Manifestet markerar
  `provider: "mock"` och benchmarkrapporten `provider_is_mock: true`.
* Inga modellpriser hårdkodas. Kostnad beräknas endast från en explicit
  `PricingTable`; annars `cost_usd = None`, `cost_basis = "unknown"`.

### generation (`generation/`) – ägare: provider-dev

```
generation/prompts.py
    PROMPT_VERSION: str
    def prompt_sha256() -> str
    def build_contract_request(scenario, document, settings, *,
                               attempt: int = 1,
                               feedback: list[str] | None = None) -> LLMRequest

generation/parsing.py
    def extract_json_object(text: str) -> dict[str, Any]

generation/engine.py
    @dataclass
    class GenerationOutcome:
        package: GeneratedPackage
        calls: list[LLMCallRecord]

    class DocumentGenerator:
        def __init__(self, provider: Provider, settings: Settings) -> None
        def generate(self, scenario: Scenario, *,
                     feedback: list[str] | None = None,
                     attempt: int = 1) -> GenerationOutcome
```

* Promptar ligger i `generation/prompts/v1/*.md` med versionsnamn; prompt-hash
  exponeras för manifestet.
* Modellen får scenariots fakta och instruktioner om stil, struktur,
  komplexitet, språk och juridiskt språk. Den ska returnera JSON med ett objekt
  per dokument: `{"document_id": ..., "clauses": [{"clause_id": ...,
  "heading": ..., "text": ...}]}`.
* `DocumentGenerator` validerar att exakt planerade clause-id:n kommer tillbaka
  (inga nya, inga saknade), annars `GenerationError`.
* `LLMRequest.metadata` innehåller `scenario`/`document` som JSON (för
  MockProvider och felsökning). `LLMCallRecord.request.metadata` saniteras i
  exporten.
* Reparationsloop ägs av `pipeline`: vid underkänt resultat anropas
  `generate(..., feedback=[...], attempt=n)` på nytt.

### rendering (`rendering/`) – ägare: render-export-dev

```
rendering/canonical.py
    CANONICAL_ENCODING = "utf-8"
    def render_document(scenario, document: GeneratedDocument,
                        spec: DocumentSpec) -> RenderedDocument
    def render_package(scenario, package: GeneratedPackage)
        -> dict[str, RenderedDocument]

rendering/formats.py
    def write_document(rendered: RenderedDocument, *,
                       target_without_suffix: Path, fmt: DocumentFormat) -> Path
```

* `canonical_text` är enda källan för teckenoffsets (Unicode-kodpunkter,
  UTF-8). `.txt`-filen ska vara **exakt** `canonical_text`; `.md`, `.docx`,
  `.pdf` härleds från samma strukturerade klausuler.
* Kanonisk text innehåller en metadatahuvud (syntetisk testdata, contract_id,
  dokument-id) följt av titel och numrerade klausuler. Klausultexten får inte
  radbrytas/reflowas inne i klausulen: en klausuls text ska återfinnas ordagrant
  inom sitt spann.
* `clause_spans` beräknas av renderaren (hitta rubrik + klausultext i
  `canonical_text`), aldrig av modellen.
* DOCX via `python-docx`, PDF via `reportlab` (inbyggda Helvetica täcker
  svenska tecken; dokumenten är textbaserade).

### validation (`validation/`) – ägare: gold-dev

```
validation/__init__.py
    def validate_package(scenario: Scenario, package: GeneratedPackage,
                         rendered: dict[str, RenderedDocument], *,
                         gold: GoldSet | None = None) -> PackageVerification

validation/dataset.py
    def validate_dataset(root: Path, *,
                         expect_min_contracts: int = 0) -> DatasetValidationReport
```

Deterministiska kontroller (minst):

1. Varje explicit faktum finns ordagrant i rätt klausul (span-slice).
2. Dokumentstruktur: dokument-id, klausul-id, ordning, unika id:n.
3. Referenser: dokument-/bilagsreferenser pekar på existerande dokument eller
   deklareras i `scenario.missing_documents`.
4. Entiteter, parter och roller finns i texten.
5. Alla evidenspann ligger inom kanonisk text och deras `quote`/slice stämmer.
6. Förväntade antal dokument/klausuler/fakta.
7. Inga oavsiktliga dupliceringar (dubbla definitioner, dubbla klausulrubriker).
8. Avsiktliga anomalies (nivå 5–6) finns där de ska; oavsiktliga motsägelser
   (värde som inte finns i scenariot) fångas av semantiska kontroller.

Semantiska kontroller: värde- och enhetsnormalisering (siffror, svenska
talord, `kvartal`→3 månader, `halvår`→6 månader, `kr`/`SEK`, `%`/`procent`)
kopplat till faktats nyckelord. En LLM-domare får finnas som **rådgivande**
(endast WARN) men aldrig räknas som oberoende facit; standard är deterministisk
verifiering. Osäkra fall → `WARN` + manuell granskning eller kassation.

`validation.dataset` läser ett exporterat dataset via
`export.loader.load_dataset` och verifierar hashar, `.txt` mot kanonisk text,
evidenspann, frågestöd, manifest och räknar om status.

### gold (`gold/`) – ägare: gold-dev

```
gold/__init__.py
    def build_gold_set(scenario: Scenario, package: GeneratedPackage,
                       rendered: dict[str, RenderedDocument]) -> GoldSet
```

* Atomära fakta med normaliserade värden, entiteter/roller, referenser,
  anomalies, exakta evidensspann och `precision`
  (`exact` | `paraphrase` | `clause`).
* Spann hittas med `utils.find_span` i kanonisk text (aldrig påhittade).
* Evidens för parafraser: hitta faktats nyckelord/parafrasform i klausulen;
  annars klausulspannet med `precision="clause"`.
* `question_ids` fylls i av pipeline efter frågegenereringen.

### questions (`questions/`) – ägare: gold-dev

```
questions/__init__.py
    @dataclass
    class QuestionOutcome:
        accepted: list[GoldenQuestion]
        rejected: list[GoldenQuestion]

    def generate_and_verify_questions(scenario: Scenario, gold: GoldSet,
                                      rendered: dict[str, RenderedDocument],
                                      *, max_questions: int | None = None) -> QuestionOutcome
```

* Frågor genereras deterministiskt från scenario + gold (mallar per
  frågekategori och språk). Kategorier: faktaextraktion,
  entitetsidentifiering, relation, jämförelse, villkor, motsägelse, juridiskt
  resonemang, frånvaro av information, otillräcklig information, fullständighet.
* Varje fråga verifieras mot kanonisk text: förväntat svar måste stödjas av
  dokumenten och alla evidenspann måste vara giltiga. Icke-stödda frågor
  flyttas till `rejected` och exporteras inte som godkända.
* Nivå 5–6: `CONFLICTING_EVIDENCE`, `CONDITIONAL`, `INSUFFICIENT_EVIDENCE` med
  korrekt `required_facts` och evidens för de fakta som faktiskt finns.
* Flervärdesfrågor (t.ex. samtliga parter) får `completeness`.

### export (`export/`) – ägare: render-export-dev

```
export/store.py
    def export_contract(result: ContractResult, dataset_root: Path, *,
                        formats: list[DocumentFormat]) -> Path
    def write_rejected_contract(result: ContractResult, dataset_root: Path) -> Path
    def write_dataset_manifest(dataset_root: Path,
                               config: GenerationConfigSnapshot,
                               contracts: list[LoadedContract],
                               stats: DatasetStats) -> Path

export/loader.py
    def load_dataset(dataset_root: Path) -> list[LoadedContract]
    def load_contract_dir(contract_dir: Path) -> LoadedContract
    def find_contract_dirs(dataset_root: Path) -> list[Path]
    def load_dataset_manifest(dataset_root: Path) -> DatasetManifest | None

export/bundle.py
    def export_zip(dataset_root: Path, output_zip: Path) -> Path
    def export_jsonl(dataset_root: Path, output_jsonl: Path) -> Path

export/stats.py
    def compute_stats(contracts: list[LoadedContract], *,
                      total_seconds: float | None = None) -> DatasetStats
```

Datasetlayout per avtal (`contract_0001/`):

```
main_agreement.txt            # exakt kanonisk text
main_agreement.md
main_agreement.docx
main_agreement.pdf
appendix_a.txt|.md|.docx|.pdf # när paketet har bilagor
scenario.json
gold_facts.json
golden_questions.json
verification.json
rendered.json                 # RenderedDocument per dokument (spann + kanonisk text)
llm_calls.json                # saniterade anrop (prompt, usage, latens)
raw/<call_id>.json            # råsvar för felsökning
manifest.json                 # skrivs sist, innehåller hashar för allt ovan
```

Rot: `dataset_manifest.json` (alla avtal, status, hashar, totals) och
`stats.json`. Kassationer skrivs till `rejected/<contract_id>.json` – aldrig
som godkända avtal. ZIP-export innehåller hela trädet + `dataset_manifest.json`
och versionssatt manifest. JSONL: en rad per avtal med scenario, gold, frågor,
manifest-referens.

### pipeline (`pipeline.py`) – ägare: Lead

```
def run_contract(scenario: Scenario, provider: Provider, settings: Settings,
                 *, progress: ProgressCallback = ...) -> ContractResult
def generate_dataset(settings: Settings, request: ScenarioRequest, output: Path,
                     *, progress: ProgressCallback = ...) -> RunOutcome
def regenerate_questions(dataset_root: Path) -> DatasetStats
```

Reparationsloop: generera → rendera → validera. Vid FAIL och `attempt <=
settings.max_repair_attempts`: ny generering med `feedback` (felcheckar som
text). Annars `REJECTED`. Godkända avtal får gold + frågor. `REPAIRED` när
första försöket underkändes men senare försök godkändes.

Kostnad: `pipeline` summerar `LLMCallRecord.usage` och räknar pris endast via
`settings.pricing`. Latens mäts per anrop (provider) och totalt (run).

### cli (`cli/`) – ägare: Lead

```
contractforge generate --type consulting --count 100 --clauses 10 \
    --difficulty 4 --language sv --provider cerebras --output ./dataset
contractforge validate ./dataset
contractforge generate-questions ./dataset
contractforge export ./dataset --format zip [--output out.zip]
contractforge export ./dataset --format jsonl [--output dataset.jsonl]
contractforge stats ./dataset
contractforge list-types
```

* `--json` ger maskinläsbar envelope på stdout; mänsklig text/logg på stderr.
* Exit-koder: 0 ok, 1 internt fel, 2 usage, 3 valideringsfel, 4 providerfel,
  5 konfigurationsfel, 6 scenariofel, 7 genereringsfel, 8 exportfel.
* Okända värden, saknade argument och saknade nycklar ger `ConfigError`/
  `UsageError` – aldrig tyst fallback.

## Kanonisk text – exakt format

Renderaren producerar (UTF-8, `\n` radbrytningar):

```
SYNTETISK TESTDATA – ContractForge
contract_id: contract_0001
document_id: main_agreement
dokumenttyp: main_agreement
språk: sv
jurisdiktion: SE

<Dokumenttitel>

1. <Rubrik klausul 1>
<Klausultext, en eller flera meningar, inga påtvingade radbrytningar>

2. <Rubrik klausul 2>
<Klausultext>
```

(Exakt layout ägs av rendering; reglerna är: markering av syntetisk data i
huvudet, numrerade klausuler, och `clause_spans` som pekar ut rubrik+text.)

## Datakvalitet och säkerhet

* Alla parter, organisationsnummer, adresser och belopp är syntetiska.
  Inga verkliga personuppgifter, inga påståenden om juristgranskning.
* `synthetic: true` i scenario, manifest och dataset-manifest.
* Inga externa dokumentkorpusar i första versionen.
* En seed reproducerar scenario och konfiguration; LLM-text är inte garanterat
  bit-identisk (dokumenteras i manifest/README).

## Testprinciper

* Deterministiska enhetstester med `MockProvider`; inga nätverksanrop i CI.
* `@pytest.mark.live`-tester mot Cerebras hoppas över utan `CEREBRAS_API_KEY`.
* Integrationstest: hela kedjan scenario → generering (mock) → rendering →
  validering → gold → frågor → export → om-läsning → datasetvalidering.
* Alla evidensspann i exporterade avtal måste gå att verifiera mot `.txt`.
