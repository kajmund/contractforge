# AI-granskning (preliminär) – 2026-10-31

Genomförd enligt beslutet: **två modeller, två pass, 100 frågor**, före den
mänskliga granskningen. Detta är en **preliminär modellbaserad mätning** – inte
en oberoende kvalitetsbedömning och inte underlag för kvalitetsgrindarna.

## Vad som kördes

| | Ratare A | Ratare B |
|---|---|---|
| Modell | `gpt-oss-120b` (Cerebras) | `qwen-3.8-27b` (Cerebras) |
| Pass 1 – självständigt svar utan facit | 100/100 lyckade | 100/100 lyckade |
| Pass 2 – facitgranskning (Q1–Q4) | 100/100 lyckade | 100/100 lyckade |
| Token (båda pass) | 247 693 | 362 111 |
| Genomsnittlig latens | 583 ms | 855 ms |

- Fryst paket: `examples/qsample_live_100` (`selection_sha256 1272bd8a…f650`,
  källa `cerebras_consulting_100.zip`, SHA-256 `43448450…ae7e`, seed 2026).
- Promptar: `ai-review-answer-v1` (pass 1, blind) och `ai-review-verdict-v2`
  (pass 2, med facit).
- Temperature 0, seed 2026, `max_tokens` 8 000.
- Allt är fryst: `benchmarks/results/ai_review_blind2/` (pass 1 och pass 2 per
  ratare, `run.json` med promptversioner, token, latens och reservation) samt
  `examples/qsample_live_100/quality_score.json` + `quality_report_draft.md`.

**Reservation (skriven i `run.json`):** båda raterna är språkmodeller via
Cerebras, och `gpt-oss-120b` genererade dessutom korpusen. Deras enighet är
därför **inget bevis** för att facit är juridiskt korrekt.

## Preliminärt resultat (urvalsvärde, N = 100)

| Axel | ja | Wilson 95 % | Enighet P₀ | κ | Grind |
|------|----:|-------------|-----------:|---:|-------|
| Q1 besvarbar | 97 % | 91,5 – 99,0 % | 98 % | 0,49 | G3 uppfylld (punkt), G5b ej |
| Q2 svar korrekt | 92 % | 85,0 – 95,9 % | 93 % | 0,19 | **G1 ej** (punkt < 95 %), **G5a ej** |
| Q3 evidens tillräcklig | 45 % | 35,6 – 54,8 % | 54 % | 0,14 | **G2 ej**, **G5c ej** |
| Q4 undantag/motsägelser | – | – | 100 % | – | ej tillämplig (n = 0) |

Slutsats för mänsklig granskning: **Q2 ligger under 95 %-grinden** och
**Q3 är den svaga länken** – men modellerna är inte tillräckligt eniga för att
deras egen kodning kan användas som mått (κ 0,14–0,49). Tabellen ska därför
läsas som en prioriteringslista, inte som en felprocent.

## Pass 1: modellernas egna svar, utan facit

| Ratare | Svar som bär facitets uppgift |
|--------|-------------------------------|
| A `gpt-oss-120b` | 59/100 |
| B `qwen-3.8-27b` | 73/100 |

Jämförelsen är semantisk (ContractForges egen normalisering för belopp, talord,
enheter och datum). Skillnaden mellan modellerna (59 vs 73) är i sig ett tecken
på att formatvalet (`1150 (SEK)` vs `1 150 kr per timme`) och svarsstilen
påverkar mer än faktainnehållet. Fall där båda modellernas egna svar skiljer sig
från facit men stämmer sinsemellan: se `run.json` →
`comparison.both_own_answers_differ_from_gold_but_agree_with_each_other`.

## Diagnostik (det du efterfrågade)

### 1. Båda underkänner facit (Q2 ≠ ja hos båda): **1 post**

`cfq-eeb92c4957fa` – "Vilket värde har Rättigheter till resultat enligt
Konsultavtal?" Facit: *Konsulten behåller bakgrundsrättigheter*. Båda modellerna
invänder att frågan byter begrepp ("rättigheter till resultat" vs
"bakgrundsrättigheter"). **Hypotes att pröva mänskligt:** frågemallen återger
fakta-etiketten, inte dokumentets term.

