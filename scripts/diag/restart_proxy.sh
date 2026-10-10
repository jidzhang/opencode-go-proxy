#!/usr/bin/env bash
# Delayed self-restart for the loopback proxy.
#
# WHY DELAYED: Codex sessions themselves travel through this proxy, so killing
# it synchronously would sever the very connection delivering this command's
# result. This script returns immediately; the kill happens after DELAY_SECS,
# then it waits for the port to free, starts a fresh proxy detached, and waits
# for /health. Safe to invoke and forget; watch progress in RESTART_LOG.
#
# Usage: restart_proxy.sh [PORT]   (default 19999)
set -u
PORT="${1:-19999}"
DELAY_SECS=3
REPO=/home/dc/github/opencode-go-proxy
RESTART_LOG=/tmp/restart-proxy.log
PROXY_LOG=/tmp/proxy19999.log
{
  echo "--- restart requested at $(date -u +%FT%TZ) port=$PORT pid=$$ ---"
  sleep "$DELAY_SECS"
  # [o] trick: match target cmdlines but never our own invoker line.
  pkill -f "[o]pencode-go-proxy --bind 127.0.0.1 --port ${PORT}" 2>/dev/null
  for _ in $(seq 1 50); do
    if ! curl -s -m 1 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then break; fi
    sleep 0.2
  done
  cd "$REPO" || exit 1
  # shellcheck disable=SC2086
  setsid nohup uv run --frozen opencode-go-proxy --bind 127.0.0.1 --port "$PORT" \
    >>"$PROXY_LOG" 2>&1 < /dev/null &
  for _ in $(seq 1 40); do
    if curl -s -m 1 "http://127.0.0.1:${PORT}/health" | grep -q '"status":"ok"'; then
      echo "restarted ok port=$PORT at $(date -u +%FT%TZ)"
      exit 0
    fi
    sleep 0.5
  done
  echo "restart FAILED: no health on $PORT"
  exit 1
} >>"$RESTART_LOG" 2>&1
