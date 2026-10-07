#!/usr/bin/env bash
# Deterministic tester-army/e2e tests against an isolated real demo server.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
export E2E_TELEMETRY_DISABLED=1
export PYTHON="${PYTHON:-.venv/bin/python}"
export CHANAPP_E2E_OUTPUT=.e2e
# Keep auxiliary evidence beside the report even for concurrent targeted runs.
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[i]}" in
    --output) CHANAPP_E2E_OUTPUT="${args[i+1]:-.e2e}" ;;
    --output=*) CHANAPP_E2E_OUTPUT="${args[i]#--output=}" ;;
  esac
done
# Same boundary as the runner: evidence stays below this checkout.
node -e 'const p = require("node:path"), r = p.resolve(process.env.CHANAPP_E2E_OUTPUT), base = process.cwd(); if (!r.startsWith(base + p.sep)) process.exit(2)'
export CHANAPP_E2E_ROOT
CHANAPP_E2E_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/chanapp-e2e.XXXXXX")"
cleanup() {
  mkdir -p "$CHANAPP_E2E_OUTPUT/logs"
  if [[ -f "$CHANAPP_E2E_ROOT/outbound.jsonl" ]]; then
    cp "$CHANAPP_E2E_ROOT/outbound.jsonl" "$CHANAPP_E2E_OUTPUT/logs/outbound.jsonl"
  fi
  rm -rf "$CHANAPP_E2E_ROOT"
}
trap cleanup EXIT
status=0
npx --no-install e2e run "$@" || status=$?
if [[ -s "$CHANAPP_E2E_ROOT/outbound.jsonl" ]]; then
  echo "FAIL: demo server attempted external transport (see $CHANAPP_E2E_OUTPUT/logs/outbound.jsonl)" >&2
  status=1
fi
exit "$status"
