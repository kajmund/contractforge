# Protokoll: oberoende mätning av facitens felprocent

Detta dokument definierar hur ContractForge mäter **facitens eget felprocent** –
oberoende av ContractForges deterministiska validator. Protokollet är normerande:
en mätning som inte följer det får inte citeras som oberoende bevis (se [Grindar](#5-grindar)).

Status: **antagen men ej mätt.** Ingen oberoende mätning har genomförts per
2026-10-31. Fram till dess gäller formuleringen i
[docs/decisions.md](decisions.md) post *2026-10-31 – Facit är strukturellt
verifierat, inte semantiskt bevisat; oberoende mätning krävs före extern
användning som bevis*.

---

## 1. Syfte

ContractForge bevisar en mängd saker om varje exporterat facit – och antar en
mängd andra. Protokollet finns för att göra den gränsen explicit, mätbar och
citerbar, så att ett påstående om facitens kvalitet alltid kan hänföras till
**antingen** en dokumenterad deterministisk check **eller** en publicerad
oberoende mätning.

Den centrala distinktionen:

| Påstående | Bevisat av | Status |
|-----------|-----------|--------|
| Dokument- och klausulstruktur, ordning, unika id:n | `structure`, `counts` | **Bevisat** i varje dataset |
| Evidensspann slice:ar exakt den text facit påstår | `validate`, spannkontroll mot kanonisk text | **Bevisat** i varje dataset |
| Faktavärden motsvarar scenariot | `explicit_fact_present`, `semantic_fact_supported`, datasetvalideringens scenario-jämförelse | **Bevisat** mot scenariot |
| Frågor stöds av texten enligt dokumenterad lexikal/deterministisk regel | `question_support_problems` (en delad implementation för generator och validator) | **Bevisat** enligt regeln |
| Avsiktliga motsägelser/undantag finns där scenariot säger | `intentional_anomaly_present`, `supersession`, `missing_information` | **Bevisat** i varje dataset |
| Att en komplex juridisk parafras har **bibehållit avsedd betydelse** | — | **Antaget, ej bevisat** |
| Att det förväntade svaret är det **juridiskt rimliga** svaret på frågan | — | **Antaget, ej bevisat** |
| Att evidensspannet är **tillräckligt** som underlag ur ett läsarperspektiv | — | **Antaget, ej bevisat** |
| Att `CONDITIONAL`/`CONFLICTING_EVIDENCE`/`INSUFFICIENT_EVIDENCE` är rätt klassning i en juridisk läsning | — | **Antaget, ej bevisat** |

Varför den deterministiska valideraren **inte ensam** räcker för raderna markerade
*antaget*: `question_support_problems` prövar om det förväntade värdet
**förekommer** i evidensens text (eller täcks av ett gold-span), inte om
frågan betyder det valideraren tror att den betyder. En parafras som lexikalt
innehåller rätt ord men juridiskt betyder något annat passerar därför. Att köra
samma kod – eller samma LLM som skrev texten – en gång till gör inte mätningen
oberoende.

Mekaniken bakom de bevisade raderna beskrivs i [docs/verification.md](verification.md)
(kontroller, statusar, LLM-domarens begränsning till `WARN`).

## 2. Design

### 2.1 Material: det frysta datasetets ZIP

Mätningen körs **alltid** mot ett publicerat, fryst datasetarkiv (ZIP) med
publicerad SHA-256 – inte mot en arbetskatalog och inte mot ett dataset som
genereras i mätögonblicket. Kandidatarkiv:

| Källa | ZIP | SHA-256 | Giltiga frågor (population) |
|-------|-----|---------|-----------------------------|
| Cerebras `gpt-oss-120b`, 100 avtal, nivå 2, seed 4242 (v0.2.0) | `cerebras_consulting_100.zip` (release-asset) | `4344845099a39dbdc7d09c54f6d50cf03e74d1f831c78eda34cb67182893ae7e` | 1 723 |
| Offline mock, 100 avtal, nivå 3, seed 1234 | återskapas deterministiskt (se [benchmarks.md](benchmarks.md)) – ZIP exporteras med `contractforge export` | beräknas vid export, publiceras i rapporten | 2 300 |

Det första datasetet är det enda som i dag är fryst som release-asset med
publicerad hash. En mätning som startar med mock-datasetet ska publicera sitt
arkiv (ZIP + SHA-256) tillsammans med rapporten – annars är urvalet inte bundet
till en artefakt (se [Reproducerbarhet](#6-reproducerbarhet)).

Se [docs/benchmarks.md](benchmarks.md) för körningarnas konfiguration och
[docs/reproducibility.md](reproducibility.md) för hur ett publicerat arkiv
verifieras. Ett **mock**-dataset får mätas med detta protokoll (det mäter facit,
inte modellen), men resultatet får aldrig citeras som utsaga om LLM-text.

### 2.2 Stratifierat urval av golden-frågor

Urvalet dras ur `golden_questions.json` i det uppackade arkivet, men **endast
från frågor som arkivet självt anger som giltiga facit** (`validated: true`).
Frågor som frågeverifieringen kasserat exporteras aldrig och ingår aldrig i
urvalet – de mäter generatorn, inte facit.

Makrot läses ur frågornas egna fält: `category` (`QuestionCategory`,
10 värden), `difficulty` (1–6), `answerability`
(`ANSWERABLE`, `CONDITIONAL`, `CONFLICTING_EVIDENCE`, `INSUFFICIENT_EVIDENCE`)
och `contract_id` (från katalogen). Stratum definieras som
`(category, difficulty, answerability)`; `contract_id` används för att sprida
urvalet över avtal.

Regler:

1. **Kvoter per stratum.** Urvalet `n` (standard 100) fördelas proportionellt mot
   stratumets andel av den giltiga frågepopulationen. Varje stratum som finns i
   populationen får minst `min(3, antal i stratumet)` frågor; överskjutande platser
   fördelas proportionellt om.
2. **Oberoende inom stratum.** Varje stratum dras med en egen deterministisk RNG
   (`random.Random(sha256("<dataset_sha256>:<seed>:<stratum-nyckel>"))`), utan
   återläggning. Därför ger samma `(dataset, seed)` alltid samma urval, och urvalet
   är inte känsligt för filordning eller Python-versionens globbning.
3. **Max en fråga per avtal och kategori** i första hand (täcker flest avtal);
   regeln släpps bara om den gör en kvot ouppfylld.
4. **Populationen dokumenteras.** Alla giltiga frågor räknas per stratum och
   skrivs i urvalsmanifeset, så att läsaren kan se vilka kategorier som saknas i
   arkivet (t.ex. `legal_reasoning`, som bara genereras för relationspar).

### 2.3 Vad raterna ser – och inte ser

För varje fråga i urvalet får rataren en **blindpost** som innehåller exakt:

1. dokumenttexten (kanonisk text, `.txt`, hela avtalets dokumentpaket),
2. frågetexten,
3. det förväntade svaret (`expected_answer` med `value`, `values`, `unit`,
   `currency`, `text`, `conditions` och, när den finns, `completeness`),
4. evidenscitatet (`evidence[].quote` med dokument- och klausul-id).

Blindposten innehåller **inte**: `scenario.json`, `gold_facts.json`,
`required_facts`, de interna `conditions`-koderna (`anomaly:<id>`,
`missing:<doc>`), verifieringsresultat, `verification.json`, facits `precision`
eller frågegeneratorns mall. Verktyget publicerar en defensiv kontroll
(`FORBIDDEN_QUESTION_FIELDS`) som avbryter om ett scenariofält skulle läcka in i
en publicerad fråga. För `CONDITIONAL`- och `INSUFFICIENT_EVIDENCE`-frågor
publiceras de mänskligt läsbara villkorens **antal och text**, aldrig de interna
id:na.

Regeln är att rataren ska bedöma **svaret utifrån texten**, inte utifrån
systemets påståenden om varför svaret är rätt. Ett evidenscitat som ligger
utanför själva svaret får läsas i sitt klaussammanhang. Raterna får inte veta
vilket dataset, vilken kategori, vilken svårighetsgrad eller vilket
`question_id` posten har.

**Verktyget blindar.** `publish-questions` skriver en `ratings.csv` vars kolumner
är `sample_id`, `question`, `expected_answer`, `evidence_quote` samt de tomma
bedömningskolumnerna `Q1_answerable`, `Q2_answer_correct`,
`Q3_evidence_sufficient`, `Q4_anomaly_handling`, `comment`. Rataren ser alltså
inget `contract_id`, ingen `question_id`, ingen kategori och ingen
svårighetsgrad. `sample_id` är en opak hash
(`cfq-` + `sha256("<contract_id>:<question_id>")[:12]`) som mappas tillbaka till
källfrågan i `selection.json` (koordinatorns fil, delas inte med raterna).

Har frågan flera evidensspann visas de i `evidence_quote` åtskilda av en rad med
`---`; varje del är en exakt avskrift ur dokumentet och finns ordagrant i
`manifest.json` → `evidence_quotes`. Varje avskrift verifieras maskinellt mot
dokumentets kanoniska text vid publicering.

Kvar för granskaren: göra en **ratarspecifik omblandning** (radordningen i
`ratings.csv` följer urvalets avtalsordning) och skriva de två
blindkopiornas SHA-256 i rapporten. Ett paket som delas utan den omblandningen
uppfyller inte grind G8.

## 3. De fyra bedömningsaxlarna

Varje ratare besvarar fyra frågor per blindpost. Alla svar kryssas i ett fast
alternativ; fritext i `kommentar` ändrar aldrig en kod utan förklarar den.

### 3.0 Rataren ska läsa HELA dokumentet – inte bara evidenscitatet

Evidenscitatet är en **ingång**, inte svaret på om frågan är besvarbar eller om
svaret är korrekt. En annan klausul kan innehålla ett undantag, en bestämmelse om
att ett tidigare villkor "ersätts" eller en motsägelse som ändrar vad som gäller.

Därför gäller:

1. Rataren **ska** läsa det **fullständiga dokumentet** i blindposten
   (`document_text`, hela avtalets kanoniska text) innan Q1–Q4 kodas – inte bara
   det angivna citatet.
2. För dokumentpaket ska **alla dokument i paketet** läsas: huvudavtal, bilagor
   och tillägg. Ett tillägg som ersätter ett villkor i huvudavtalet är en del av
   texten, inte en referens rataren får bortse från.
3. Undantag, villkor och senare ändringar som står **i dokumenten** räknas som
   en del av underlaget. Rataren får **inte** använda information utanför
   dokumenten (lagtext, egna antaganden om partsavsikter, scenariot) – men får
   inte heller **bortse** från text som finns i dokumenten.
4. Om svaret beror på ett undantag eller en ersättning som rataren hittar i ett
   annat dokument än citatet: koda Q2 = `nej` när facit svarar med det
   överspelade värdet, och ange i `kommentar` vilket dokument/klausul som avgjorde
   (`kommentar` krävs för Q2 = `nej` när orsaken är en annan klausul).
5. `osäker` är förbehållet fall där rataren *inte kan avgöra* – inte för fall där
   rataren inte har läst klart dokumentet.

Verktyget publicerar `document_text` per dokument i `manifest.json` och
`document_texts` (alla dokument i paketet) på blindposten, så att rataren alltid
har hela texten tillgänglig (se 6.1).

### Q1 – ANSWERABLE: kan frågan besvaras entydigt med dokumenttexten?

> **Fråga till rataren:** Kan frågan besvaras entydigt med *enbart* dokumenttexten
> (utan information utanför dokumenten, utan antaganden om vad parterna "menar")?

| Kod | Betydelse |
|-----|-----------|
| `ja` | Dokumenten ger ett entydigt svar på frågan. |
| `nej` | Dokumenten ger inget entydigt svar: informationen saknas, eller flera värden motsäger varandra utan att dokumenten avgör vilket som gäller. |
| `osäker` | Rataren kan inte avgöra om texten räcker utan juridisk tolkning utanför dokumenten. |

Jämförelse: ratarens `ja` ↔ facit `ANSWERABLE`; `nej` ↔ `INSUFFICIENT_EVIDENCE`
eller `CONFLICTING_EVIDENCE`; `CONDITIONAL` räknas som *svar under villkor* och
godtas som `ja` endast om villkorets premisser uttryckligen står i texten.

### Q2 – ANSWER_CORRECT: är det förväntade svaret korrekt?

> **Fråga till rataren:** Givet dokumenttexten – är det förväntade svaret det
> svar som en noggrann jurist skulle acceptera på frågan?

| Kod | Betydelse |
|-----|-----------|
| `ja` | Värdet/värdena (och enheten) är korrekt, fullständigt och avsett enligt texten. |
| `nej` | Svaret är felaktigt, ofullständigt, avser fel part/fel klausul eller har fel enhet/villkor. |
| `osäker` | Rataren kan inte avgöra utan juridisk tolkning utanför dokumenten, eller frågan är så otydlig att flera svar är försvarbara. |

`osäker` är ett giltigt svar och räknas **inte** som korrekt i trösklarna (se
[Grindar](#5-grindar)). Det är inte samma sak som ett fel.

### Q3 – EVIDENCE_SUFFICIENT: räcker evidenscitatet som underlag?

> **Fråga till rataren:** Räcker det/de angivna evidenscitaten – lästa i sin
> klausul – för att motivera det förväntade svaret för en läsare som inte har
> tillgång till scenariot?

| Kod | Betydelse |
|-----|-----------|
| `ja` | Citaten pekar på den text som avgör svaret; en läsare kan kontrollera svaret direkt. |
| `nej` | Citaten saknar det avgörande värdet, pekar på fel ställe, eller kräver scenariot/scenarioinformation för att förstås. |
| `osäker` | Citaten är gränsfall (t.ex. ett helt klausulcitat där ett ord är relevant, eller ett citat som är tekniskt rätt men juridiskt tvetydigt). |

Q3 mäter **tillräcklighet**, inte spannets exakthet. Att spannet slice:ar exakt
rätt tecken är redan bevisat (se [Syfte](#1-syfte)); Q3 frågar om den valda
texten är det rätta underlaget. Facits `precision`
(`exact`/`paraphrase`/`clause`) får **inte** visas för rataren.

### Q4 – ANOMALY_HANDLING: hanteras frågans avvikelse korrekt?

> **Fråga till rataren:** När frågan rör en anomali – motstridiga villkor, ett
> undantag, ett tillägg som ersätter ett villkor, en saknad bilaga eller en
> omständighet utanför dokumenten – hanterar facit anomaliens *rättsliga*
> innebörd korrekt?

| Kod | Betydelse |
|-----|-----------|
| `ja` | Facit skiljer korrekt på avsiktlig och oavsiktlig motsägelse, identifierar rätt styrande villkor/undantag, och klassningen (`CONFLICTING_EVIDENCE`, `CONDITIONAL`, `INSUFFICIENT_EVIDENCE`) stämmer med texten. |
| `nej` | Facit missar eller feltolkar anomalin (t.ex. väljer det ersatta villkoret, ignorerar ett undantag, eller svarar `ANSWERABLE` på en fråga som bara kan besvaras villkorat). |
| `ej_tillamplig` | Frågan rör ingen anomali enligt texten (ingen motstridighet, inget undantag, ingen saknad källa). |

`ej_tillamplig` räknas **inte** i Q4:s nämnare. Q4 bedöms av rataren på texten
och evidensen – rataren vet inte vilka anomalies scenariot deklarerat, och ska
inte heller veta det.

## 4. Analys

All statistik beräknas per axel, per frågekategori och per svårighetsgrad.
Beräkningen sker på de **kodade** svaren från båda raterna; där de är oense
avgör tredje rataren (se [Omfattning](#8-omfattning-och-begränsningar)).

### 4.1 Definitioner

För ett kodat svar `ja`/`nej`/`osäker` (Q4: `ja`/`nej`/`ej_tillamplig`):

- nämnare `N` = alla bedömda poster där axeln är tillämplig,
- `k` = antal `ja`,
- `f` = `N − k` = antal `nej` + antal `osäker` på Q1–Q3 (på Q4 = antal `nej`),
- felprocent `p = f / N`.

`osäker` räknas alltså som *inte bevisat korrekt* – det gör trösklarna
konservativa och förhindrar att otydlighet döljs som kvalitet. Rapporten ska
ändå redovisa `osäker` separat från `nej` per axel:

| Axel | Kodat som fel | Redovisas separat |
|------|---------------|-------------------|
| Q1 ANSWERABLE | `nej`, `osäker` | `nej` och `osäker` |
| Q2 ANSWER_CORRECT | `nej`, `osäker` | `nej` och `osäker` |
| Q3 EVIDENCE_SUFFICIENT | `nej`, `osäker` | `nej` och `osäker` |
| Q4 ANOMALY_HANDLING | `nej` | `ej_tillamplig` exkluderas ur `N`; saknar `osäker`-kod – tvekan kodas `ja` endast om texten uttryckligen stöder klassningen, annars `nej` med kommentar |

Rapporten redovisar för varje axel: `N`, `k`, `osäker`, `nej`, `f`, `felprocent p`
och 95 % KI.

### 4.2 Wilson 95 %-konfidensintervall

Ett Wald-intervall är olämpligt när andelen ligger nära 100 % (som förväntas
här) – det kan ge övre gränser över 1,0 och döljer skevheten. Protokollet använder
därför **Wilson score-intervallet** med `z = 1,959964` (95 %):

```
n     = N                         (alla tillämpliga poster i gruppen)
p̂     = k / n                     (andelen "ja")
z     = 1,959964
c     = z² / n
       p̂ + c/2 ± z·sqrt( p̂(1−p̂)/n + c/(4n) )
KI95  = ────────────────────────────────────────
                     1 + c
nedre = ( p̂ + c/2 − z·sqrt( p̂(1−p̂)/n + c/(4n) ) ) / (1 + c)
övre  = ( p̂ + c/2 + z·sqrt( p̂(1−p̂)/n + c/(4n) ) ) / (1 + c)
```

Specialfall: för `p̂ = 1,0` är `p̂(1−p̂) = 0` och intervallet reduceras till
`n/(n+z²) ± z·sqrt(c/(4n))/(1+c)`; det är endast giltigt när `n > 0`.
Rapporten anger alltid **nedre gräns** tillsammans med punktskattningen – det är
nedre gränsen som avgör om tröskeln är uppnådd, aldrig punktskattningen ensam.

För grupper med `n < 10` rapporteras `N`, `k` och `osäker` men KI markeras
`otillräckligt underlag` och tröskeln bedöms inte per grupp – enbart på den
aggregerade nivån. Gränsen 10 följer tumregeln att ett Wilson-intervall är
meningsfullt först när nämnaren ger flera förväntade fel.

**Räkneexempel** (kontrollvärden för rapportskriptet, med formeln ovan):

| `n` | `k` | `p̂` | Wilson 95 % KI |
|----:|----:|----:|----------------|
| 100 | 95 | 95,0 % | 88,8 % – 97,9 % |
| 100 | 98 | 98,0 % | 93,0 % – 99,5 % |
| 100 | 90 | 90,0 % | 82,6 % – 94,5 % |
| 98 | 96 | 98,0 % | 92,9 % – 99,4 % |
| 200 | 192 | 96,0 % | 92,3 % – 98,0 % |

Exemplet illustrerar varför tröskeln har **både** ett punkt- och ett
nedre-gräns-villkor: `n = 100`, `k = 95` uppnår punktgränsen 95 % men **inte**
G1:s nedre gräns 90 % (88,8 %). Urvalets storlek sätter alltså ett tak för vad
som kan påstås. Kravet G1 (punkt ≥ 95 % **och** Wilson-nedre ≥ 90 %) kräver
följande antal `ja` enligt samma formel:

| `n` | Minsta `k` för G1 | Andel | `k = n` ger |
|----:|------------------:|------:|------------:|
| 34 | — (omöjligt) | — | 89,9 % |
| 35 | 35 | 100 % | 90,1 % |
| 98 | 95 | 96,9 % | 96,2 % |
| 100 | 96 | 96,0 % | 96,3 % |
| 150 | 143 | 95,3 % | 97,5 % |
| 200 | 190 | 95,0 % | 98,1 % |

Även med **noll fel** understiger nedre gränsen 90 % vid `n ≤ 34`; ett så litet
urval kan alltså inte uppfylla G1 oavsett resultat. Det är skälet till att
`--count 100` är ett krav och inte en rekommendation: vid `n = 100` räcker det
med fyra fel för att G1 ska missas.

### 4.3 Inter-rater-överensstämmelse – per axel

Oenighet mellan två rater behandlas **separat från kvalitetsmåttet**. Två rater
kan vara eniga om att svaret är korrekt (Q2 = `ja`) men oense om evidensen räcker
(Q3 = `ja`/`nej`) – det är en oenighet om *mätbarheten*, inte ett faktum om
kvaliteten. Ett enda sammanslaget kappa över alla fyra axlarna döljer både
systematisk oenighet på en axel och att axlarna mäter olika saker.

**Krav: överensstämmelse beräknas och rapporteras per axel.**

För varje axel `a ∈ {Q1, Q2, Q3, Q4}` och varje stratum `g` (kategori,
svårighetsgrad, totalt) beräknas:

```
P₀(a,g) = (antal poster där raterna valt samma kod) / (antal poster kodade av båda)
pₑ(a,g) = Σ_c (n₁c · n₂c) / N²          (marginalerna per kod c på axeln)
κ(a,g)  = (P₀(a,g) − pₑ(a,g)) / (1 − pₑ(a,g))
```

`ej_tillamplig` på Q4 utesluts ur parvisa beräkningar där raterna inte båda
angett axeln som tillämplig; `n` anges alltid. För grupper med `N < 10`
rapporteras `P₀`, `κ` och `n`, men KI/tolkning markeras `otillräckligt underlag`.

**Rapporten ska innehålla följande tabell (samtliga axlar, ej sammanslagna):**

| Axel | N | Enighet P₀ | κ | Tolkning (Landis & Koch) |
|------|--:|-----------:|--:|--------------------------|
| Q1 ANSWERABLE | … | … | … | … |
| Q2 ANSWER_CORRECT | … | … | … | … |
| Q3 EVIDENCE_SUFFICIENT | … | … | … | … |
| Q4 ANOMALY_HANDLING | … | … | … | … |

Avvikelsegrindarna (G1–G5) bedöms **per axel**:

- **G5a** gäller Q2 (axeln som avgör om facit får citeras som oberoende bevis):
  `P₀ ≥ 90 %` och `κ ≥ 0,70`.
- **G5b–G5d** gäller Q1, Q3 respektive Q4 var för sig: `κ ≥ 0,60` och
  `P₀ ≥ 80 %`. Uppfylls inte en axels tröskel ska rapporten **inte** sammanfatta
  mätningen med ett gemensamt "kappa" utan med vilken axel som är otillräckligt
  kalibrerad.
- Ett **gemensamt kappa över alla fyra axlarna får redovisas endast som
  information** (`κ_allas`), aldrig som grind eller som underlag för slutsats om
  enskilda axlar. Koderna betyder olika saker per axel, så det gemensamma värdet
  har ingen självständig betydelse.

**Oenighetsanalys (obligatorisk i rapporten).** Varje oenig post klassas i exakt
en av tre typer, räknat per axel:

| Typ | Definition | Åtgärd |
|-----|------------|--------|
| **felavvikelse** | Raterna är eniga om de andra axlarna men oense på denna (t.ex. Q2 = `ja`/`ja`, Q3 = `ja`/`nej`). | Facit *kan* vara korrekt men underlaget är otillräckligt: åtgärdas i frågegeneratorn/evidensvalet, inte i facitvärdet. |
| **gränsfall** | Ena rataren har kodat `osäker` och den andra en fast kod. | Redovisas separat; räknas som `osäker` i felprocenten enligt 4.1 och kräver tredje ratare. |
| **faktaoenighet** | Raterna har olika fast kod på samma axel (`ja`/`nej`). | Tredje rataren avgör; posten klassas i en felorsak (4.4) och felprocenten räknas på avgörandet. |

Rapporten redovisar antal och andel per typ och axel. En mätning där
*felavvikelser* dominerar på Q3 men Q2-kappan är god får **inte** sammanfattas
som "facit är korrekt" – slutsatsen ska då vara att evidensvalet är den svaga
länken.

### 4.4 Felorsaksklassificering

Varje post som någon av raterna (eventuellt efter tredje rataren) kodat som
`nej`/`osäker` på minst en axel får **exakt en** orsak. Orsaken sätts av
Tredje-rataren/granskaren, inte av förstaraterna, och får aldrig väljas på
grundval av vad som är lätt att åtgärda:

| Orsak | Kännetecken | Klassas på |
|-------|-------------|------------|
| `frågeformulering` | Frågan är tvetydig, öppen, ledande eller dubbeltolkbar. Svaret kan vara rätt, men frågan gör det svårt att avgöra vad som efterfrågas. | Endast när svaret och evidensen i sig är korrekta. |
| `facitvärde` | Det förväntade värdet är fel, ofullständigt, har fel enhet eller avser fel part/villkor; eller klassningen (`CONDITIONAL`/`CONFLICTING_EVIDENCE`/`INSUFFICIENT_EVIDENCE`) är fel. | Q1/Q2. |
| `evidensspann` | Spannets valda text räcker inte som underlag (fel klausul, saknar avgörande värde, kräver scenariot), medan värdet i sig är rätt. | Q3. |
| `validatorlucka` | Frågan passerar `question_support_problems` men bedöms fel/otillräcklig av raterna, och orsaken är **inte** `frågeformulering`, `facitvärde` eller `evidensspann`. Regeln fångar då inte den här typen av fel. | Kräver att de tre övriga orsakerna uttryckligen uteslutits. En `validatorlucka` är alltid underlag för en åtgärd i validatorn. |
| `genereringsfel` | Dokumenttexten själv är felaktig eller tillhör inte scenariot: fel belopp/part/datum skrivet (inte bara felaktigt återgivet i facit), text som motsäger sig själv utan avsiktlig anomali, eller en klausul som saknas. | Endast när raterna kan peka på en diskrepans **i dokumentet** (t.ex. mot en annan klausul), inte mot facit. Åtgärd: generering/validering. |
| `juridisk_tolkning` | Frågan kräver en juridisk bedömning som går utöver vad dokumenten entydigt säger: ett villkor är tolkningsbart, två giltiga regler kan samverka på olika sätt, eller svaret beror på rättstillämpning utanför texten. | Får användas endast när texten är korrekt och facit inte självklart följer av den. Räknas **inte** som ett facitfel i Q2-statistiken men redovisas separat; åtgärd: frågan bör klassas som `CONDITIONAL`/`INSUFFICIENT_EVIDENCE` eller formuleras om. |

De sju orsakerna mappar till varsin typ av åtgärd och **styr vilket nästa steg
som väljs om en grind fallerar**:

| Orsak | Åtgärd (nästa steg) |
|-------|---------------------|
| `frågeformulering` | Frågemallarna i `questions/` (tydlighet, vad som efterfrågas). |
| `facitvärde` | Gold-byggaren/scenariofakta (`gold/`, `scenarios/`) – facit måste säga samma som dokumentet. |
| `evidensspann` | Evidensvalslogiken (`gold/` spannval, `questions/` evidenssamling). |
| `validatorlucka` | Valideraren (`validation/`) – en regel saknas och ska namnges. |
| `genereringsfel` | Generering/mock/scenario + validering (reparationsloopen fångade inte felet). |
| `juridisk_tolkning` | Klassning/answerability i scenariot och frågegeneratorn. |

En post där tredje rataren dömer svaret korrekt får ingen orsak – den är inte ett
fel, bara en oenighet. Kvarvarande `validatorluckor` är det som protokollet är
till för att hitta: fel som den deterministiska regeln inte ser, och som därför
inte fångas av någon befintlig check.

Rapporten redovisar en **åtgärdstabell** (antal per orsak × axel) så att en
fallerad grind direkt pekar på rätt komponent. Roller:
`facitvärde`/`evidensspann` = facitfel, `frågeformulering`/`juridisk_tolkning` =
mätbarhetsproblem, `genereringsfel`/`validatorlucka` = systemfel.

## 5. Grindar

### 5.1 Status i dag

**Antagen men ej mätt.** Till dess att en publicerad rapport finns:

- facit får citeras som *strukturellt verifierat* (bevisat, se [Syfte](#1-syfte)),
- facit får **inte** citeras som *oberoende bevis* på juridisk eller semantisk
  korrekthet,
- inga förkortade eller enskilda stickprov får ersätta protokollet ("vi tittade
  på 20 frågor" är inte en mätning),
- en mätning med det nuvarande verktygets likformiga urval (6.3) är inte
  protokollenlig: den får publiceras som **indikation**, aldrig som
  "oberoende bevis".

### 5.2 Exakta trösklar för att facit får citeras som "oberoende bevis"

En mätning får citeras som oberoende bevis **endast** när **alla** följande
villkor är uppfyllda för det publicerade urvalet:

| # | Villkor | Tröskel |
|---|---------|---------|
| G1 | Q2 ANSWER_CORRECT, korrekthet (andel `ja`) | punktestimat **≥ 95 %** **och** Wilson 95 % KI nedre gräns **≥ 90 %** |
| G2 | Q3 EVIDENCE_SUFFICIENT, tillräcklighet (andel `ja`) | punktestimat **≥ 90 %** **och** Wilson 95 % KI nedre gräns **≥ 85 %** |
| G3 | Q1 ANSWERABLE, klassning | andel `ja` där facit säger `ANSWERABLE` **≥ 95 %**; andel `nej` där facit säger `INSUFFICIENT_EVIDENCE`/`CONFLICTING_EVIDENCE` **≥ 85 %** |
| G4 | Q4 ANOMALY_HANDLING (poster där axeln är tillämplig) | andel `ja` **≥ 85 %**, och `N ≥ 10` |
| G5a | Inter-rater-överensstämmelse på **Q2** (axeln bakom G1) | procent **≥ 90 %**; Cohen's κ **≥ 0,70** |
| G5b | Inter-rater-överensstämmelse på **Q1** | procent **≥ 80 %**; Cohen's κ **≥ 0,60** |
| G5c | Inter-rater-överensstämmelse på **Q3** | procent **≥ 80 %**; Cohen's κ **≥ 0,60** |
| G5d | Inter-rater-överensstämmelse på **Q4** (tillämpliga poster) | procent **≥ 80 %**; Cohen's κ **≥ 0,60** |
| G6 | Felorsaker | **100 %** av alla poster kodade `nej`/`osäker` har exakt en av de **sju** orsakerna (4.4), varje `validatorlucka`/`genereringsfel` har ett namngivet motbevis (vilken regel som inte fångade felet), och rapporten innehåller åtgärdstabellen orsak × axel |
| G7 | Urvalsintegritet | varje stratum med ≥ 10 giltiga frågor i populationen har ≥ 1 bedömd post; alla blindposter (100 frågor × 2 rater = 200 kodningar) är kodade |
| G8 | Blindning | blindkopian innehåller inte scenario, gold, `precision`, kategori eller svårighetsgrad, och blindningen intygas i rapporten |

Grindarna gäller **urvalet**, inte hela korpusen (se
[Omfattning](#8-omfattning-och-begränsningar)). Ett eventuellt
populationsviktat tal (5.4) får **inte** användas för att kryssa i en grind.
Överensstämmelse bedöms **per axel**; ett sammanslaget kappa över alla fyra
axlarna redovisas som information men är aldrig en grind (4.3).
Om en tröskel missas får facit citeras med mätningen som **indikation**, aldrig
som oberoende bevis; rapportens slutsats ska då ange exakta avvikelser och
vilken åtgärd (frågegenerering, validator, scenario) som följer.

### 5.4 Populationsviktning – urvalet får inte blandas ihop med hela korpusen

Urvalet är **stratifierat**, inte ett likformigt stickprov. Ett obearbetat
felprocent över urvalet beskriver därför **urvalet**, inte de 1 723 giltiga
frågorna. Vill man uppskatta felprocenten i hela populationen måste urvalet
viktas om med de anatomiska vikterna `w_s = N_s / N` (stratumets andel av
populationen):

```
w_s       = N_s / N              (stratumets andel av populationen)
p̂_s       = k_s / n_s            (andel "ja" bland bedömda poster i stratum s)
p̂_viktat  = Σ_s w_s · p̂_s
```

Regler:

1. **Standardredovisningen är den oviktade urvalsfelprocenten** med Wilson-KI.
   Den är exakt för urvalet och den får citeras som
   *"X % av det publicerade urvalet"*.
2. **Populationsskattningen kräver vikterna** och redovisas som ett **separat**
   tal, märkt `skattning för populationen`, med stratumvikterna i rapporten.
   Den kräver att **varje stratum i populationen är representerat** i urvalet
   (G7). Saknas ett stratum får ingen populationsskattning göras – då rapporteras
   endast urvalsvärdet och bristen namnges.
3. Populationsskattningen har **större osäkerhet** än urvalsvärdet eftersom den
   viktar små strata tungt: standardfel med stratifierat urval
   `SE = sqrt(Σ_s w_s² · p̂_s(1−p̂_s)/n_s)`, och 95 %-KI rapporteras som
   `p̂_viktat ± 1,959964 · SE` (för intervall i procent används Wilson på den
   oviktade basen; båda redovisas om de skiljer sig).
4. **Slutsatsen måste ange vilket tal som avses.** Grindarna G1–G4 bedöms på
   **urvalsvärdet** (det frysta urvalet är mätprotokollets enhet); en
   populationsskattning får aldrig användas för att kryssa i en grind.
5. Ett nytt eller ändrat urval kräver **ny rapport** med egen
   `selection_sha256`. Ett viktat tal från ett urval får inte tillämpas på ett
   annat.

### 5.3 Publicerad rapport

En mätning räknas som genomförd först när en rapport publiceras som
`docs/quality-report-<dataset>.md` där `<dataset>` är en stabil artefakt av
ZIP-namnet utan ändelse, gemener med understreck (t.ex.
`docs/quality-report-cerebras_consulting_100.md`). Rapporten:

1. anger arkivets `source_sha256` och `corpus_manifest_sha256` som mätningen är
   bunden till (se [Reproducerbarhet](#6-reproducerbarhet)),
2. innehåller urvalsmetadata: population per stratum, urval per stratum,
   `--count`, `--seed`, `sample_version`/urvalsalgoritm och SHA-256 för
   urvalskatalogens `manifest.json` samt för båda blindkopiorna,
3. namnger raterna (roll/funktion och datum, inte nödvändigtvis personnamn) och
   intygar blindning och oberoende,
4. redovisar resultat per axel, kategori och svårighetsgrad med KI, kappa och
   felorsaker enligt [Analys](#4-analys),
5. anger status: `antagen men ej mätt` (innan mätningen) eller
   `verifierad för publicerat urval` / `indikation – grindar ej uppnådda`.

Rapportens rubriker och tabeller finns i [docs/quality-report-mall.md](quality-report-mall.md).
En rapport som saknar SHA-256-, urvals- eller rater-uppgifterna är ogiltig och
får tas bort. Att "förbättra" en rapport genom att ta bort felaktiga poster från
urvalet är ett brott mot protokollet; posterna räknas med.

## 6. Reproducerbarhet

### 6.1 Urvalskommandot

Urvalet dras med ett dedikerat CLI-kommando:

```
contractforge publish-questions <zip> --count 100 --seed <seed> --output <dir>
```

| Del | Krav / beteende |
|-----|-----------------|
| `source` (positionell) | Det frysta datasetets ZIP (eller en uppackad katalog, för lokala prov). ZIP läses direkt; arkivet får inte packas upp för hand. |
| `--count` | Antal frågor som begärs. Standard 100; **mätningen kräver 100** frågor, dvs. 200 kodningar. Är `count` större än urvalspoolen publiceras hela poolen – rapporten anger då `pool_size`. |
| `--seed` | Heltal som styr urvals-RNG:n. Loggas i `manifest.json` och i rapporten. |
| `--output` | Katalog som skrivs. Ett befintligt, icke-tomt utdata kräver `--force`; ingenting skrivs tyst över. |
| `--corpus-sha256 <hex>` | **Obligatorisk i protokollets mätning.** Förväntat SHA-256 för källan; kommandot avbryter (`ValidationError`) om hashen inte stämmer. Utan flaggan binds urvalet bara i efterhand – se 6.2. |
| `--json` | Maskinläsbart resultat (`sample_size`, `pool_size`, `source_sha256`, `corpus_manifest_sha256`, `seed`, `count`, `sample_version`, `selection_sha256`, rater-blanketternas hashar, skrivna filer). |
| `--stratified` / `--no-stratified` | Stratifierat urval är standard (6.3). `--no-stratified` ger det likformiga urvalet och får bara ge status **indikation**. |
| `--blind-sheets` / `--no-blind-sheets` | Skriver `ratings_A.csv` + `ratings_B.csv` med olika ordning och `raters.json` (standard: på, krävs av 7.4). |

Utdata i mätningen: `manifest.json` (koordinatorns kopia), `selection.json`
(opaka id:n, härkomst, `selection_sha256`), `raters.json` (blanketthashar och
ordningar), `ratings_A.csv`, `ratings_B.csv`, `ratings.csv` (neutral kopia) och
`README.md`.

`--corpus-sha256` är kravet som gör urvalet *bundet i förväg*: körs kommandot mot
ett annat arkiv än det publicerade avbryts det i stället för att producera ett
tyst annat urval. Mätningen ska alltid kalla:

```bash
contractforge validate <uppackad katalog>          # ska ge PASS
contractforge publish-questions <zip> \
  --count 100 --seed <seed> \
  --corpus-sha256 <publicerad SHA-256> \
  --output <dir>
```

Output-katalogen innehåller (format `manifest.json` schema 1.0, `sample_version`
`v1`):

```
<dir>/
├── manifest.json     # urvalet maskinläsbart: source_sha256, corpus_manifest_sha256,
│                     # sampling {seed, count, sample_size, pool_size, algorithm,
│                     # sorted_by}, excluded_questions, ratings_columns,
│                     # questions[].{sample_id, row, contract_id, question_id,
│                     # category, difficulty, answerability, language, question,
│                     # expected_answer, expected_answer_structured, evidence,
│                     # evidence_quote, evidence_quotes, document_title,
│                     # document_text, document_texts {document_id: text}}
├── selection.json    # urvalet verifierbart: opaka sample_id -> (contract_id,
│                     # question_id), stratum-metadata och selection_sha256
├── ratings.csv       # blind ifyllningsblankett: sample_id, question,
│                     # expected_answer, evidence_quote + tomma Q1–Q4/comment
└── README.md         # instruktion till raterna, genererad ur manifestet
```

`selection.json` och `manifest.json` är koordinatorns filer. Raterna får
`ratings.csv` (och vid behov `README.md`).

`manifest.json` bär både blindposterna och kategoriseringsnyckeln; det får inte
delas med raterna före mätningen. `evidence_quotes` är den exakta avskriften av
varje evidensspann ur `document_text` (`start` inklusiv, `end` exklusiv), och
`evidence_quote` är samma delar åtskilda av en rad som bara innehåller `---`.
Varje avskrift verifieras maskinellt vid publicering.

### 6.2 Bindning till datasetets SHA-256

`manifest.json` innehåller:

- `source_sha256` – SHA-256 för ZIP-filen (för en katalog: en innehållshash över
  hela trädet, sorterad på relativ sökväg),
- `corpus_manifest_sha256` – SHA-256 för `dataset_manifest.json` inuti arkivet,
- `sampling` – `seed`, `count`, `sample_size`, `pool_size`, `algorithm`
  (`random.Random(seed).sample(pool, min(count, pool_size))`) och `sorted_by`
  (`(contract_id, question_id)`),
- `excluded_questions` – frågor som uteslutits ur poolen och varför (saknar
  förväntat svar, saknar evidens, spann utanför texten). En fråga som uteslutits
  här mäts inte och ska redovisas i rapporten som **ej bedömbar via paketet** – den
  får inte förbises, eftersom den kan vara just det fel protokollet söker.

`source_sha256`/`corpus_manifest_sha256` tillsammans med seed, `--count` och
`sample_version` är det som binder rapporten till exakt det dataset och den
urvalsmängd den beskriver. Rapporten skriver dessutom SHA-256 för de två
blindkopiorna (2.3) och för `manifest.json`.

`selection.json` innehåller dessutom `selection_sha256` – en hash över de
dra­gna `(contract_id, question_id)`-paren – så att en rapport kan bindas till
exakt den urvalsmängd den bedömer, oberoende av radordning. En ändrad
urvalsalgoritm kräver ny `sample_version`; en rapport som anger en
`sample_version` som inte motsvarar verktygets kan inte reproduceras.

### 6.3 Stratifierat urval (standard)

Verktyget drar numera ett **stratifierat** urval över strata
`(category, difficulty, answerability)`:

1. varje stratum får `floor(count × storlek / pool)` platser,
2. resten fördelas enligt största bråkdel (lika fall avgörs av stratumets nyckel,
   så resultatet beror aldrig på dict-ordning),
3. ett stratum som är mindre än sin kvot bidrar med alla sina frågor och resten
   omfördelas,
4. inom varje stratum drar `random.Random("<seed>:<stratum>")` ur en lista
   sorterad på `sha256(question_id)`, så urvalet är stabilt mellan körningar och
   mellan maskiner.

Två följder att känna till:

- **G6/G7 gäller aggregatnivån.** Ett stratums kvot är `count × andel`; ett
  litet stratum (t.ex. `contradiction` i en korpus utan motsägelsefrågor) kan
  fortfarande få för få poster för en egen felprocent. Rapportera då stratumet
  som **kategorin ej bedömd** – aldrig som "0 % fel".
- **`--no-stratified`** ger det gamla likformiga urvalet (`random.Random(seed)
  .sample`). En sådan mätning får status **indikation**, inte beslut, eftersom
  kategorier kan saknas helt.

För det frysta consulting-datasetet (nivå 2) finns bara kategorierna
`fact_extraction`, `entity_identification`, `comparison` och `completeness`; ett
urval därifrån mäter inte `contradiction`, `conditional` eller
`insufficient_evidence`. Det ska stå i rapporten, och en mätning över de
kategorierna kräver ett dataset med nivå 5–6.

### 6.4b Så körs mätningen (exakta kommandon)

```bash
# 1. Hämta och verifiera det frysta arkivet
gh release download v0.2.0 --pattern cerebras_consulting_100.zip   # eller v0.2.1
shasum -a 256 cerebras_consulting_100.zip    # 4344845098…ae7e
unzip -q cerebras_consulting_100.zip -d corpus
contractforge validate corpus                 # måste ge PASS

# 2. Publicera (eller återanvänd det publicerade) mätpaketet
contractforge publish-questions cerebras_consulting_100.zip \
  --output qa_packet --count 100 --seed 2026 \
  --corpus-sha256 4344845099a39dbdc7d09c54f6d50cf03e74d1f831c78eda34cb67182893ae7e
# -> ratings_A.csv och ratings_B.csv med olika ordning + raters.json med hashar

# 3. Två oberoende rater kodar var sin blankett (isolerat, se 7.4)

# 4. Beräkna resultatet
contractforge score-ratings qa_packet \
  --rater-a ratings_A.ifylld.csv --rater-b ratings_B.ifylld.csv \
  --rater-a-kind model --rater-b-kind model \
  --causes causes.csv --population population.json
```

`qa_packet/ratings.csv` är bara en neutral kopia av urvalet och ska inte delas ut;
raterna får `ratings_A.csv` respektive `ratings_B.csv`.
Det publicerade paketet finns redan i `examples/qsample_live_100/` med exakt dessa
filer (och `selection_sha256 1272bd8a…f650`).

### 6.4 Nytt urval kräver nytt seed eller nytt arkiv

- **Samma `(source, seed, count, sample_version)` ⇒ samma urval**, byte för byte,
  även för ZIP-arkivet (exporten är deterministisk med fasta ZIP-tidsstämplar;
  se `export/bundle.py`). `manifest.json:s` `created_at` skiljer sig, eftersom det
  beskriver körningen, inte urvalet.
- Ett **nytt urval kräver** antingen ett **nytt `--seed`** eller **ett nytt
  arkiv** (ny `source_sha256`). Att dra om samma seed är inte ett nytt urval
  utan en återupprepning.
- Efter en mätning får det **använda seedet och arkivet inte återanvändas för en
  ny mätning** med ändrade förutsättningar (t.ex. ny frågegenerator) – det skulle
  göra urvalet kopplat till resultatet. Ny frågegenerator ⇒ nytt arkiv ⇒ nytt
  urval.
- Har ett seed använts enligt protokollet får det inte användas igen som om det
  vore nytt.

## 7. Analysflöde i praktiken

**Verktyget gör de flesta stegen åt granskaren.** `publish-questions` skriver
två separata, slumpmässigt ordnade blanketter (`ratings_A.csv`, `ratings_B.csv`) och
en `raters.json` med deras SHA-256 och ordningar; `score-ratings` beräknar sedan
felprocent, Wilson-KI, överensstämmelse **per axel**, oenighetstyper, grindar och en
rapportutkast. Resten av avsnittet beskriver fortfarande det manuella flödet, som
är det som gäller när verktyget inte kan användas.

1. Hämta det frysta arkivet, kontrollera SHA-256 mot det publicerade värdet och
   kör `contractforge validate <uppackad katalog>` – ska ge `PASS`. Ett dataset
   som inte validerar får inte mätas.
2. Kör `publish-questions` med fryst arkiv, `--count 100`, seed och
   `--corpus-sha256 <publicerad hash>` (6.1).
3. Kontrollera `pool_size`, `sample_size`, `selection_sha256` och att vald
   `sample_version` motsvarar verktygets version (stratifierat urval är
   standard, se 6.3); därefter: kontrollera `pool_size` och
   `excluded_questions`. Anteckna risken för kategorier som saknas i urvalet.
4. Gör två blinda kopior (2.3) med neutrala blind-id:n; beräkna deras SHA-256.
5. Utfärda två separata, slumpmässigt ordnade blanketter (7.4) och dela dem med
   två oberoende ratare. Raterna arbetar isolerat och ser inte varandras svar
   förrän båda kodat klart.
6. Koda 200 poster (100 frågor × 2 rater) mot de fyra axlarna. Varje ratare
   skriver `{blind_id, Q1, Q2, Q3, Q4, kommentar}` – i praktiken `ratings.csv`
   fylld med `;`-separerade värden, eller motsvarande JSON-rader.
7. Frys de individuella blanketterna och beräkna enighet och kappa **per axel**
   på dem (7.5). Först därefter avgör en **tredje ratare** oeniga poster; dennes
   kod gäller för postens kvalitet och fältet `decisor` sätts – aldrig i
   överensstämmelseberäkningen.
8. Klassificera felorsak per felaktig post (4.4).
9. Beräkna felprocent, Wilson-KI och gruppstatistik. Jämför mot grindarna (5.2).
10. Publicera rapporten enligt mallen och sätt status.

Räkneuttalanden (felprocent, KI, kappa) ska upprepas av en andra person eller
verifieras med ett skript som publiceras tillsammans med rapporten. Med
`score-ratings` är skriptet en del av ContractForge; publicera
`quality_score.json` tillsammans med rapporten så att talen kan kontrolleras.

```bash
contractforge score-ratings examples/qsample_live_100 \
  --rater-a ratings_A.ifylld.csv --rater-b ratings_B.ifylld.csv \
  --rater-a-kind model --rater-a-name "<modell>" \
  --rater-b-kind model --rater-b-name "<modell>" \
  --causes causes.csv [--consensus consensus.csv] [--population population.json]
```

Kommandot vägrar köra om inga poster är kodade, kräver en orsak för varje felpost
(G6), rapporterar okodade poster (G7) och skriver
`quality_score.json` + `quality_report_draft.md`. Utkastet ska föras över i mallen
och granskas av en människa innan det publiceras.
 Protokollet
mäter facit, inte ratarna; ratarnas egen kompetens och tidigare erfarenhet av
svenska avtal ska dokumenteras i rapporten.

### 7.4 Två separata, slumpmässigt ordnade blanketter – och ingen kontakt

Radordningen i `ratings.csv` följer urvalets avtalsordning. Om båda raterna får
samma ordning kan de dra slutsatser om vilka frågor som hör ihop (samma avtal)
och därmed bli icke-oberoende. Kravet:

1. Utfärda **två separata blanketter** ur samma urval – en per ratare.
2. Varje blankett **slumpas om** med sitt eget ratarspecifika seed:
   `random.Random("<urvals-seed>:<ratar-id>").shuffle`, där `ratar-id` är ett
   neutralt id (`A`, `B`) och **inte** anger vem rataren är.
3. Blanketterna får **olika ordning** (verifieras: ordningen får inte vara
   identisk) och kolumnordningen ska vara densamma i båda, så svaren kan slås
   ihop på `blind_id`.
4. Blanketternas **SHA-256** skrivs i rapporten (tillsammans med
   `selection_sha256`), så att det går att bevisa vilken version respektive
   ratare kodade.
5. Raterna arbetar **isolerat**: de ser inte varandras svar, får inte diskutera
   enskilda poster och får inte veta hur många fel den andra hittat – förrän båda
   lämnat in sin fullständiga kodning.

### 7.5 Efter inlämning: konsensus får aldrig ersätta de individuella bedömningarna

Efter kodningen följer, i den här ordningen:

1. **Frys de individuella blanketterna först.** Deras SHA-256 och koderna per
   `blind_id` sparas innan något annat görs.
2. **Beräkna överensstämmelsen på de ursprungliga, individuella koderna** – per
   axel (4.3). Detta är ett faktum om mätningen och får inte ändras i efterhand.
3. **Först därefter** får en oenig post lösas: tredje ratare, eller (om
   protokollet så medger) en dokumenterad konsensusdiskussion.
4. En konsensuskod får **enbart** användas för att avgöra postens *kvalitet*
   (Q2/Q3-felprocent och felorsak). Den får **aldrig** räknas in i P₀ eller κ och
   aldrig skrivas in i en individuell kodning.
5. Rapporten redovisar **både** den individuella överensstämmelsen och det
   konsensuslösningsförfarande som använts, med antal lösta poster, vem som var
   tredje ratare och hur många poster som förblev oeniga (`oenighet kvar`).

En rapport som beräknar kappa på efterhandsjämkade svar är ogiltig och räknas inte
som genomförd mätning.

## 8. Omfattning och begränsningar

- **Urvalet är icke-representativt för hela korpusen.** Det dras från ett
  (1) fryst dataset med en (2) avtalstyp, en nivå och en konfiguration. Resultatet
  gäller **endast det publicerade urvalet**. Det får citeras som
  *"X % av det publicerade urvalet (N = 100) i <dataset>, seed <seed>"* – aldrig
  som *"ContractForges facit är X % korrekt"*.
- Mätningen gäller inte frågor som arkivet avvisat (de finns inte i arkivet) och
  inte frågor som aldrig genererades för en kategori (t.ex. `legal_reasoning` när
  inga relationspar finns) och inte frågor som verktyget uteslutit ur urvalspoolen
  (6.2). Det gör att mätningen underskattar fel som beror på att en fråga inte
  gick att läsa ur sitt eget paket.
- Två rater fångar oenighet men **inte gemensamma blinda fläckar**. Andra
  ratare, andra dataset och en tredje oberoende mätning på annan avtalstyp är
  krav för vidare påståenden; se [Grindar](#5-grindar) och
  [docs/decisions.md](decisions.md).
- **Ratarnas art måste anges i rapporten.** Är en eller båda ratarna AI-modeller
  ska mätningen betecknas som en **modellbaserad oberoende bedömning** – aldrig
  som mänsklig juridisk granskning. Två olika modeller kan dela systematiska fel
  (samma fördomar om svensk avtalssvenska, samma tunnelseende på villkor och
  undantag), så en hög överensstämmelse mellan två modeller bevisar inte
  mänsklig korrekthet. Rapporten ska ange: modellnamn och version för varje
  ratare, om de är mänskliga granskare (och deras erfarenhet) eller modeller,
  vem som var tredje ratare, och en uttrycklig mening om vad resultatet gäller:
  *"modellbaserad bedömning av N = 100 frågor"* eller
  *"mänsklig juridisk bedömning av N = 100 frågor"*.
- Vid oenighet som tredje rataren inte avgör (alla tre svar olika) räknas posten
  som `osäker` i felprocenten och redovisas separat som `oenighet kvar`.
- Protokollet mäter **facit**; det mäter inte om ett AI-system som använder
  facit ger bra svar. Det är en annan mätning (se README:s benchmarkavsnitt).
- `provider=mock`-dataset får mätas, men mätningens resultat får aldrig citeras
  som modellresultat.

## 9. Nästa mätning: villkor, motsägelser och otillräcklig evidens (nivå 5–6)

Den första mätningen ska **förbli ett fryst kvalitetsprov av consulting-datasetet**
(nivå 2) och får inte blandas ihop med nivå 5–6. Det är ett medvetet val: att
ändra populationen mitt i en mätning gör resultatet otydligt.

Nivå 5–6 kräver ett **eget** prov med eget arkiv och egen rapport, eftersom
bedömningen då handlar om annat:

| Fråga | Varför nivå 5–6 är en egen mätning |
|-------|-------------------------------------|
| Q1 ANSWERABLE | Populationssanningen är inte alltid `ANSWERABLE`; `CONDITIONAL`, `CONFLICTING_EVIDENCE` och `INSUFFICIENT_EVIDENCE` är **förväntade** svar och måste kodas mot rätt klass, inte som fel. |
| Q2 ANSWER_CORRECT | Svaret kan vara *rätt villkorat* ("om bilaga B gäller …") utan att vara ett enskilt värde; korrekthet är då att **rätt klass** anges. |
| Q3 EVIDENCE_SUFFICIENT | Evidensen kan med flit peka på en **saknad** bilaga. Q3 ska då bedöma om frånvaron är korrekt deklarerad, inte om citatet räcker. |
| Q4 ANOMALY_HANDLING | På nivå 2 är axeln nästan alltid `ej_tillamplig`; på nivå 5–6 är den huvudfrågan (undantag, motsägelse, tillägg som ersätter). |

Praktiska krav för det provet (samma verktyg, nytt arkiv/seed):

1. Generera och frys ett nivå 5–6-arkiv (alla 10 avtalstyper, båda svårighetsgraderna),
   publicera det som release-asset med SHA-256 och verifiera med
   `contractforge validate`.
2. Utöka bedömningsblanketten med **klassaxeln** – en kod per post för vilken
   answerability som är korrekt (`ANSWERABLE` / `CONDITIONAL` /
   `CONFLICTING_EVIDENCE` / `INSUFFICIENT_EVIDENCE`) – och mät G3 mot den.
3. Separata grindar: Q1-klassning är den centrala axeln; Q2 handlar om att klassen
   är rätt, inte om att ett värde finns.
4. Rapporten får **inte** jämföra felprocenten med nivå 2-mätningen som ett
   "försämringsmått" – populationerna är olika.
5. Nivå 5–6 är de fall som är mest värdefulla för svårare testkorpora; mätningen
   bör därför publiceras **innan** de används som testdata i externa experiment.

## Referenser

- [docs/verification.md](verification.md) – kontroller, statusar, LLM-domaren
- [docs/benchmarks.md](benchmarks.md) – frysta dataset och release-assets
- [docs/reproducibility.md](reproducibility.md) – hur ett publicerat arkiv verifieras
- [docs/quality-report-mall.md](quality-report-mall.md) – rapportmall
- [docs/decisions.md](decisions.md) – beslutspost 2026-10-31 om bevisat vs antaget
- `src/contractforge/publish.py` – implementationen av urvalskommandot (referens
  för fältnamn; ändra inte koden som en del av en mätning)