### 2. Modellerna är oense: Q2 7 poster, Q3 46 poster

Q2-oenigheten är koncentrerad till tre mönster (se nedan). Q3-oenigheten är
massiv (46 %) och handlar nästan uteslutande om **hur mycket kontext citatet
behöver** – A kräver att citatet bokstavligen innehåller facitvärdet, B kräver
att citatet också bär *kopplingen* (t.ex. att "Nordvik Teknik AB" är
uppdragsgivaren). Det är en fråga om Q3:s definition, inte om facit som sådant.

### 3. Båda godkänner men egna svaret skiljer sig

Inga fall där båda godkänner (Q2 = ja) men bådas egna svar avviker från facit
och stämmer inbördes. Skillnaderna i pass 1 ligger nästan alltid i **format**
(SEK/kr, ISO-datum vs svenskt datum, "och" vs "samt").

### 4. Evidens otillräcklig trots korrekt svar

A: **7 av 95** poster där modellen godkänner svaret (Q2 = ja) har samtidigt
otillräcklig evidens (Q3 ≠ ja). B: **53 av 96**. Skillnaden är i sig en viktig
signal: samma facit bedöms olika beroende på hur strängt "tillräcklig evidens"
läses, vilket är exakt vad den mänskliga granskningen ska avgöra. Det bekräftar
den tidigare observationen: **ett korrekt värde betyder inte att underlaget
räcker.**

## Kandidatlista för mänsklig granskning (8 poster)

| sample_id | Q2 A/B | Misstänkt mönster |
|-----------|--------|-------------------|
| `cfq-22096908a3be` | nej/ja | "projektledning och analys" vs dokumentets "projektledning samt analysarbete" |
| `cfq-6b820eff9e68` | nej/ja | samma mönster |
| `cfq-89e49f1feafc` | nej/ja | samma mönster |
| `cfq-2218421025ec` | ja/nej | jämförelsefråga: svaret anger talet men inte **vilken** post som är störst |
| `cfq-330989a73d7b` | ja/nej | samma mönster |
| `cfq-a3b0f6a2a220` | ja/nej | samma mönster |
| `cfq-c67b9f72a573` | nej/ja | "Vilken part har rollen client" – dokumentet säger "uppdragsgivare"; roll-ordet finns inte i texten |
| `cfq-eeb92c4957fa` | nej/nej | frågan byter begrepp mot dokumentets term |

Dessa åtta är **hypoteser**, inte verifierade fel. Den mänskliga granskningen
ska koda alla 100 posterna enligt protokollet; kandidatlistan är bara en
prioritering av var fel sannolikt finns.

## Vad detta inte är

- Inte en kvalitetsmätning enligt grindarna: raterna är modeller, en av dem
  dessutom samma modellfamilj som genererade korpusen.
- Inte ett underlag för att ändra generatorn ännu: ändringar först efter den
  mänskliga mätningen och felorsaksklassificeringen (G6).
- Inte reproducerbart bit-för-bit: temperature 0 och fast seed gör körningen
  stabil, men leverantören garanterar inte deterministisk inferens.

## Reproduktion

```bash
export CEREBRAS_API_KEY=...
python benchmarks/ai_review.py \
  --packet examples/qsample_live_100 \
  --output benchmarks/results/ai_review_blind2 \
  --rater-a gpt-oss-120b --rater-b qwen-3.8-27b --seed 2026 --max-tokens 8000

contractforge score-ratings examples/qsample_live_100 \
  --rater-a benchmarks/results/ai_review_blind2/ratings_A_ai.csv \
  --rater-b benchmarks/results/ai_review_blind2/ratings_B_ai.csv \
  --rater-a-kind model --rater-a-name gpt-oss-120b \
  --rater-b-kind model --rater-b-name qwen-3.8-27b
```

**Nästa steg:** den mänskliga granskningen enligt det frysta protokollet, utan
att raterna får se denna rapport i förväg. Därefter jämförs de två mätningarna
per axel (protokollet 7.6) och felorsakerna klassificeras.
