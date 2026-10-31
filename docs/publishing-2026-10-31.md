# Publikation: 2026-10-31

Den här releasen påverkar **inte** generering, validering eller det frysta
mätpaketet (`selection_sha256 1272bd8a…f650`). Den lägger till det som saknades
inför den mänskliga granskningen och en separat återtestning av de tolv
granskningsfynden från v0.1.0.

## 1. De tolv granskningsfynden återtestade mot `d81fd25`

Kvalitetsmätningen visar om **facit** är rätt. Den visar inte om defekterna från
v0.1.0-granskningen är åtgärdade – det kräver ett separat test på den kod som
ska användas. Det finns nu som ett reproducerbart skript:

```bash
python benchmarks/review_checks.py        # mänsklig sammanfattning
python benchmarks/review_checks.py --json # maskinläsbart (exit 0 = alla passerar)
```

En kontroll per fynd, varje med en kort reproduktion och evidens:

| Fynd | Kontroll | Resultat |
|------|----------|----------|
| F1 | nivå 5/6-dataset validerar; `generate` gate:ar före exit | OK – validation=PASS, exporterade = validerade frågor |
| F2 | referenser har spann som namnger målet, inga reservspann | OK – 290 namnger målet, 0 saknar |
| F3 | "ersätts härmed" → REJECTED, icke-bindande distraktor → ACCEPTED | OK |
| F4 | okänd usage → `cost_usd=None`, känd usage → kostnad | OK |
| F5 | providerfel som ekar nyckel redigeras i meddelande/details/envelope | OK |
| F6 | främmande katalog refuseras, användarfil finns kvar, prefixbyte refuseras | OK |
| F7 | exakt spann som breddas → dataset FAIL, orört → PASS | OK |
| F8 | villkorade fakta har evidens; endast missing-information undantas | OK – 10 kontrollerade, 0 utan evidens |
| F9 | syntetiska org.nr är Luhn-ogiltiga | OK – 0/120 giltiga |
| F10 | benchmarkrapporten har hashar, runtime, validering | OK |
| F11 | olika scenarioingångar ger olika snapshot; definitionshash + runtime sparas | OK |
| F12 | Retry-After-tak 300 s, delad cooldown, inga riktiga sömnar | OK |

**12/12 passerar**, både i projektets venv och i en **ren venv** skapad från
`constraints.txt` (`python -m venv`, `pip install -c constraints.txt -e ".[dev]"`,
1 803 tester gröna). Kontrollerna ingår även i CI (se 4).

## 2. Den mänskliga körningen – protokollet preciserat

Ny sektion 7.7 i `docs/quality-protocol.md`, utifrån din avgränsning:

- **Orörda blanketter:** raterna får alltid blanketter direkt ur
  `publish-questions` (eller fryst kopia med dokumenterad SHA-256) – aldrig en
  fil som varit ifylld eller redigerad.
- **Ingen förhandsinformation:** den som sett urvalet, kandidat-id:n,
  modellresultat eller den preliminära rapporten **får inte** räknas som blind
  förstahandsgranskare. En sådan person kan bara delta som tredje ratare i
  adjudikeringen.
- **Obligatorisk granskar- och tidslogg** (`rater_log.json`): granskare, typ,
  kvalifikationer, blanketthash, `received_at`/`completed_at` med tidszon,
  isoleringsintyg och `prior_exposure`. Saknas loggen är G8 ej uppfylld.
- **Adjudikering åtskild:** individuella koder fryses och hashas **först**,
  överensstämmelsen beräknas på dem, och först därefter avgör en tredje ratare
  oeniga poster i en **egen fil** (`adjudication.csv`) – aldrig i A:s eller B:s
  blanketter.
- **Jämförelse per post:** människa vs modell jämförs `sample_id`-vis per axel,
  felorsaker jämförs för poster där någon underkänt facit, och den preliminära
  mätningens kandidatposter redovisas som bekräftade/avfärdade. Modellpassen är
  **inte** oberoende observationer och får aldrig bidra till N i en grind.

## 3. Rapportmallen har fått motsvarande fält

`docs/quality-report-mall.md` har nu plats för granskar-/tidslogg, blanketthashar,
adjudikationsfil och jämförelsen mot modellmätningen.

## 4. CI

- Nytt steg: `python benchmarks/review_checks.py` – failar om något av F1–F12
  inte längre håller.
- Testsviten: 1 803 → 1 804 tester (AI-granskningens blanketter kan poängsättas
  av samma motor).

## Status

| Mått | Godkänt när | Status |
|---|---|---|
| Q2 – svarskorrekthet | ≥ 95 %, Wilson nedre ≥ 90 % | **ej uppmätt** (mänsklig) |
| Q3 – evidenstillräcklighet | ≥ 90 %, Wilson nedre ≥ 85 % | **ej uppmätt** (mänsklig) |
| Q2 – överensstämmelse | ≥ 90 %, κ ≥ 0,70 | **ej uppmätt** (mänsklig) |
| Q1/Q3/Q4 – överensstämmelse | ≥ 80 %, κ ≥ 0,60 | **ej uppmätt** (mänsklig) |

Preliminär modellmätning: [docs/quality-report-ai-preliminary.md](https://github.com/kajmund/contractforge/blob/main/docs/quality-report-ai-preliminary.md) (fryst prioriteringsmätning, inte en grind).
