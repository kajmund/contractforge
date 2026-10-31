# Exempel

- `pricing.example.json` – formatet för en explicit `PricingTable`. Ersätt
  nollorna med leverantörens aktuella priser; ContractForge hårdkodar inga
  priser. Används med `CONTRACTFORGE_PRICING` eller `--pricing`.
- `sample_live_consulting/` – ett **live-genererat** exempeldataset med två
  konsultavtal från Cerebras `gpt-oss-120b` (promptversion v1, seed 2026).
  Innehåller scenario, kanoniska texter, DOCX/PDF, facit, golden-frågor,
  manifest, verifieringsresultat och råsvar. Validera med:
  `contractforge validate examples/sample_live_consulting`.
- `offline_demo.sh` – genererar och verifierar ett litet dataset med den
  deterministiska mock-providern (ingen API-nyckel, inga nätverksanrop).
  Skriver till `./dataset` (gitignorerat).

## Snabb demo

```bash
./examples/offline_demo.sh
```
