# Avtalsdefinitioner (contract definitions)

Den här katalogen innehåller **all** typvariation för ContractForge. Varje
`*.json`-fil beskriver en avtalstyp och läses in av
`contractforge.scenarios.registry.ContractTypeRegistry`. Genereringskoden
(`contractforge.scenarios.generator`) innehåller inga specialfall per avtalstyp:
all variation – parter, fakta, klausulplan, dokumentpaket, distraktorer,
motsägelser och saknad information – kommer från dessa filer.

Alla tio avtalstyper måste finnas. Saknas en typ, eller är en definition
ogiltig, kastar registret `ScenarioError` vid inläsning (ingen tyst fallback).

## Syntetiska data

* Alla person- och organisationsnamn är påhittade.
* Adresser använder tydligt fiktiva gatunamn (`Exempelvägen`, `Provgatan`,
  `Testvägen`) och är inte verkliga adresser.
* Organisationsnummer är **tydligt syntetiska**. Fältet
  `org_number_series` måste matcha `5590xx` (fyra obligatoriska siffror + två
  valfria siffror, t.ex. `559001`). Generatorn lägger till fyra deterministiska
  siffror: `5590xx-xxxx`. Serien `5590xx` används uteslutande som testserie i
  det här projektet och representerar inga verkliga organisationer.
* Individer har inget organisationsnummer (`org_number_series` får inte anges
  för `kind: "individual"`).
* Alla genererade scenarier har `synthetic: true` och en svensk beskrivning som
  anger att uppgifterna är påhittade.

## Filstruktur

```json
{
  "contract_type": "consulting",
  "definition_version": "1.0",
  "title_sv": "Konsultavtal",
  "title_en": "Consulting Agreement",
  "description_sv": "...",
  "parties": [ ... ],
  "facts": [ ... ],
  "main_clauses": [ ... ],
  "extra_main_clauses": [ ... ],
  "appendix_clauses": [ ... ],
  "amendment_clauses": [ ... ],
  "package": { "appendix": { ... }, "amendment": { ... } },
  "variation": { "styles": [ ... ], "document_qualities": [ ... ] },
  "distractor_facts": [ ... ],
  "contradiction": { ... },
  "exception": { ... },
  "missing_information": { ... }
}
```

### Parter (`parties`)

| Fält | Betydelse |
| --- | --- |
| `party_id` | Stabilt id, t.ex. `party_client` |
| `role` | Rollkod (`client`, `supplier`, `landlord`, ...) |
| `role_sv` / `role_en` | Rollens namn på svenska/engelska |
| `kind` | `organisation` eller `individual` |
| `name_pool` | Minst ett syntetiskt namn; generatorn väljer deterministiskt |
| `address_pool` | Syntetiska adresser (valfritt) |
| `org_number_series` | Endast organisationer; `5590xx` |

### Fakta (`facts`, `distractor_facts`, `contradiction.fact`, `exception.fact`, `missing_information.conditional_fact`)

| Fält | Betydelse |
| --- | --- |
| `fact_id` | Unikt id i definitionen |
| `kind` | `money`, `duration`, `percentage`, `integer`, `decimal`, `text`, `date`, `defined_term`, `entity`, ... |
| `label_sv` / `label_en` | Människoläsbar etikett |
| `unit` / `currency` | Enhet (`months`, `days`, `hours`, `percent`, ...) och valuta (`SEK`, `EUR`) |
| `keywords_sv` / `keywords_en` | Nyckelord för semantisk verifiering |
| `variants` | En eller flera tillåtna värden. Varje variant har `value`, `explicit_surfaces` (nivå 1) och `paraphrase_surfaces` (nivå 2) |
| `entity_party_id` | Endast `entity`-fakta: värdet hämtas från partens namn |
| `based_on_fact_id` | Endast distraktorfakta: vilket basfaktum det liknar |

Regler som registret upprätthåller:

* `entity`-fakta har `entity_party_id` och inga varianter.
* Övriga fakta har minst en variant med minst en explicit och en parafras.
* `money` kräver `currency`, `duration` kräver `unit`, `percentage` kräver
  `unit: "percent"`.
* Alla basfakta måste användas av minst en huvudklausul.
* Varje faktum-id är unikt över hela definitionen.

### Klausulplan (`main_clauses`, `extra_main_clauses`, `appendix_clauses`, `amendment_clauses`)

| Fält | Betydelse |
| --- | --- |
| `clause_id` | Stabilt id (`clause_01`, `appendix_a_clause_01`, ...) |
| `heading_sv` / `heading_en` | Rubrik |
| `fact_ids` | Fakta som klausulen ska uttrycka |
| `mode` | `explicit`, `paraphrase`, `distractor`, `reference`, `definition`, `contradiction`, `exception`, `conditional`, ... |
| `optional` | Får tas bort när `request.clauses` är mindre än planen |
| `distributed_facts` | Bilageklausul som tar emot fakta som flyttas ut ur huvudavtalet (nivå 4) |
| `depends_on` / `reference_to` | Klausulberoenden |
| `instruction_sv` / `instruction_en` | Anvisning till dokumentgenereringen |
| `min_difficulty` / `max_difficulty` | Svårighetsintervall (används av tilläggsklausuler) |

### Dokumentpaket (`package`)

`appendix` läggs till från svårighetsgrad 3 och `amendment` från
svårighetsgrad 5 när `ScenarioRequest.package` är `true`. När paketet är avstängt
viks bilage- och tilläggsklausulerna in i huvudavtalet i stället, så att
svårighetsgradens egenskaper (distraktorer, relationer, motsägelser, saknad
information) finns kvar även i en enda dokumentfil.

### Distraktorer (`distractor_facts`)

Nära men felaktiga värden som förekommer i bilagan. De uttrycks i
`appendix_clauses` med `mode: "distractor"` och skapar svårighetsgrad 3.

### Anomalimaterial (`contradiction`, `exception`, `missing_information`)

* `contradiction.base_fact_id` + `contradiction.fact`: avsiktlig motsägelse
  (nivå 5, `CONFLICTING_EVIDENCE`, tilläggsklausul med `mode: "contradiction"`).
* `exception.base_fact_id` + `exception.fact`: avsiktligt undantag (nivå 5,
  `Anomaly` med `governing_fact_id`, klausul med `mode: "exception"`).
* `missing_information`: `missing_documents` som refereras men inte genereras,
  ett `insufficient_evidence_fact_id` som beskriver vilken uppgift som inte kan
  verifieras, ett villkorat fakta för `CONDITIONAL` och styrande faktum för
  villkoret (nivå 6). Anomalin för `INSUFFICIENT_EVIDENCE` har medvetet inga
  `fact_ids`: alla scenariofakta ska vara uttryckta i dokumenten, medan själva
  informationsluckan ligger i de dokument som saknas.

Notera att numeriska fakta måste använda den `kind` som motsvarar
normaliseringen i `contractforge.validation.semantic`: perioder i dagar, veckor,
månader, år och timmar är `duration` (med `unit`), medan rena antal (platser,
användare, personer, enheter) är `integer`. Belopp anges i SEK eftersom
valutanormaliseringen är SEK-baserad.

## Lägga till eller ändra en avtalstyp

1. Kopiera en befintlig JSON-fil och ändra innehållet.
2. Kör `python -c "from contractforge.scenarios.registry import load_registry;
   load_registry()"` – registret validerar alla definitioner vid inläsning.
3. Kör testsviten `python -m pytest tests/test_scenarios.py -q`.

Definitionerna får inte innehålla verkliga personer, organisationer,
organisationsnummer eller adresser.
