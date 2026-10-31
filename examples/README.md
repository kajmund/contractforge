# Exempel

- `pricing.example.json` – formatet för en explicit `PricingTable`. Ersätt
  nollorna med leverantörens aktuella priser; ContractForge hårdkodar inga
  priser. Används med `CONTRACTFORGE_PRICING` eller `--pricing`.
- `sample_live_consulting/` – ett **live-genererat** exempeldataset med två
  konsultavtal från Cerebras `gpt-oss-120b` (promptversion v1, seed 2026).
  Innehåller scenario, kanoniska texter, DOCX/PDF, facit, golden-frågor,
  manifest, verifieringsresultat och råsvar. Validera med:
  `contractforge validate examples/sample_live_consulting`.
- `qsample_live_100/` – **mätpaket för oberoende kvalitetsbedömning** av facit:
  100 stratifierade frågor från live-datasetet (källa
  `cerebras_consulting_100.zip`, SHA-256 `43448450…ae7e`, urvals-seed 2026,
  `selection_sha256` `1272bd8a…f650`). `ratings.csv` är blind för rater;
  `ratings_A.csv` och `ratings_B.csv` är raternas blanketter (olika ordning,
  hashar i `raters.json`); `manifest.json` och `selection.json` är koordinatorns
  filer. Protokoll och grindar:
  [`docs/quality-protocol.md`](../docs/quality-protocol.md). Resultatet beräknas
  med `contractforge score-ratings <katalog> --rater-a … --rater-b …`.
  Skapa ett nytt urval med
  `contractforge publish-questions <dataset.zip> --count 100 --seed <seed> --output <dir>`.
- `offline_demo.sh` – genererar och verifierar ett litet dataset med den
  deterministiska mock-providern (ingen API-nyckel, inga nätverksanrop).
  Skriver till `./dataset` (gitignorerat).

## Snabb demo

```bash
./examples/offline_demo.sh
```
