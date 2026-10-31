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

### 4.3 Inter-rater-överensstämmelse

Två mått, båda rapporteras per axel och för alla fyra axlarna sammantaget:

**Procentuell överensstämmelse** – andelen poster där raterna valt samma kod
(bland annat enighet):

```
P₀ = (antal poster där raterna är eniga) / (antal poster kodade av båda raterna)
```

**Cohens kappa** – korrigerar för den enighet som slumpen skulle ge givet
marginalerna:

```
P₀  = observerad enighet (se ovan)
pₑ  = Σ_c (n₁c · n₂c) / N²

      n₁c = antal poster där ratare 1 valde kod c
      n₂c = antal poster där ratare 2 valde kod c
      N   = antal poster kodade av båda raterna

κ  = (P₀ − pₑ) / (1 − pₑ)
```

Tolkning (Landis & Koch, redovisas i rapporten): `< 0,41` låg, `0,41–0,60`
måttlig, `0,61–0,80` betydande, `> 0,80` i stort sett fullständig. Frågor med
`ej_tillamplig` på Q4 hålls utanför Q4-kappan eftersom en ratare kan välja
`ej_tillamplig` där den andra bedömer `ja`; rapporten anger `n` i varje kappa.

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

En post där tredje rataren dömer svaret korrekt får ingen orsak – den är inte ett
fel, bara en oenighet. Kvarvarande `validatorluckor` är det som protokollet är
till för att hitta: fel som den deterministiska regeln inte ser, och som därför
inte fångas av någon befintlig check.

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
| G5 | Inter-rater-överensstämmelse på Q2 (alla rater) | procent **≥ 90 %**; Cohen's κ **≥ 0,70** |
| G6 | Felorsaker | **100 %** av alla poster kodade `nej`/`osäker` har en av de fyra orsakerna, och varje `validatorlucka` har ett namngivet motbevis (vilken regel som inte fångade felet) i rapporten |
| G7 | Urvalsintegritet | varje stratum med ≥ 10 giltiga frågor i populationen har ≥ 1 bedömd post; alla blindposter (100 frågor × 2 rater = 200 kodningar) är kodade |
| G8 | Blindning | blindkopian innehåller inte scenario, gold, `precision`, kategori eller svårighetsgrad, och blindningen intygas i rapporten |

Grindarna gäller **urvalet**, inte hela korpusen (se [Omfattning](#8-omfattning-och-begränsningar)).
Om en tröskel missas får facit citeras med mätningen som **indikation**, aldrig
som oberoende bevis; rapportens slutsats ska då ange exakta avvikelser och
vilken åtgärd (frågegenerering, validator, scenario) som följer.

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
| `--json` | Maskinläsbart resultat (`sample_size`, `pool_size`, `source_sha256`, `corpus_manifest_sha256`, `seed`, `count`, `sample_version`, skrivna filer). |

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
│                     # evidence_quote, evidence_quotes, document_title, document_text}
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
5. Dela blindkopian med två oberoende ratare. Raterna ser inte varandras svar
   förrän båda kodat klart, och får inte diskutera enskilda poster före kodning.
6. Koda 200 poster (100 frågor × 2 rater) mot de fyra axlarna. Varje ratare
   skriver `{blind_id, Q1, Q2, Q3, Q4, kommentar}` – i praktiken `ratings.csv`
   fylld med `;`-separerade värden, eller motsvarande JSON-rader.
7. Beräkna enighet och kappa. Vid oenighet ser en **tredje ratare** samma
   blindpost; tredje ratarens kod gäller för den posten och fältet `decisor` sätts.
8. Klassificera felorsak per felaktig post (4.4).
9. Beräkna felprocent, Wilson-KI och gruppstatistik. Jämför mot grindarna (5.2).
10. Publicera rapporten enligt mallen och sätt status.

Räkneuttalanden (felprocent, KI, kappa) ska upprepas av en andra person eller
verifieras med ett skript som publiceras tillsammans med rapporten. Protokollet
mäter facit, inte ratarna; ratarnas egen kompetens och tidigare erfarenhet av
svenska avtal ska dokumenteras i rapporten.

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
- Vid oenighet som tredje rataren inte avgör (alla tre svar olika) räknas posten
  som `osäker` i felprocenten och redovisas separat som `oenighet kvar`.
- Protokollet mäter **facit**; det mäter inte om ett AI-system som använder
  facit ger bra svar. Det är en annan mätning (se README:s benchmarkavsnitt).
- `provider=mock`-dataset får mätas, men mätningens resultat får aldrig citeras
  som modellresultat.

## Referenser

- [docs/verification.md](verification.md) – kontroller, statusar, LLM-domaren
- [docs/benchmarks.md](benchmarks.md) – frysta dataset och release-assets
- [docs/reproducibility.md](reproducibility.md) – hur ett publicerat arkiv verifieras
- [docs/quality-report-mall.md](quality-report-mall.md) – rapportmall
- [docs/decisions.md](decisions.md) – beslutspost 2026-10-31 om bevisat vs antaget
- `src/contractforge/publish.py` – implementationen av urvalskommandot (referens
  för fältnamn; ändra inte koden som en del av en mätning)
