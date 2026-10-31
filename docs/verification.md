# Verifieringsmodell och facit

Detta dokument beskriver hur ContractForge avgör om ett genererat dokument får
exporteras som godkänt, och vad facit får påstå.

## Grundregel

**Ett scenario är inte facit.** Scenariot är den avsedda sanningen. Facit är det
som kan bevisas mot den färdiga kanoniska texten. Om texten inte uttrycker
scenariots fakta – eller uttrycker dem fel – får dokumentet inte exporteras som
godkänt.

## Kanonisk text och teckenoffsets

- Varje dokument renderas till en **kanonisk text** (UTF-8). `.txt`-filen är
  exakt denna text.
- Alla evidenspann är `(document_id, clause_id, start, end)` där `start` är
  inklusiv och `end` exklusiv, räknat i Unicode-kodpunkter i den kanoniska
  texten.
- Positioner beräknas av renderaren och verifieraren – aldrig av modellen.
- `.md`, `.docx` och `.pdf` är härledda representationer från samma
  strukturerade klausuler. Evidensspann refererar alltid den kanoniska texten.
- Datasetets `validate`-kommando läser `.txt` och `rendered.json` och verifierar
  att varje spann slice:ar exakt den text som facit påstår.

## Determinism

Verifieringen använder inga API-anrop. Den är reproducerbar för samma indata och
kan köras offline i CI.

## Kontroller

### Dokumentnivå

| Check | Vad den avgör |
|-------|----------------|
| `structure` | Dokument-id, klausul-id, ordning, rubriker och unika id:n |
| `explicit_fact_present` | Explicit faktum finns ordagrant i rätt klausul (mode `EXPLICIT`) |
| `semantic_fact_supported` | Parafras/vagt uttryck har avsedd betydelse (talord, enheter, valuta) |
| `entity_present` | Parter/entiteter och roller finns i texten |
| `reference_resolves` | Hänvisningar pekar på existerande dokument/bilaga eller deklarerad saknad bilaga |
| `counts` | Förväntat antal dokument, klausuler och fakta |
| `no_duplicate_definitions` | Inga oavsiktliga dupliceringar |
| `intentional_anomaly_present` | Avsiktliga motsägelser/undantag finns där scenariot säger |

Varje check har ett stabilt `check_id`, en status (`PASS`, `FAIL`, `WARN`,
`SKIP`), dokument/klausul och ett meddelande som kan skickas tillbaka till
modellen som reparationsfeedback.

### Paketnivå

| Check | Vad den avgör |
|-------|----------------|
| `document_set` | Alla planerade dokument finns, inga extra |
| `cross_document_reference` | Tillägg/bilagor refererar korrekt till huvudavtalet |
| `supersession` | Ett tillägg ersätter rätt villkor och facit beskriver både gammalt och nytt villkor |
| `missing_information` | Nivå 6-frågor saknar entydigt svar och markeras korrekt |

## Statusar

| Status | Betydelse | Exporteras |
|--------|-----------|------------|
| `ACCEPTED` | Alla checkar passerade på första försöket | Ja |
| `REPAIRED` | Underkänt försök reparerades och godkändes | Ja, med `attempts` |
| `REJECTED` | Reparationsförsök uttömda eller oanalyserbar modellutdata | Nej – endast `rejected/<contract_id>.json` |

Datasetet redovisar alltid antal godkända, reparerade och kasserade dokument.

## Semantisk verifiering

Den semantiska kontrollen är deterministisk och oberoende av generatorns modell:

- normaliserar siffror och svenska talord (`tre`, `tolv`, `trettio`, …),
- mappar enheter (`månader`, `mån`, `dagar`, `dygn`, `år`, `veckor`, `kvartal`,
  `halvår`),
- mappar valutor (`SEK`, `kr`, `kronor`) och procent (`%`, `procent`),
- söker runt faktats nyckelord i rätt klausul och jämför det operativa värdet,
- flaggar när ett distraktorvärde används som om det vore det avsedda.

En LLM-domare får finnas som **rådgivande** komponent men:

1. får bara ge `WARN`, aldrig godkänna ett dokument,
2. måste markeras som icke-oberoende i resultatet,
3. är avstängd som standard.

Att köra samma modell som genererade texten i ett separat anrop gör den inte till
oberoende facit.

## Golden-frågor

Frågor genereras deterministiskt från scenario + gold och verifieras mot den
kanoniska texten innan de godkänns:

- förväntat svar måste stödjas av dokumenten,
- alla `required_facts` måste ha evidens,
- alla evidensspann måste vara giltiga,
- flervärdesfrågor (t.ex. samtliga parter) får ett
  `completeness`-krav.

Frågor som inte kan stödjas flyttas till `rejected` och exporteras inte som
godkända. Nivå 5–6 använder `CONFLICTING_EVIDENCE`, `CONDITIONAL` och
`INSUFFICIENT_EVIDENCE` när dokumenten inte ger ett entydigt svar.

## Facitformat (exempel)

```json
{
  "question_id": "q_001",
  "question": "Vilken uppsägningstid gäller?",
  "expected_answer": { "value": 3, "unit": "months" },
  "required_facts": ["fact_notice_period"],
  "evidence": [
    {
      "document_id": "contract_0001",
      "clause_id": "clause_03",
      "start": 1250,
      "end": 1318
    }
  ],
  "answerability": "ANSWERABLE"
}
```

## Manuell granskning

`WARN`-checkar och kasserade avtal är avsedda för granskning. Inget i
ContractForge påstår att ett godkänt dokument är juridiskt korrekt – endast att
det motsvarar det strukturerade scenariot enligt de dokumenterade kontrollerna.
