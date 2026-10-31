#!/usr/bin/env bash
# Offline demo: generate, verify and inspect a small dataset with the
# deterministic mock provider. No API key and no network access.
set -euo pipefail

cd "$(dirname "$0")/.."
CONTRACTFORGE="${CONTRACTFORGE:-.venv/bin/contractforge}"
OUTPUT="${1:-dataset}"

if [[ ! -x "$CONTRACTFORGE" ]]; then
  echo "Kör 'make install' först (saknar $CONTRACTFORGE)." >&2
  exit 1
fi

rm -rf "$OUTPUT"
"$CONTRACTFORGE" generate \
  --type consulting --count 2 --clauses 8 --difficulty 2 \
  --language sv --provider mock --seed 2026 \
  --formats txt,markdown,docx,pdf \
  --output "$OUTPUT"
"$CONTRACTFORGE" validate "$OUTPUT"
"$CONTRACTFORGE" stats "$OUTPUT"
echo
echo "Dataset: $OUTPUT"
