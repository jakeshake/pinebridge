#!/usr/bin/env bash
# Quick manual sanity check across the whole stack. Run from the Unraid
# host or any machine on the same network.
set -euo pipefail

BRIDGE_HOST="${1:-localhost}"
BRIDGE_PORT="${2:-5000}"

echo "== pinebridge-bridge /health =="
curl -fsS "http://${BRIDGE_HOST}:${BRIDGE_PORT}/health" | tee /dev/stderr | grep -q '"status": *"healthy"' \
  && echo "OK" || echo "FAILED -- is the pinebridge-bridge container running?"

echo
echo "== pinebridge-bridge /test =="
curl -fsS "http://${BRIDGE_HOST}:${BRIDGE_PORT}/test"
echo
