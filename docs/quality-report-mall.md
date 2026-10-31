# Kvalitetsrapport – `<dataset>`

> **Mall.** Fyll i alla fält; stryk inga rubriker. Rapporten får publiceras som
> `docs/quality-report-<dataset>.md` först när alla obligatoriska fält är ifyllda.
> Saknas datasetets SHA-256, urvalsmetadata eller rater är rapporten ogiltig.
> Protokollet: [docs/quality-protocol.md](quality-protocol.md).

| Fält | Värde |
|------|-------|
| Rapportdatum | `ÅÅÅÅ-MM-DD` |
| Ansvarig granskare | `namn/roll` |
| Dataset (namn) | `t.ex. cerebras_consulting_100` |
| Provider / modell | `cerebras/gpt-oss-120b` \| `mock` \| annan: `…` |
| Avtalstyp / nivå / antal avtal | `consulting` / `2` / `100` |
| ContractForge-version + commit | `0.1.1` / `…` |
| **Status** | `antagen men ej mätt` \| `verifierad för publicerat urval` \| `indikation – grindar ej uppnådda` |

## 1. Dataset och hash

| Fält | Värde |
|------|-------|
| ZIP-filnamn (release-asset) | `…zip` |
| **`source_sha256`** (SHA-256 för ZIP) | `…` |
| **`corpus_manifest_sha256`** (SHA-256 för `dataset_manifest.json` inuti arkivet) | `…` |
| `--corpus-sha256` som angavs vid urvalet | `…` (ska vara lika med `source_sha256`) |
| `contractforge validate` på det uppackade arkivet | `PASS` / `FAIL` – antal avtal kontrollerade: `…` |
| Frågor totalt / giltiga (`validated: true`) | `…` / `…` |
| `generated_at` i dataset-manifestet | `…` |

Verifieringskommandon som faktiskt kördes:

```bash
shasum -a 256 <arkiv>.zip
contractforge validate <uppackad katalog>
```

## 2. Urvalsmetadata

Fälten hämtas ur urvalskatalogens `manifest.json` (schema 1.0).

| Fält | Värde |
|------|-------|
| Kommando som faktiskt kördes | `contractforge publish-questions <zip> --count 100 --seed <seed> --corpus-sha256 <hash> --output <dir>` |
| `--count` | `100` |
| `--seed` | `…` |
| `sample_version` | `v1` |
| `sampling.algorithm` | `…` |
| `sampling.pool_size` | `…` |
| `sampling.sample_size` | `…` |
| Urval: stratifierat / likformigt | `stratifierat` / `likformigt` (grindar endast aggregerat) |
| SHA-256 för `manifest.json` | `…` |
| Blindkopia ratare 1 – SHA-256 | `…` |
| Blindkopia ratare 2 – SHA-256 | `…` |
| `excluded_questions` (antal + orsaker) | `…` |

Blindning tillämpad: `ja / nej` – granskaren har tagit fram två neutralt
märkta blindkopior (protokollet 2.3) utan kategori, svårighetsgrad, `question_id`
eller `contract_id`.

### Urval per stratum

Stratum = `(category, difficulty, answerability)`. Populationen = giltiga frågor
i arkivet; urvalet = frågor i blindposterna. Fyll i en rad per stratum; om
urvalet är likformigt (6.3) redovisas det i kolumnen och grindarna gäller endast
aggregatet.

| Kategori | Svårighetsgrad | Answerability | Population | Urval | Antal bedömda |
|----------|----------------|---------------|-----------:|------:|--------------:|
| `fact_extraction` | `…` | `…` | `…` | `…` | `…` |
| `entity_identification` | `…` | `…` | `…` | `…` | `…` |
| `relation` | `…` | `…` | `…` | `…` | `…` |
| `comparison` | `…` | `…` | `…` | `…` | `…` |
| `conditional` | `…` | `…` | `…` | `…` | `…` |
| `contradiction` | `…` | `…` | `…` | `…` | `…` |
| `legal_reasoning` | `…` | `…` | `…` | `…` | `…` |
| `absence` | `…` | `…` | `…` | `…` | `…` |
| `insufficient_evidence` | `…` | `…` | `…` | `…` | `…` |
| `completeness` | `…` | `…` | `…` | `…` | `…` |
| **Totalt** | | | `…` | `…` | `…` |

Kategorier som saknas i populationen (förklara varför): `…`

Kategorier som finns i populationen men **saknas i urvalet** – redovisas som
`ej bedömd`, inte som 0 % fel: `…`

## 3. Rater

