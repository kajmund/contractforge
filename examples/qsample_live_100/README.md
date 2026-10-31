# Bedömning av ContractForge-facit

Det här paketet innehåller 100 slumpade golden-frågor med förväntat
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
  deterministiskt slumpurval (seed 2026) ur 1723 frågor och säger
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
| `source_name` | `cerebras_consulting_100.zip` |
| `source_sha256` | `4344845099a39dbdc7d09c54f6d50cf03e74d1f831c78eda34cb67182893ae7e` |
| `corpus_manifest_sha256` | `7ddd32c544a21f60dafc9a8873e73345bf6e271e2446005ce9a2ae1e561723cf` |
| urvals-seed | `2026` |
| antal begärda (`count`) | `100` |
| `sample_version` | `v1` |

Ett resultat utan dessa värden kan inte hänföras till en bestämd källa. Är
`count` större än antalet frågor i poolen (1723) publiceras hela poolen;
`sample_size` i `manifest.json` blir då 100.

Fält i `manifest.json`: `evidence[].start`/`end` är teckenoffsets i
`document_text` (start inklusiv, end exklusiv) och `evidence_quote` är exakt den
delsträngen. Har en fråga flera spann är de sammanfogade med en rad som bara
innehåller `---`. Urvalsmanifestet skapades 2026-10-31T15:13:35.414810+00:00.
