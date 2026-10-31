# Cerebras-provider

ContractForge använder en utbytbar provideradapter. Cerebras är den första
implementationen; generatorns övriga kod känner bara till `Provider`-protokollet
i `src/contractforge/providers/base.py`.

```
Provider (Protocol)
├── CerebrasProvider   # OpenAI-kompatibel /chat/completions
└── MockProvider       # deterministisk offline-provider för tester/CI
```

## Konfiguration

| Variabel | Default | Beskrivning |
|----------|---------|-------------|
| `CEREBRAS_API_KEY` | – | Krävs för `--provider cerebras`. Loggas aldrig. |
| `CEREBRAS_BASE_URL` | `https://api.cerebras.ai/v1` | OpenAI-kompatibel bas-URL. |
| `CONTRACTFORGE_MODEL` | `gpt-oss-120b` | Modell-id som skickas till providern. |
| `CONTRACTFORGE_TIMEOUT_S` | `180` | HTTP-timeout per anrop. |
| `CONTRACTFORGE_MAX_RETRIES` | `4` | Antal retries vid 429/5xx/timeout. |
| `CONTRACTFORGE_BACKOFF_S` | `1.5` | Bas för exponentiell backoff (med jitter). |
| `CONTRACTFORGE_CONCURRENCY` | `4` | Antal avtal som genereras parallellt. |
| `CONTRACTFORGE_TEMPERATURE` | `0.7` | Genereringstemperatur. |
| `CONTRACTFORGE_TOP_P` | – | Valfri nucleus-sampling. |
| `CONTRACTFORGE_MAX_TOKENS` | `4000` | Max antal output-tokens per anrop. |
| `CONTRACTFORGE_MAX_REPAIRS` | `2` | Antal reparationsförsök per avtal. |
| `CONTRACTFORGE_PRICING` | – | Sökväg till explicit `PricingTable` (JSON). |

`Settings` är enda källan till konfiguration. CLI-flaggor (`--model`,
`--concurrency`, `--temperature`, `--max-tokens`, `--max-repairs`, `--pricing`)
överrider miljön för en körning.

## Vad adaptern gör

- Skickar `POST {base_url}/chat/completions` med `model`, `messages`,
  `temperature`, `max_tokens`, `top_p` och `seed` när de är satta.
- Sätter `response_format = {"type": "json_object"}` när prompten begär JSON.
- Mäter latens och plockar ut `usage` (`prompt_tokens`, `completion_tokens`,
  `total_tokens`) samt `finish_reason`.
- Retryar 429, 5xx, timeout och nätverksfel med exponentiell backoff och jitter;
  respekterar `Retry-After` när servern skickar den.
- Sparar råsvar för felsökning (`raw/<call_id>.json` i datasetet). Råsvaret
  innehåller aldrig API-nyckeln.
- Kastar `ProviderError` efter uttömda retries, `ConfigError` om nyckeln saknas
  när providern skapas och `GenerationError` om modellens JSON inte kan användas.

## Promptversioner

Promptarna ligger i `src/contractforge/generation/prompts/<version>/` och
exponerar `PROMPT_VERSION` samt `prompt_sha256()`. Manifestet per avtal sparar
både version och hash, så en körning kan alltid knytas till exakt den prompt som
användes.

## Kostnad

ContractForge hårdkodar inga priser och gör inga antaganden om en viss
Cerebras-modell. Kostnad rapporteras endast om du anger en prisuppgift:

```json
{
  "providers": {
    "cerebras": {
      "gpt-oss-120b": {
        "input_usd_per_million": 0.0,
        "output_usd_per_million": 0.0
      }
    }
  }
}
```

```bash
CONTRACTFORGE_PRICING=./pricing.json contractforge generate ...
```

Sätt `input_usd_per_million` / `output_usd_per_million` till leverantörens
aktuella priser. Utan fil rapporteras `cost_usd = null` och
`cost_basis = "unknown"` – aldrig en påhittad siffra.

## Live-test

```bash
export CEREBRAS_API_KEY="csk-..."
.venv/bin/python -m pytest -m live -q
```

Live-testerna är avstängda i CI. En seed reproducerar scenario och
konfiguration, men LLM-texten är inte garanterat bit-identisk eftersom
leverantören inte garanterar deterministisk inferens.
