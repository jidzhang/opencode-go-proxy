#!/usr/bin/env bash
set -euo pipefail
# opencode-go-proxy local run: loopback-only on 127.0.0.1:19999
# Usage:
#   chmod +x start-proxy-19999.sh
#   ./start-proxy-19999.sh
# Requires: uv + OPENCODE_GO_API_KEY env var

PROXY_BIND="${OPENCODE_GO_PROXY_BIND:-127.0.0.1}"
PROXY_PORT="${OPENCODE_GO_PROXY_PORT:-19999}"

if [[ -z "${OPENCODE_GO_API_KEY:-}" ]]; then
  echo "[input] OPENCODE_GO_API_KEY is not set." >&2
  echo "Export it first, e.g.:" >&2
  echo '  export OPENCODE_GO_API_KEY="your-key-here"' >&2
  exit 1
fi

echo "[proxy] bind=${PROXY_BIND} port=${PROXY_PORT}"
echo "[proxy] health: http://${PROXY_BIND}:${PROXY_PORT}/health"
echo "[proxy] models: http://${PROXY_BIND}:${PROXY_PORT}/v1/models"
echo

cd "$(dirname "$0")"
# --frozen keeps uv from rewriting uv.lock on every start.
exec uv run --frozen opencode-go-proxy --bind "${PROXY_BIND}" --port "${PROXY_PORT}"
