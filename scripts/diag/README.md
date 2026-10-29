# Proxy diagnostics

All files in this directory are committed to the repository.

Offline repro/bisect kit for gateway 400s. Survivable across reboots
(unlike /tmp). All scripts are stdlib-only and take `--help`.

- `replay_turn.py ROLLOUT [ORD_CAP] [...]` - rebuild a turn's
  `/v1/responses` request from a rollout jsonl and POST it to the proxy.
  Flags: `--strip-reasoning`, `--drop-orphans`, `--max-tokens`,
  `--model`, `--port`, `--stream`, `--timeout`.
- `probe.py ROLLOUT SPEC [...]` - bisect aid. Units keep each
  function_call with its output (no orphan noise). `SPEC` like
  `354-531,620-709`; `--repeat N` for flake detection; `--list-units`
  dumps the unit table without touching the network.
- `direct_upstream.py ROLLOUT [ORD_CAP]` - same bytes straight to
  `opencode.ai` (needs `$OPENCODE_GO_API_KEY`). Proxy-vs-gateway blame
  isolation: identical verdicts mean the proxy is innocent.
- `chat_replay.py ROLLOUT [ORD_CAP]` - translate to chat-completions via
  the proxy's own translator, replay verbatim on `/v1/chat/completions`.
- `tools_smoke.py` - chat-family model + function tools (the proxy merges
  its codex_app snapshot, like real traffic). Catches tool-schema 400s.
- `restart_proxy.sh [PORT]` - delayed self-restart (default 19999).
  **Always restart this way**: your session travels through the proxy, so
  a synchronous kill would sever the caller's own connection. Returns
  immediately; progress in `/tmp/restart-proxy.log`, proxy log in
  `/tmp/proxy19999.log`.

Rollout files live under `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
(newest last). Ordinal caps come from the `ordinal` field in rows.