| Roll | Funktion/erfarenhet | Kodade poster | Signerade datum | Blindning intygad |
|------|---------------------|--------------:|-----------------|-------------------|
| Ratare 1 | `…` | `…` | `…` | ja / nej |
| Ratare 2 | `…` | `…` | `…` | ja / nej |
| Tredje ratare (vid oenighet) | `…` | `…` | `…` | ja / nej |

Intyg: raterna har inte sett `scenario.json`, `gold_facts.json`,
`required_facts`, verifieringsresultat, kategori eller svårighetsgrad före
kodningen. `ja` / `nej` – motivera om `nej`.

## 4. Resultat per axel

`osäker` räknas som *inte bevisat korrekt* i felprocenten (se protokollet 4.1).

| Axel | Nämnare `N` | `ja` (k) | `nej` | `osäker` | `f` = fel | Felprocent `p` | Wilson 95 % KI (nedre–övre) |
|------|------------:|---------:|------:|---------:|----------:|---------------:|-----------------------------|
| Q1 ANSWERABLE | `…` | `…` | `…` | `…` | `…` | `…` % | `…` – `…` |
| Q2 ANSWER_CORRECT | `…` | `…` | `…` | `…` | `…` | `…` % | `…` – `…` |
| Q3 EVIDENCE_SUFFICIENT | `…` | `…` | `…` | `…` | `…` | `…` % | `…` – `…` |
| Q4 ANOMALY_HANDLING (`ej_tillamplig` exkluderat) | `…` | `…` | `…` | `–` | `…` | `…` % | `…` – `…` |

Q1 delposter:

| Delpost | N | Andel korrekt klassning | Wilson 95 % KI |
|---------|--:|------------------------:|----------------|
| Facit `ANSWERABLE` ⇒ ratare `ja` | `…` | `…` % | `…` – `…` |
| Facit `INSUFFICIENT_EVIDENCE`/`CONFLICTING_EVIDENCE` ⇒ ratare `nej` | `…` | `…` % | `…` – `…` |
| Facit `CONDITIONAL` ⇒ ratare `ja` (premisser uttryckligen i texten) | `…` | `…` % | `…` – `…` |

## 5. Resultat per frågekategori

Grupper med `N < 10` markeras `otillräckligt underlag`; tröskeln bedöms inte per
grupp.

| Kategori | N (Q2) | Q2 korrekthet | Q2 Wilson 95 % KI | N (Q3) | Q3 tillräcklighet | Q3 Wilson 95 % KI |
|----------|-------:|--------------:|-------------------|-------:|------------------:|-------------------|
| `fact_extraction` | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` |
| `entity_identification` | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` |
| `relation` | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` |
| `comparison` | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` |
| `conditional` | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` |
| `contradiction` | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` |
| `legal_reasoning` | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` |
| `absence` | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` |
| `insufficient_evidence` | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` |
| `completeness` | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` |

## 6. Resultat per svårighetsgrad

