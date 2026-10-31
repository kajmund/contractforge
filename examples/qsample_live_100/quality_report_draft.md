# Kvalitetsbedömning av ContractForge-facit

- **Datum:** 2026-10-31T17:28:19.564970+00:00
- **Bedömningens art:** modellbaserad oberoende bedömning
- **Källa:** `examples/qsample_live_100`
- **Urval:** 100 av 1723 frågor, `selection_sha256` `1272bd8a819f1b39bfe6b17225da64635dad50854f42ad66be8665886702f650`
- **Dataset:** `source_sha256` `4344845099a39dbdc7d09c54f6d50cf03e74d1f831c78eda34cb67182893ae7e`
- **Ratare:** A = model (gpt-oss-120b), B = model (qwen-3.8-27b)
- **Blanketter olika ordnade:** True
- **Status:** measured – gates not all met

Modellbaserad bedömning – inte mänsklig juridisk granskning. Två modeller kan dela systematiska fel.

## Resultat per axel

| Axel | N | ja | nej | osäker | andel ja | Wilson 95 % | Grind |
|------|--:|---:|----:|-------:|---------:|-------------|-------|
| Q1 | 100 | 97 | 1 | 2 | 97.0% | 91.5% – 99.0% | uppfylld |
| Q2 | 100 | 92 | 1 | 7 | 92.0% | 85.0% – 95.9% | ej uppfylld |
| Q3 | 100 | 45 | 9 | 46 | 45.0% | 35.6% – 54.8% | ej uppfylld |
| Q4 | 0 | 0 | 0 | 0 | – | – | ej bedömd |

## Fullständighet (G7)

- Urval: 100 frågor; okodade: 0
- Alla poster kodade av båda raterna: ja

## Överensstämmelse per axel (ursprungliga individuella koder)

| Axel | Parvisa koder | Eniga | P₀ | κ | Grind |
|------|--------------:|------:|---:|--:|-------|
| Q1 | 100 | 98 | 98.0% | 0.49 | ej uppfylld |
| Q2 | 100 | 93 | 93.0% | 0.19 | ej uppfylld |
| Q3 | 100 | 54 | 54.0% | 0.14 | ej uppfylld |
| Q4 | 100 | 100 | 100.0% | – | ej uppfylld |

## Grindar

- **Alla grindar uppfyllda:** nej
- G-Q2: nej — punkt 92.0% < 95%; Wilson nedre 85.0% < 90% (n=100)
- G-Q3: nej — punkt 45.0% < 90%; Wilson nedre 35.6% < 85% (n=100)
- G-Q1: ja — –
- G-Q4: nej — för få tillämpliga poster (n=0 < 10)
- G5-Q2: nej — kappa 0.19 < 0.70
- G5-Q1: nej — kappa 0.49 < 0.60
- G5-Q3: nej — enighet 54.0% < 80%; kappa 0.14 < 0.60
- G5-Q4: nej — kappa kan inte beräknas

## Oenighet

- Oeniga poster totalt: 55
- Poster där alla tre svar olika: 0
- Q4: felavvikelse 1, gränsfall 0, faktaoenighet 1
- Q4: felavvikelse 2, gränsfall 0, faktaoenighet 5
- Q4: felavvikelse 1, gränsfall 0, faktaoenighet 45
- Q4: felavvikelse 0, gränsfall 0, faktaoenighet 0

## Felorsaker

| Orsak | Antal |
|-------|------:|
| `frågeformulering` | 0 |
| `facitvärde` | 0 |
| `evidensspann` | 0 |
| `validatorlucka` | 0 |
| `genereringsfel` | 0 |
| `juridisk_tolkning` | 0 |

- Klassificerade poster: 0
- Oklassificerade felposter: 61

## Populationsskattning

Ej beräknad: Ingen populationsfil angavs (--population). En viktad skattning kräver stratumstorlekarna i hela populationen; urvalsvärdet gäller urvalet.

## Vad mätningen inte bevisar

- Resultatet gäller det publicerade urvalet, inte alla avtalstyper eller nivåer.
- Modellbaserad bedömning – inte mänsklig juridisk granskning. Två modeller kan dela systematiska fel.
