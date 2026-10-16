#!/usr/bin/env bash
# Delayed self-restart for the loopback proxy (thin wrapper around
# restart-proxy-19999.sh at the repo root).
#
# Usage: restart_proxy.sh [PORT]   (default 19999)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
OPENCODE_GO_PROXY_PORT="${1:-${OPENCODE_GO_PROXY_PORT:-19999}}" \
  exec "$REPO/restart-proxy-19999.sh"
