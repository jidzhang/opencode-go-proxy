#!/usr/bin/env bash
set -euo pipefail
# opencode-go-proxy local run: loopback-only on 127.0.0.1:19999
#
# Usage:
#   ./start-proxy-19999.sh              # start detached, wait for /health, return
#   ./start-proxy-19999.sh --foreground # stay attached (Ctrl-C to stop)
#   ./start-proxy-19999.sh --detach     # explicit detached form
# Requires: uv + OPENCODE_GO_API_KEY env var

PROXY_BIND="${OPENCODE_GO_PROXY_BIND:-127.0.0.1}"
PROXY_PORT="${OPENCODE_GO_PROXY_PORT:-19999}"
# Log verbosity switch: warn | info | debug.
#   warn  = quiet: only retries/failures/degradation reach the log
#   info  = + lifecycle events (server start/stop, credential source, fallbacks)
#   debug = + full per-request telemetry (received/converted, upstream start/done)
# Debugging period runs on info; flip to warn when it wraps up.
export OPENCODE_GO_PROXY_LOG_LEVEL="${OPENCODE_GO_PROXY_LOG_LEVEL:-info}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="/tmp/proxy${PROXY_PORT}.log"
PID_FILE="/tmp/proxy${PROXY_PORT}.pid"
HEALTH_URL="http://${PROXY_BIND}:${PROXY_PORT}/health"
# [o] trick: match the target cmdline but never this script's own lines.
MATCH="[o]pencode-go-proxy --bind ${PROXY_BIND} --port ${PROXY_PORT}"

MODE=detach
while [[ $# -gt 0 ]]; do
  case "$1" in
    -f | --foreground) MODE=foreground ;;
    -d | --detach) MODE=detach ;;
    -h | --help)
      cat <<'USAGE'
Usage: start-proxy-19999.sh [--foreground|--detach]

  --detach, -d      start in the background and wait for /health (default)
  --foreground, -f  run attached to this terminal (Ctrl-C to stop)
USAGE
      exit 0
      ;;
    *)
      echo "[start] unknown argument: $1 (try --help)" >&2
      exit 2
      ;;
  esac
  shift
done

if [[ -z "${OPENCODE_GO_API_KEY:-}" ]]; then
  echo "[input] OPENCODE_GO_API_KEY is not set." >&2
  echo "Export it first, e.g.:" >&2
  echo '  export OPENCODE_GO_API_KEY="your-key-here"' >&2
  exit 1
fi

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
  echo "[start] uv is not on PATH." >&2
  echo "Install it (https://docs.astral.sh/uv/) or add its bin directory to PATH." >&2
  exit 1
fi

echo "[proxy] bind=${PROXY_BIND} port=${PROXY_PORT}"
echo "[proxy] health: http://${PROXY_BIND}:${PROXY_PORT}/health"
echo "[proxy] models: http://${PROXY_BIND}:${PROXY_PORT}/v1/models"
echo

cd "$REPO_DIR"
# --frozen keeps uv from rewriting uv.lock on every start.
if [[ "$MODE" == foreground ]]; then
  exec "$UV_BIN" run --frozen opencode-go-proxy --bind "${PROXY_BIND}" --port "${PROXY_PORT}"
fi

health_ok() {
  curl -s -m 1 "$HEALTH_URL" 2>/dev/null | grep -q '"status":"ok"'
}

if health_ok; then
  echo "[start] already running on ${PROXY_BIND}:${PROXY_PORT}; nothing to do."
  exit 0
fi

# Start-time log rotation, mirroring restart-proxy-19999.sh: the proxy appends
# stderr here for its whole lifetime, so cap it at 5 generations.
if [[ -f "$LOG_FILE" ]]; then
  rm -f "${LOG_FILE}.5"
  for n in 4 3 2 1; do
    [[ -f "${LOG_FILE}.${n}" ]] && mv "${LOG_FILE}.${n}" "${LOG_FILE}.$((n + 1))"
  done
  mv "$LOG_FILE" "${LOG_FILE}.1"
fi

# Detach from the terminal: setsid where available (Linux), nohup elsewhere
# (macOS has no setsid). The proxy ignores SIGHUP and holds no tty.
if command -v setsid >/dev/null 2>&1; then
  setsid nohup "$UV_BIN" run --frozen opencode-go-proxy --bind "${PROXY_BIND}" --port "${PROXY_PORT}" \
    >>"$LOG_FILE" 2>&1 </dev/null &
else
  nohup "$UV_BIN" run --frozen opencode-go-proxy --bind "${PROXY_BIND}" --port "${PROXY_PORT}" \
    >>"$LOG_FILE" 2>&1 </dev/null &
fi
disown 2>/dev/null || true

for _ in $(seq 1 60); do
  if health_ok; then
    pgrep -f "$MATCH" | head -1 >"$PID_FILE" || true
    echo "[start] up: ${HEALTH_URL%/health} (detached; safe to close this terminal)."
    echo "[start] proxy log: ${LOG_FILE}   pid: $(cat "$PID_FILE" 2>/dev/null || echo '?')"
    exit 0
  fi
  sleep 0.5
done

echo "[start] FAILED: /health never returned 200. See ${LOG_FILE}." >&2
tail -n 20 "$LOG_FILE" 2>/dev/null || true
exit 1
