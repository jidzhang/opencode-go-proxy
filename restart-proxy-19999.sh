#!/usr/bin/env bash
set -euo pipefail
# One-shot restart for the 19999 proxy (macOS/Linux twin of
# restart-proxy-19999.bat). This always starts the proxy detached with stdio
# redirected to a log file, so closing the terminal afterwards does not stop
# it. Linux uses setsid(1); macOS has no setsid, so there nohup(1) alone
# detaches the process (it ignores SIGHUP and holds no tty).
#
# WHY DELAYED: Claude Code / Codex sessions may themselves travel through this
# proxy, so killing it synchronously could sever the very connection
# delivering this command's result. The kill happens after DELAY_SECS.
#
# Usage:
#   chmod +x restart-proxy-19999.sh
#   ./restart-proxy-19999.sh
# Requires: uv + OPENCODE_GO_API_KEY env var
# Session state lives in the client, not here: a request sent while the
# proxy is down fails once; resend after health turns 200.

PROXY_BIND="${OPENCODE_GO_PROXY_BIND:-127.0.0.1}"
PROXY_PORT="${OPENCODE_GO_PROXY_PORT:-19999}"
# Log verbosity switch: warn | info (debugging period) | debug. See
# start-proxy-19999.sh for what each level adds; flip to warn when it wraps up.
export OPENCODE_GO_PROXY_LOG_LEVEL="${OPENCODE_GO_PROXY_LOG_LEVEL:-info}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="/tmp/proxy${PROXY_PORT}.log"
RESTART_LOG="/tmp/restart-proxy${PROXY_PORT}.log"
PID_FILE="/tmp/proxy${PROXY_PORT}.pid"
DELAY_SECS=3
HEALTH_URL="http://${PROXY_BIND}:${PROXY_PORT}/health"
# [o] trick: match the target cmdline but never this script's own lines.
MATCH="[o]pencode-go-proxy --bind ${PROXY_BIND} --port ${PROXY_PORT}"

if [[ -z "${OPENCODE_GO_API_KEY:-}" ]]; then
  echo "[restart] OPENCODE_GO_API_KEY is not set." >&2
  echo "Export it first, e.g.:" >&2
  echo '  export OPENCODE_GO_API_KEY="your-key-here"' >&2
  exit 1
fi

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
  echo "[restart] uv is not on PATH." >&2
  echo "Install it (https://docs.astral.sh/uv/) or add its bin directory to PATH." >&2
  exit 1
fi

health_ok() {
  curl -s -m 1 "$HEALTH_URL" 2>/dev/null | grep -q '"status":"ok"'
}

# Detach from the terminal: setsid where available, nohup elsewhere. The
# trailing disown keeps this shell from reaping/HUPing the child on exit.
detach() {
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$@" >>"$LOG_FILE" 2>&1 </dev/null &
  else
    nohup "$@" >>"$LOG_FILE" 2>&1 </dev/null &
  fi
  disown 2>/dev/null || true
}

echo "[restart] stopping any ${PROXY_PORT} listener in ${DELAY_SECS}s, then starting a detached one ..."
# Run the delayed stop/start in a subshell so `exit` there cannot terminate the
# parent, then wait for it: the parent reports the real outcome instead of
# racing the old instance or the start delay with an immediate health check.
(
  sleep "$DELAY_SECS"

  echo "--- restart requested at $(date -u +%FT%TZ) port=$PROXY_PORT ---"
  echo "step 1/3: stopping the old instance ..."
  pkill -f "$MATCH" 2>/dev/null || true
  for _ in $(seq 1 50); do
    health_ok || break
    sleep 0.2
  done
  if health_ok; then
    echo "old instance ignored SIGTERM; sending SIGKILL ..."
    pkill -9 -f "$MATCH" 2>/dev/null || true
    sleep 1
  fi
  if health_ok; then
    echo "restart FAILED: ${PROXY_BIND}:${PROXY_PORT} is still held by a process this script cannot stop"
    exit 1
  fi

  # Restart-time log rotation: the proxy appends stderr here for its whole
  # lifetime, so cap it at 5 generations on every restart.
  if [[ -f "$LOG_FILE" ]]; then
    rm -f "${LOG_FILE}.5"
    for n in 4 3 2 1; do
      [[ -f "${LOG_FILE}.${n}" ]] && mv "${LOG_FILE}.${n}" "${LOG_FILE}.$((n + 1))"
    done
    mv "$LOG_FILE" "${LOG_FILE}.1"
  fi

  echo "step 2/3: starting detached instance ..."
  cd "$REPO_DIR"
  # --frozen keeps uv from rewriting uv.lock on every start.
  detach "$UV_BIN" run --frozen opencode-go-proxy --bind "${PROXY_BIND}" --port "${PROXY_PORT}"

  echo "step 3/3: waiting for /health ..."
  for _ in $(seq 1 60); do
    if health_ok; then
      pgrep -f "$MATCH" | head -1 >"$PID_FILE" || true
      echo "restarted ok port=$PROXY_PORT at $(date -u +%FT%TZ) pid=$(cat "$PID_FILE" 2>/dev/null || echo '?')"
      exit 0
    fi
    sleep 0.5
  done
  echo "restart FAILED: no health on $PROXY_PORT"
  tail -n 20 "$LOG_FILE" 2>/dev/null || true
  exit 1
) >>"$RESTART_LOG" 2>&1 &
RESTART_PID=$!

if wait "$RESTART_PID"; then
  echo "[restart] done: ${HEALTH_URL%/health} is up (detached; safe to close this terminal)."
  echo "[restart] proxy log: ${LOG_FILE}   restart log: ${RESTART_LOG}"
else
  echo "[restart] FAILED: /health never returned 200. See ${RESTART_LOG}." >&2
  exit 1
fi
