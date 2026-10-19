#!/usr/bin/env bash
set -euo pipefail
# One-shot restart for the 19999 proxy (Linux twin of restart-proxy-19999.bat).
# Unlike start-proxy-19999.sh, which runs the proxy in the foreground and dies
# with the terminal, this starts the proxy in its own session (setsid) with
# SIGHUP ignored (nohup) and stdio redirected to a log file, so closing the
# terminal afterwards does not stop it.
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

if [[ -z "${OPENCODE_GO_API_KEY:-}" ]]; then
  echo "[restart] OPENCODE_GO_API_KEY is not set." >&2
  echo "Export it first, e.g.:" >&2
  echo '  export OPENCODE_GO_API_KEY="your-key-here"' >&2
  exit 1
fi

echo "[restart] stopping any ${PROXY_PORT} listener in ${DELAY_SECS}s, then starting a detached one ..."
# Subshell on purpose: `exit` inside must not terminate this script, which
# still owes the terminal a health verdict below.
(
  sleep "$DELAY_SECS"

  echo "--- restart requested at $(date -u +%FT%TZ) port=$PROXY_PORT ---"
  echo "step 1/3: stopping the old instance ..."
  # [o] trick: match the target cmdline but never this script's own lines.
  pkill -f "[o]pencode-go-proxy --bind ${PROXY_BIND} --port ${PROXY_PORT}" 2>/dev/null || true
  for _ in $(seq 1 50); do
    if ! curl -s -m 1 "http://${PROXY_BIND}:${PROXY_PORT}/health" >/dev/null 2>&1; then
      break
    fi
    sleep 0.2
  done

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
  setsid nohup uv run --frozen opencode-go-proxy --bind "${PROXY_BIND}" --port "${PROXY_PORT}" \
    >>"$LOG_FILE" 2>&1 < /dev/null &

  echo "step 3/3: waiting for /health ..."
  for _ in $(seq 1 60); do
    if curl -s -m 1 "http://${PROXY_BIND}:${PROXY_PORT}/health" | grep -q '"status":"ok"'; then
      pgrep -f "[o]pencode-go-proxy --bind ${PROXY_BIND} --port ${PROXY_PORT}" | head -1 >"$PID_FILE" || true
      echo "restarted ok port=$PROXY_PORT at $(date -u +%FT%TZ) pid=$(cat "$PID_FILE" 2>/dev/null || echo '?')"
      exit 0
    fi
    sleep 0.5
  done
  echo "restart FAILED: no health on $PROXY_PORT"
  exit 1
) >>"$RESTART_LOG" 2>&1

if curl -s -m 1 "http://${PROXY_BIND}:${PROXY_PORT}/health" | grep -q '"status":"ok"'; then
  echo "[restart] done: http://${PROXY_BIND}:${PROXY_PORT} is up (detached; safe to close this terminal)."
  echo "[restart] proxy log: ${LOG_FILE}   restart log: ${RESTART_LOG}"
else
  echo "[restart] FAILED: /health never returned 200. See ${RESTART_LOG}." >&2
  exit 1
fi
