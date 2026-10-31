# Designbeslut

Korta, daterade beslut som styr projektet. Ändra genom ny post, inte genom att
skriva om historiken.

## 2026-10-31 – Licens: MIT

ContractForge är ett fristående open source-projekt. MIT valdes för maximal
användbarhet och enkel integration i testverktyg. Se `LICENSE`.

## 2026-10-31 – Ingen databas i första versionen

Datasetet är filer + manifest + SHA-256-hashar. Det räcker för 100–tusentals
dokument, gör exporten självbeskrivande och undviker driftberoenden. Ett API kan
byggas ovanpå samma kärna senare.

## 2026-10-31 – MockProvider är explicit, aldrig fallback

CI, enhetstester och offline-benchmarks behöver en deterministisk provider utan
nätverk. Den väljs uttryckligen med `--provider mock`, rapporteras som
`provider: "mock"` i manifestet och `provider_is_mock: true` i
benchmarkrapporten. Koden väljer aldrig automatiskt en annan provider när den
valda providern failar – då kastas ett fel.

## 2026-10-31 – Inga priser eller prestandaantaganden i koden

Kostnad beräknas endast från en explicit `PricingTable`. Saknas den rapporteras
`cost_usd = null` och `cost_basis = "unknown"`. Inga Cerebras-modellpriser,
latenser eller kapacitetssiffror är hårdkodade.

## 2026-10-31 – LLM genererar klausuler, inte layout

Modellen returnerar strukturerad JSON med exakt de planerade `clause_id`:na.
ContractForge äger numrering, rubriker, metadatahuvud och rendering. Det gör
teckenoffsets deterministiska och förhindrar att modellen lägger till egna
dokument eller påhittade parter.

## 2026-10-31 – Verifiering är deterministisk och oberoende

En LLM-domare får bara ge `WARN` och får aldrig räknas som oberoende facit.
Standardvägen är deterministiska kontroller + deterministisk semantisk
normalisering. Osäkra fall flaggas eller kasseras.

## 2026-10-31 – Kanonisk text är källan för offsets

`.txt` är exakt den kanoniska texten. Alla evidenspann räknas i Unicode-kodpunkter
mot den. DOCX/PDF/MD är härledda representationer och används aldrig för offsets.
Det gör att datasetets `validate`-kommando kan verifiera varje spann mot en fil.

## 2026-10-31 – Promptar är versionshanterade filer

Prompterna ligger i `generation/prompts/<version>/` och både version och SHA-256
sparas i manifestet. Promptändringar kräver ny version så att genererade
dataset kan härledas till rätt instruktioner.

## 2026-10-31 – Syntetiska data som standard

Inga verkliga personuppgifter, inga identifierande organisationsnummer och inga
påståenden om juristgranskning. `synthetic: true` i scenario, manifest och
dataset-manifest. Dokumentens metadatahuvud märker dem som syntetisk testdata
utan att störa klausultexterna.

## 2026-10-31 – Python + argparse + pydantic

Python 3.10+ för ett ekosystem som redan finns i målgruppen. `argparse` ger
exakta exit-koder och maskinläsbara fel utan extra beroende. `pydantic`
validerar den delade domänmodellen och gör korrupta dataset till tidiga,
begripliga fel. DOCX/PDF via `python-docx` och `reportlab` – inga externa
binärer krävs.

## 2026-10-31 – Facit är strukturellt verifierat, inte semantiskt bevisat; oberoende mätning krävs före extern användning som bevis

**Bevisat:** dokument- och klausulstruktur, exakta evidensspann mot kanonisk
text, faktavärden mot scenariot, och att varje fråga stöds av texten enligt den
dokumenterade lexikal/deterministiska regeln `question_support_problems`.
**Inte bevisat:** att en komplex juridisk parafras har bibehållit avsedd
betydelse, att det förväntade svaret är det juridiskt rimliga svaret, och att
evidensspannet är tillräckligt som underlag ur ett läsarperspektiv.

Den deterministiska valideraren kontrollerar att värdet **förekommer** i texten
– den kan inte skilja på formell och semantisk korrekthet. Att återanvända
samma validator eller samma LLM-domare ger ingen oberoende prövning.

Beslut:

1. Facit får citeras som *strukturellt verifierat* direkt, men inte som
   *oberoende bevis* på juridisk eller semantisk korrekthet förrän ett
   oberoende mätprotokoll är genomfört och publicerat.
2. Mätningen styrs av [docs/quality-protocol.md](quality-protocol.md):
   stratifierat urval ur ett fryst ZIP med publicerad SHA-256, två blinda rater
   som endast ser dokumenttext, fråga, förväntat svar och evidenscitat, fyra
   bedömningsaxlar, Wilson 95 % KI, Cohen's kappa och felorsaksklassificering.
3. Exakta grindar innan "oberoende bevis" får användas: Q2 ≥ 95 % korrekthet och
   Wilson-nedre ≥ 90 %, Q3 ≥ 90 % tillräcklighet och Wilson-nedre ≥ 85 %, Q4
   ≥ 85 %, alla felorsaker klassificerade. Rapporten publiceras som
   `docs/quality-report-<dataset>.md` med datasetets SHA-256.
4. Status till dess: **antagen men ej mätt.** Inga förkortade stickprov får
   ersätta protokollet.
5. Mätningen gäller endast det publicerade urvalet – aldrig hela korpusen.
   v0.1.1 ligger fryst som referens och ändras inte för att underlätta en
   mätning.

Samma princip som i `verification.md`: skilj på vad systemet faktiskt har
bevisat och vad vi ännu antar. Ett påstående får bara citeras tillsammans med
det bevis som bär det.