| Svårighetsgrad | N (Q2) | Q2 korrekthet | Q2 Wilson 95 % KI | N (Q3) | Q3 tillräcklighet | Q3 Wilson 95 % KI | N (Q4 tillämplig) | Q4 andel `ja` |
|----------------|-------:|--------------:|-------------------|-------:|------------------:|-------------------|------------------:|--------------:|
| 1 | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` | `…` | `…` % |
| 2 | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` | `…` | `…` % |
| 3 | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` | `…` | `…` % |
| 4 | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` | `…` | `…` % |
| 5 | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` | `…` | `…` % |
| 6 | `…` | `…` % | `…` – `…` | `…` | `…` % | `…` – `…` | `…` | `…` % |

## 7. Inter-rater-överensstämmelse – per axel

Överensstämmelse redovisas **per axel** (protokollet 4.3). Ett sammanslaget
kappa får stå **endast som information** och används aldrig som grind.

| Axel | N (kodade av båda) | Eniga | P₀ | pₑ | Cohen's κ | Tolkning | Grind |
|------|-------------------:|------:|---:|---:|---:|----------|-------|
| Q1 ANSWERABLE | `…` | `…` | `…` % | `…` | `…` | låg/måttlig/betydande/fullständig | G5b: ja / nej |
| Q2 ANSWER_CORRECT | `…` | `…` | `…` % | `…` | `…` | … | G5a: ja / nej |
| Q3 EVIDENCE_SUFFICIENT | `…` | `…` | `…` % | `…` | `…` | … | G5c: ja / nej |
| Q4 ANOMALY_HANDLING | `…` | `…` | `…` % | `…` | `…` | … | G5d: ja / nej |
| *(information: alla axlar sammantaget – ej grind)* | `…` | `…` | `…` % | `…` | `…` | … | – |

### 7.1 Oenighetsanalys (obligatorisk)

| Axel | Felavvikelse | Gränsfall | Faktaoenighet | Summa |
|------|-------------:|----------:|--------------:|------:|
| Q1 ANSWERABLE | `…` | `…` | `…` | `…` |
| Q2 ANSWER_CORRECT | `…` | `…` | `…` | `…` |
| Q3 EVIDENCE_SUFFICIENT | `…` | `…` | `…` | `…` |
| Q4 ANOMALY_HANDLING | `…` | `…` | `…` | `…` |

Oenighet avgjord av tredje rataren: `antal` poster.
`oenighet kvar` (alla tre svar olika, räknade som `osäker`): `antal` poster.

Om *felavvikelser* dominerar på Q3 medan Q2-kappan är god: slutsatsen ska vara
att evidensvalet är den svaga länken – **inte** att "facit är korrekt".

## 7.2 Fullständig dokumentläsning (protokollet 3.0)

- Bekräfta att raterna läst **hela dokumentet/paketet** (inte bara citatet): ja / nej
- Antal poster där ett undantag eller en ersättning i **annat dokument** ändrade
  svaret: `…` (poster: `B-…`)
- Av dessa: korrekt fångade av facit `…`, missade av facit `…`

## 8. Felorsaker och åtgärder

Varje post som kodats `nej`/`osäker` på minst en axel har exakt en av de **sju**
orsakerna (protokollet 4.4). Summan ska vara lika med antalet felaktiga poster.

| Orsak | Roll | Antal | Andel av fel | Poster (blind-id) |
|-------|------|------:|-------------:|-------------------|
| `frågeformulering` | mätbarhetsproblem | `…` | `…` % | `B-…` |
| `facitvärde` | facitfel | `…` | `…` % | `B-…` |
| `evidensspann` | facitfel (evidens) | `…` | `…` % | `B-…` |
| `validatorlucka` | systemfel | `…` | `…` % | `B-…` |
| `genereringsfel` | systemfel (generering) | `…` | `…` % | `B-…` |
| `juridisk_tolkning` | mätbarhetsproblem | `…` | `…` % | `B-…` |
| **Totalt** | | `…` | 100 % | |

### 8.1 Åtgärdstabell (orsak × axel)

| Orsak | Q1 | Q2 | Q3 | Q4 | Nästa åtgärd |
|-------|---:|---:|---:|---:|--------------|
| `frågeformulering` | `…` | `…` | `…` | `…` | Frågemallar |
| `facitvärde` | `…` | `…` | `…` | `…` | Gold/scenario |
| `evidensspann` | `…` | `…` | `…` | `…` | Evidensval |
| `validatorlucka` | `…` | `…` | `…` | `…` | Valideraren |
| `genereringsfel` | `…` | `…` | `…` | `…` | Generering + loop |
| `juridisk_tolkning` | `…` | `…` | `…` | `…` | Klassning/fråga |

### 8.1.1 Om en grind fallerar

| Fallerad grind | Orsak som ska dominera | Första åtgärd |
|----------------|------------------------|---------------|
| G1 (Q2 korrekthet) | `facitvärde`, `genereringsfel` | Stoppa användning som bevis; åtgärda gold/generering innan ny mätning. |
| G2 (Q3 tillräcklighet) | `evidensspann` | Facit kan vara korrekt av rätt anledning men underlaget otillräckligt: åtgärda evidensvalet. |
| G3 (Q1 klassning) | `juridisk_tolkning`, `facitvärde` | Review av answerability-klassning. |
| G4 (Q4 undantag) | `facitvärde`, `genereringsfel` | Undantags-/motsägelselogiken. |
| G5a (Q2-överensstämmelse) | – | Raterna är osäkra: förtydliga frågor/protokoll, tredje ratare. |
| G6 (felklassificering) | – | Ofullständig analys, inte kvalitetsresultat. |

Frågor som verktyget uteslutit ur urvalspoolen (ej bedömbara via paketet –
saknar förväntat svar, saknar evidens eller har spann utanför texten):

| `sample_id` | Orsak | Bedömd separat? |
|-------------|-------|-----------------|
| `…` | `…` | ja / nej |

Motbevis för varje `validatorlucka`/`genereringsfel` (vilken regel i
`question_support_problems` eller vilka checks i `validate_package` som inte
fångade felet, och varför):

- `B-…`: `…`

## 8.2 Populationsskattning (om varje stratum representeras)

Urvalsvärdet i avsnitt 5 är standardredovisningen. En **separat** skattning för
hela populationen kräver att varje stratum i populationen har minst en bedömd
post (G7); annars skrivs "ingen populationsskattning – stratum `…` saknas".

| Stratum (kategori/svårighet/answerability) | N_s (population) | n_s (bedömda) | p̂_s | w_s | w_s · p̂_s |
|-------------------------------------------|-----------------:|--------------:|----:|---:|----------:|
| `…` | `…` | `…` | `…` | `…` | `…` |

- p̂_viktat = `…` % (oviktat urvalsvärde: `…` %)
- SE (stratifierat) = `…` %; 95 % KI = `…` – `…`
- Populationsskattningen används **inte** för grindarna (5.4).

## 8.3 Ratare, blanketter och oberoende (protokollet 7.4–7.5)

| Fält | Värde |
|------|-------|
| Ratare A | människa / AI-modell: `…` (namn, version) |
| Ratare B | människa / AI-modell: `…` (namn, version) |
| Tredje ratare | människa / AI-modell: `…` (namn, version) |
| Bedömningens art | **modellbaserad oberoende bedömning** / mänsklig juridisk bedömning |
| Blindkopia A SHA-256 | `…` |
| Blindkopia B SHA-256 | `…` |
| Ordningen olika i A och B | ja / nej (krav: ja) |
| Raterna arbetade isolerat (intygas) | ja / nej |
| P₀/κ beräknat **före** eventuell konsensus | ja / nej (krav: ja) |
| Konsensus/tredje ratare använd endast för postens kvalitet | ja / nej |
| Poster lösta efter inlämning | `…` (varav oeniga kvar: `…`) |

Om någon ratare är en AI-modell gäller: slutsatsen får citeras som
*modellbaserad bedömning*, **aldrig** som mänsklig juridisk granskning eller som
bevis på juridisk korrekthet hos en annan AI-modell. Två modeller kan dela
systematiska fel.

## 9. Grindstatus

| Grind | Krav | Uppmätt | Uppfylld |
|-------|------|---------|----------|
| G1 Q2 korrekthet | punkt ≥ 95 % **och** Wilson nedre ≥ 90 % | `…` % / `…` % | ja / nej |
| G2 Q3 tillräcklighet | punkt ≥ 90 % **och** Wilson nedre ≥ 85 % | `…` % / `…` % | ja / nej |
| G3 Q1 klassning | `ANSWERABLE` ⇒ ja ≥ 95 %; `INSUFFICIENT`/`CONFLICTING` ⇒ nej ≥ 85 % | `…` % / `…` % | ja / nej |
| G4 Q4 anomalihantering | andel ja ≥ 85 % och `N ≥ 10` | `…` % (N = `…`) | ja / nej |
| G5a Q2 inter-rater | procent ≥ 90 % och κ ≥ 0,70 | `…` % / `…` | ja / nej |
| G5b Q1 inter-rater | procent ≥ 80 % och κ ≥ 0,60 | `…` % / `…` | ja / nej |
| G5c Q3 inter-rater | procent ≥ 80 % och κ ≥ 0,60 | `…` % / `…` | ja / nej |
| G5d Q4 inter-rater | procent ≥ 80 % och κ ≥ 0,60 | `…` % / `…` | ja / nej |
| G6 Felorsaker klassificerade | 100 % av felposterna, en av sju orsaker, åtgärdstabell ifylld | `…` % | ja / nej |
| G7 Urvalsintegritet | stratifierat urval; alla stratum ≥ 10 har ≥ 1 post; 200 kodningar | `…` | ja / nej |
| G8 Blindning | blindkopior enligt 2.3, intygat i avsnitt 2 och 3 | ja / nej | ja / nej |
| **Samtliga** | alla ovan | | ja / nej |

## 10. Slutsats

Vad mätningen bevisar (urvalsvärdet; populationsskattningen redovisas separat
och används inte för grindar): `…`

Vad mätningen **inte** bevisar: `…` (t.ex. att facit är juridiskt korrekt utanför
urvalet, att den gäller andra avtalstyper/nivåer, eller – om raterna är modeller
– att den gäller mänsklig juridisk korrekthet).

Vad mätningen **inte** bevisar (urvalets omfattning, ej testade kategorier,
datasetspecifika resultat): `…`

Åtgärder som följer (frågegenerering / validator / scenario / dokumentation):

1. `…`

Status: **`antagen men ej mätt` / `verifierad för publicerat urval` /
`indikation – grindar ej uppnådda`**.

Rapporten får citeras **endast** som: `"… % av det publicerade urvalet (N = 100,
seed …, <source_sha256>) i <dataset>"` – aldrig som ett påstående om hela
korpusens kvalitet.
