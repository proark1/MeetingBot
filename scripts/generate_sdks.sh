#!/usr/bin/env bash
# Regenerate the typed Python and JavaScript/TypeScript SDKs from the
# committed `api/openapi.json` snapshot.
#
# Output:
#   sdk/python/generated/         — full openapi-python-client package
#   sdk/js/generated/             — full openapi-typescript-codegen client
#   sdk/js/src/openapi-types.ts   — types-only file imported by the hand-
#                                   written client at sdk/js/src/index.ts
#
# Requirements:
#   - python 3.11+ with pip
#   - node 18+ with npx
#
# Run from the repo root:
#   bash scripts/generate_sdks.sh
#
# CI runs the same generators against the live server in
# .github/workflows/sdk-gen.yml; this script is the local equivalent and
# uses the committed schema so it works offline.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCHEMA="$REPO_ROOT/api/openapi.json"

if [ ! -f "$SCHEMA" ]; then
  echo "ERROR: $SCHEMA not found. Run 'python scripts/generate_openapi.py' first." >&2
  exit 1
fi

echo "Regenerating SDKs from $(basename "$SCHEMA") ($(wc -c < "$SCHEMA") bytes)"

# ── Python SDK (typed httpx client) ───────────────────────────────────────────
echo
echo "── Python: openapi-python-client → sdk/python/generated/ ──"
python -m pip install --quiet --upgrade "openapi-python-client>=0.21,<0.22"
rm -rf "$REPO_ROOT/sdk/python/generated"
(
  cd "$REPO_ROOT/sdk/python"
  openapi-python-client generate \
    --path "$SCHEMA" \
    --output-path generated \
    --overwrite \
    --meta=none
)

# ── TypeScript types-only file ────────────────────────────────────────────────
echo
echo "── TypeScript: openapi-typescript → sdk/js/src/openapi-types.ts ──"
command -v npx >/dev/null 2>&1 || { echo "ERROR: npx not on PATH" >&2; exit 1; }
npx --yes openapi-typescript "$SCHEMA" \
  --output "$REPO_ROOT/sdk/js/src/openapi-types.ts"

# ── Full TypeScript client (optional) ─────────────────────────────────────────
echo
echo "── TypeScript: openapi-typescript-codegen → sdk/js/generated/ ──"
rm -rf "$REPO_ROOT/sdk/js/generated"
npx --yes openapi-typescript-codegen \
  --input "$SCHEMA" \
  --output "$REPO_ROOT/sdk/js/generated" \
  --client fetch \
  --useUnionTypes

echo
echo "Done."
echo "  - Python:     sdk/python/generated/"
echo "  - TS types:   sdk/js/src/openapi-types.ts"
echo "  - TS client:  sdk/js/generated/"
echo
echo "The hand-written SDKs at sdk/python/meetingbot/ and sdk/js/src/index.ts"
echo "are still the supported entry points; generated output is meant for"
echo "type checking and as a reference."
