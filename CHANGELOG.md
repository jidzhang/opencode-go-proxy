# Changelog

## Unreleased

### Removed

- The vision caption engine and its "cheapest image-capable model" auto-pick.
  Image turns now keep the requested model end to end; a model that cannot
  accept images surfaces its own upstream error instead of being silently
  re-routed to a substitute. The `CODEX_IMAGE_MODEL`,
  `OPENCODE_GO_PROXY_CAPTION_MODEL`, and `OPENCODE_GO_PROXY_VISION_LOCAL_*`
  variables no longer exist.
- The default model. A request that omits `model` is rejected with
  `400 model must be a non-empty string` on every surface; `/state` reports
  the most recently used model or `null` instead of a hardcoded slug. The
  `deepseek-v4.1-flash` constant was deprecated upstream and every omitted-model
  request was silently billed against it.
- The dead translation path: `handle_responses_request`,
  `handle_streaming_request`, `handle_chat_stream_passthrough`,
  `call_upstream_chat`, and `call_upstream_chat_verbatim` had no production
  caller but carried the go-reject-to-zen re-dispatch, the two-strike picker
  auto-hide, and a 4xx-to-502 relabelling. Also removed the proxy's writers
  for `model-picker.json` (a user-owned file); an existing file is still
  honoured at render time.
- Cross-vendor reasoning-level clamping: Go models' declared
  `supported_reasoning_levels` are forwarded verbatim instead of being
  intersected with the native Codex effort vocabulary.
- Catalog fabrication: unknown `context_window` / `max_context_window` stay
  `null` instead of asserting 1,000,000, `auto_compact_token_limit` is only
  computed when the window is known, zen entries no longer carry a synthesized
  description, and the seed no longer ships "Fast, cheap. Best default for
  everyday coding." style claims the repo has no pricing data to support.
- The "(no summary available)" compaction placeholder: a summarization call
  that returns no text now surfaces an explicit error instead of writing a
  vacuous checkpoint the next model would resume from.

### Changed

- Provider usage numbers reach the client untouched. The zero-input estimate
  is still surfaced, but only as an extra `estimatedInputTokens` field; it no
  longer overwrites `input_tokens` / `total_tokens`.
- An upstream `finish_reason` other than `stop`/`tool_calls` now reports
  `status: "incomplete"` instead of every answer claiming `completed`.
- Structured output survives translation: `text.format` maps to
  `response_format`, and `stop`, `seed`, `presence_penalty`,
  `frequency_penalty`, `logprobs`, `top_logprobs`, `parallel_tool_calls`, and
  `metadata` are forwarded instead of silently dropped.
- Assistant text between a tool call and its output is preserved in the
  translated history (previously discarded).
- Compaction transcripts include tool calls and their results instead of
  flattening them away.
- Tool-loop turns keep their thinking budget rules visible: the proxy default
  `max_tokens` is still raised to fit a thinking budget, but an explicit
  caller cap is never inflated, and mid-loop thinking suppression plus every
  cap adjustment emit trace events.
- The Gemini `"Continue."` trailing user turn stays (the Go Google gateway
  rejects those tail shapes outright) but is now traced and can be disabled
  with `OPENCODE_GO_PROXY_GEMINI_CONTINUE_TAIL=0`.
- Upstream 5xx statuses are forwarded verbatim on the non-stream family path
  instead of being relabelled 502.
- The update check, the release anchors, the macOS menu bar source pin, and
  the systemd unit now point at this repository instead of the upstream
  project this fork came from. The menu bar no longer hardcodes
  `--chat-base-url`, so `OPENCODE_GO_BASE_URL` and friends are honoured, and
  the update confirmation shows the exact install source before restarting.
- `opencode-go-proxy smoke-test` requires `--model <slug>` (or
  `OPENCODE_GO_PROXY_SMOKE_MODEL`).
- `create_thread` calls that omit a model inherit the session model. The
  rewrite is on by default as a routing policy (the client cannot know its
  native fallback is unavailable behind this proxy), emits a trace event every
  time it fires, and `OPENCODE_GO_PROXY_INJECT_SESSION_MODEL=0` disables it.
  The `codex_app` tool merge stays opt-in via
  `OPENCODE_GO_PROXY_MERGE_CODEX_APP_TOOLS`.
- Windows restart/install scripts only kill listeners whose process name is
  this proxy; a foreign process holding the port is reported and left alone.
- CI runs the suite on ubuntu and windows (Python 3.11/3.12), builds and
  tests the macOS menu bar app, and separates lint into its own job.

### Added

- Claude Code support: `POST /v1/messages` now serves every certified Go
  model, not only the nine Anthropic Messages models. Messages-family models
  keep the byte-verbatim relay; Chat- and Responses-family models translate
  through a Responses payload (`src/opencode_go_proxy/anthropic_client.py`),
  reusing the existing retry, metering, prefix caching, and image-captioning
  machinery, and stream back as real Anthropic SSE (`message_start` /
  `content_block_*` / `message_delta` / `message_stop`). Errors on the
  surface use the Anthropic error envelope; `POST /v1/messages/count_tokens`
  answers a documented byte-based estimate. Input `thinking` blocks are
  dropped (Go models cannot replay Anthropic-signed thinking) while thinking
  output - e.g. GLM reasoning - streams as real `thinking` blocks;
  `stop_sequences` and `cache_control` are not forwarded.
- Three new certified Chat Completions models verified against the live
  gateway: `omen-alpha`, `step-5-preview`, `cat-jam-free` (35 -> 38 certified
  IDs).
- Fourth Go wire protocol: `gemini-4-argon-contributor` is served only
  through Go's Google-native endpoint
  (`/inference/go/google/v1beta/models/<id>:generateContent`, Bearer auth);
  `/chat/completions`, `/responses`, and `/messages` all return
  `ModelProtocolUnsupported` for it. The proxy reuses the Zen Google
  translation (`responses_payload_to_gemini_payload`, `_gemini_to_response`,
  gemini stream chunks) against the Go google base URL, so the model works
  on `/v1/responses` and `/v1/messages` (streaming and non-streaming). It is
  absent from the live `/models` listing, so a `user-models.json` overlay
  entry is required for it to appear in `/v1/models`.
- Remote-access caller token now also accepted via `x-api-key` (Claude Code
  sends its credentials that way and cannot send custom headers), alongside
  the existing `X-OpenCode-Go-Proxy-Token` header and zcode's Bearer
  fallback. The native relay strips a caller-token `x-api-key` before
  forwarding, mirroring the Bearer strip.
- Transparent empty-stream retry for Responses-family models on the Claude
  Code surface: `POST /v1/messages` with `stream: true` now buffers attempts
  1-2 and retries empty or reasoning-only streams (up to three attempts,
  same `OPENCODE_GO_PROXY_EMPTY_RETRY_WAIT_MS` backoff as `/v1/responses`)
  instead of handing Claude Code an empty reply. Upstream terminal errors
  are still delivered immediately, never retried.

### Changed

- The structured trace stream was redesigned around severity levels instead
  of an undifferentiated stderr flood. Every JSON line now carries a `level`
  (`debug`/`info`/`warn`/`error`) and every trace site is tagged by what it
  means: per-request telemetry (received/converted, upstream start/done) is
  `debug`; lifecycle and routing milestones (server start/stop, credential
  source, go-reject zen fallback) are `info`; handled failures and
  degradation (upstream retries/timeouts/errors, empty-stream retries,
  image/reasoning fallbacks, refresh skipped) are `warn`; outright failures
  (handler crash, empty-stream exhaustion, refused start) are `error`.
  Events that earned no keep were removed outright (`credential.lookup`,
  `catalog.refresh.tick`, `split_turn.caption_cache_hit` - outcome events
  already cover them). `OPENCODE_GO_PROXY_LOG_LEVEL` sets the minimum
  emitted level, currently defaulting to `info` for an active debugging
  period (the steady-state default is `warn`, where a healthy proxy stays
  silent); the start scripts carry a one-line `LOG_LEVEL` switch
  (warn/info/debug). Filtering happens before the async queue, and
  `OPENCODE_GO_PROXY_TRACE=0` still disables tracing entirely.

### Fixed

- Empty-stream exhaustion no longer ends a Responses-family turn with a
  silent, valid-but-empty completion: clients logged "no text, no tool
  calls, and no usage" and the agent stopped mid-task as if the work were
  done. When the final (third) empty-retry attempt also carries no output,
  the relay now replaces the contentless terminal with an explicit
  `response.error` (`code: "empty_stream"`) + `[DONE]`, and the same
  terminal is used when a retry never connects and every buffered attempt
  was empty. Both paths meter as 502 instead of a 200, so dashboards and
  agent UIs show a real failure
  (`src/opencode_go_proxy/passthrough.py`,
  `src/opencode_go_proxy/zen_upstream.py`).
- Chat-completions sub-calls now send the stable `x-opencode-session`
  affinity header (derived from the payload anchor when the caller supplies
  no session header, mirroring the compaction path). The Go gateway rejects
  session-less requests with `MissingSessionID` - observed as ~400
  rejections on 2026-10-16, three per split-turn image caption, which
  burned the caption's whole fallback budget on every captioned turn
  (`src/opencode_go_proxy/upstream.py`, `src/opencode_go_proxy/opencode_session.py`).
- Gemini payloads whose conversation ends on anything other than a plain
  user text turn - a model turn, a bare `functionResponse` (every post-tool
  request from Claude Code), or a `functionResponse` merged with text - are
  rejected by the Go Google gateway with an empty-body 400. The builder now
  appends a separate trailing `{"role": "user", "parts": [{"text":
  "Continue."}]}` content for those shapes only; tails that already end in
  user text are untouched. Size was never the trigger, which is why only
  tool-using dialogs failed.
- Map every network exception reachable from `urlopen` (notably
  `ssl.SSLError` and bare `http.client` failures) into the standard
  retry-then-502/504 path: 50 `SSLV3_ALERT_BAD_RECORD_MAC` and 24
  `RemoteDisconnected` escapes previously surfaced as 500 proxy crashes.
  Timeout keeps its distinct 504 mapping.
- Restart-time proxy log rotation (keep 5 generations) in
  `scripts/diag/restart_proxy.sh`; the stderr log is otherwise unbounded.
- Trace `upstream.pool` counters (fresh/reused/idle) every
  `OPENCODE_GO_PROXY_POOL_REPORT_EVERY` opens (default 100) so keep-alive
  savings are observable from the logs.
- Codex CLI discovery now searches `PATH` after the explicit
  `OPENCODE_GO_PROXY_CODEX_BIN` override and before the standalone/app-bundle
  fallbacks, so npm and Homebrew installs are found by native-model and
  codex-app-tool capture instead of logging `codex CLI not found`.

### Added

- Upstream keep-alive connection pool (`src/opencode_go_proxy/upstream_pool.py`,
  stdlib only): reuses persistent connections across turns instead of paying a
  fresh TLS handshake per request. Enabled by default; `OPENCODE_GO_PROXY_POOL=0`
  disables, `OPENCODE_GO_PROXY_POOL_SIZE` / `OPENCODE_GO_PROXY_POOL_IDLE_SEC`
  tune it. Stale idle connections retry once transparently on a fresh socket.
- Async buffered stderr tracing: `trace()` now enqueues (never blocks a
  request; drops are counted as `trace.dropped`) and a single writer thread
  batches lines. `OPENCODE_GO_PROXY_TRACE=0` disables tracing entirely.
- Fast path in the Responses SSE relay: text/reasoning deltas without
  `function_call` + `__` markers skip JSON parsing byte-identically.
- Drop dangling `function_call` items (a call with no matching output later
  in `input`, e.g. an abandoned client-side tool call) before forwarding a
  Responses request: strict gateways reject the whole turn otherwise and the
  session hard-sticks. Traced as `request.dangling_calls_dropped`;
  `OPENCODE_GO_PROXY_DROP_DANGLING_CALLS=0` disables.
- Fix merged `codex_app` tool schemas for the strict chat gateway: snapshots
  may carry union schemas with no top-level `type` (`automation_update` is a
  top-level `oneOf`), which rejected every tools-bearing chat-family turn
  with 400. The normalizer now guarantees top-level `"type": "object"` while
  preserving sibling keywords (an explicit `"type": null` no longer
  overwrites the marker); the checked-in snapshot is corrected too.
- Harden the keep-alive pool against truncated responses: a Content-Length
  body cut short detaches silently on readline EOF, so `close()` additionally
  requires a satisfied length before parking the connection.
- Reasoning-detox retry: when the gateway rejects a turn because cited
  server-side reasoning has expired (`Referenced reasoning item 'rs_...'
  was not found or has expired`), retry once with the opaque reasoning blobs
  stripped (traced as `request.reasoning_detox`). Applies to streaming and
  non-streaming Responses-family turns; `OPENCODE_GO_PROXY_REASONING_DETOX=0`
  disables.

### Security

- Validate the actual client address and require a separate caller token for
  explicitly enabled non-loopback access.
- Refuse insecure non-loopback startup configurations.

### Changed

- Responses-family empty-stream retry now tries up to three times with
  backoff (`OPENCODE_GO_PROXY_EMPTY_RETRY_WAIT_MS`, default `1000,2000`):
  attempts 1-2 stay buffered and are discarded when empty, attempt 3 relays
  live so the client always sees a terminal outcome.
- Reject unknown or malformed model IDs before routing instead of silently
  substituting `deepseek-v4.1-flash`.
- Avoid reverse-DNS lookup delays while starting the local listener.
- Clarify the project's independent, single-user credential and policy boundary.

### Fixed

- Normalize OpenCode Go's terminal `GoUsageLimitError` 429 into the OpenAI
  `insufficient_quota` error shape and stop retrying it locally. Codex treats
  the native envelope as generic pacing and can otherwise wait for the weekly
  quota's multi-hour `Retry-After` interval.

- Retry transient `RemoteDisconnected` upstream failures (the gateway closed
  the connection before response headers) inside the bounded retry bucket
  instead of crashing the request: `streaming.py`, both `upstream.py` chat
  callers, both `zen_upstream.py` zen POST helpers, and the native
  passthrough relay.
- Refuse to start a second listener when the port already serves one
  (`server.start_refused`, exit 1): on Windows `SO_REUSEADDR` lets two
  processes silently share one port, so double-clicking the start script
  used to split traffic across two instances.

## [0.4.10] - 2026-08-14

## [0.4.8] - 2026-08-14

### Added

- `opencode-go-proxy update` command: checks the latest GitHub release
  (`--json` for machines, exits 3 when an update exists), and `update --apply`
  re-installs a `uv tool` install from the newest tag or prints the exact
  pinned `uvx` one-liner otherwise. Never touches a running proxy.
- Menu bar pin bumped to `@v0.4.8`; version strings across the package now
  read 0.4.8.

## [0.4.7] - 2026-08-14

### Added

- `GET /version` (and `/v1/version`): the running version, git commit, and an
  update block checked against the latest GitHub release (24-hour TTL cache
  under the state dir, `?force=1` bypasses it). `GET /state` gains `version`
  and `update` fields; older clients are unaffected.

## [0.4.6] - 2026-08-14

### Added

- `scripts/make-release.py`: one-command release (bump, changelog section,
  commit, tag, push; `--dry-run` prints the plan). CHANGELOG anchor links,
  PUBLISHING.md refreshed, README gains an Updating section covering the menu
  bar, the `update` CLI, and manual/systemd installs.

## [0.4.5] - 2026-08-14

### Added

- Menu bar update support: a Version row, an Update Available row with a
  one-click apply (confirm -> new pin saved -> proxy restarts), Check for
  Updates, and a UserDefaults pin override so proxy updates no longer require
  rebuilding the menu bar app.

## [0.4.4] - 2026-08-14

### Fixed

- Catalog self-cleaning: a bare OpenCode Go slug the gateway rejects twice with
  `ModelError: not supported` is hidden from the picker (two-strike rule,
  in-memory per process lifetime; a later successful turn restores it).

## [0.4.3] - 2026-08-14

### Fixed

- Streaming fallback: when Go rejects an unknown model before the first
  streamed event, the identical request is re-dispatched through Zen and the
  Zen stream is relayed.

## [0.4.2] - 2026-08-14

### Fixed

- Non-streaming fallback: when Go answers `ModelError: not supported` for a
  bare slug that Zen serves, the identical request is retried through Zen and
  the Zen response (or error envelope) is returned. Go keeps ownership of
  colliding slugs it actually serves.

## [0.4.1] - 2026-08-14

### Fixed

- Zen-only bare slugs route to Zen, and Zen rows that collide with Go display
  names get a `(Zen)` suffix in the catalog so the picker stays unambiguous.
  Bare collisions both catalogs share keep routing to OpenCode Go.

## [0.4.0] - 2026-08-14

### Added

- OpenCode Zen support: the proxy now serves both OpenCode Go and OpenCode Zen.
  Zen models are auto-discovered from `https://opencode.ai/zen/v1/models` (no
  auth), merged with models.dev metadata, and appear in the catalog and
  `/v1/models` as `zen/<id>` slugs. Requests route per family - claude/qwen to
  Anthropic Messages (`/zen/v1/messages`, `x-api-key`), gemini to the Gemini API
  (`/zen/v1/models/<id>`, `x-goog-api-key`), gpt/grok to the Responses API
  (`/zen/v1/responses`, relayed verbatim), everything else to
  chat/completions - with each family's stream translated back to one uniform
  Responses stream. Auth uses the same key as Go
  (`OPENCODE_GO_API_KEY` / `opencode-go-api-key` keychain) with the header
  mapped per family. Zen turns meter with `provider="zen"` so they never count
  against the Go quota. Free models (`deepseek-v4-flash-free`, `mimo-v2.5-free`,
  `big-pickle`) need no credits.
- Usage in the menu bar: `GET /state` gains `usage.go` (OpenCode Go usage
  polled from `https://opencode.ai/zen/go/v1/usage` with a 60s TTL, `null` on
  fetch failure), `usage.goLimits` (fixed Go dollar budgets: $60/month,
  $30/week, $12 per rolling 5 hours), and `usage.zen` (today's turns/tokens and
  a seven-entry last-7-day token rollup from the local meter), alongside the
  existing all-provider keys.

## [0.3.1] - 2026-08-14

### Fixed

- Catalog `availability_nux` is emitted in the `{message: string} | null`
  object form the Codex app schema expects instead of a bare string;
  string-form nux arriving from upstream records is normalized, with empty
  strings mapped to `null`.

## [0.3.0] - 2026-08-13

### Added

- Native model coexistence: models from your logged-in Codex account
  (`codex debug models`, filtered of every `opencode-go/` slug) are merged
  with the OpenCode Go catalog and served by `/v1/models`. Native slugs route
  verbatim to `https://chatgpt.com/backend-api/codex`
  (`OPENCODE_GO_PROXY_NATIVE_BASE_URL` overrides); everything else translates
  to OpenCode Go as before. Official OAuth and API setups are untouched.
- App tool capture: `codex_app` tool definitions are snapshotted via
  `codex debug prompt-input` and merged into session-spawn requests
  (`contrib/codex-app-tools.json` fallback); `web_search_preview` translates
  to a chat function instead of being dropped.
- Compaction requests with query strings now route correctly (404 fixed).
- Multi-agent config v2 (`multi_agent_version: "v2"`) with agent sync, an
  `install-skills` ops command, and a `refresh-runtime` command (native
  recapture + merged re-render) that the menu bar's Refresh Catalog calls.
- macOS launchd agent removed (`contrib/launchd/`); the menu bar app owns the
  proxy process on macOS.

### Changed

- `MODEL_ALIASES` is gone: unknown non-native slugs fall back to the default
  model, same as before.
- Meter events carry a `provider` tag (`native` vs `opencode-go`).


- Ops contracts match the codex-router reference (plan 010): `smoke-test` posts
  a marker prompt through the local proxy at `http://127.0.0.1:8787/v1/responses`
  and asserts the marker comes back (custom base via `--base-url` or
  `OPENCODE_GO_PROXY_BASE_URL`); `support-bundle` emits a single JSON document
  (schemaVersion 1, mode 0600) with version, generatedAt, env summary, redacted
  config snapshot, meter tail, log tail, catalog model count, and doctor checks
  instead of a tar.gz; `doctor` gains reference-style `ok`/`warn`/`fail` checks
  (config file presence, catalog readability, port free/owned, log writability,
  best-effort upstream reachability) plus `--fix`, which repairs only what is
  safe without writing config.toml.
- Session-model inheritance matches the reference (plan 009): only
  `create_thread` calls get the session model injected and
  `chatgptWorkCloud` targets are skipped; `send_message_to_thread` is no
  longer rewritten.
- Catalog discovery sends an identifying `User-Agent`
  (`opencode-go-proxy/<version>`) because models.dev answers 403 to
  urllib's default UA (plan 009).
- Image routing (plan 009): a non-tools image turn keeps the requested
  model when the catalog marks it image-capable (`input_modalities`
  contains `image`), otherwise it falls back to `CODEX_IMAGE_MODEL` /
  `mimo-v2.5`; the image-plus-tools caption path is unchanged.
- Image fallback (plan 009b): when the upstream rejects a non-tools image
  turn at runtime with a caption-fallback 4xx (400/404/415/422), the proxy
  captions the images through the vision module and retries the same
  requested model once instead of failing the turn. The split-turn
  (image plus tools) caption path and the verbatim `/chat/completions`
  passthrough are unchanged.
- Usage meter events now match the codex-router reference schema (plan 008):
  `at` is ISO 8601 UTC, token and duration fields are camelCase
  (`inputTokens` / `outputTokens` / `totalTokens` / `durationMs`), and every
  event carries `meteringVersion: "opencode-go-proxy/1"` plus
  `provider: "opencode-go"`. The legacy snake_case/epoch spelling was dropped
  rather than double-written because no live consumer reads the file today
  (the menu bar is reworked in a later plan); if a consumer needs the old
  fields, they return additively.

- Rate-limit harvesting (plan 011): OpenAI-style (`x-ratelimit-*`) and
  Anthropic-style (`anthropic-ratelimit-*`) response headers from upstream 200s
  are parsed into per-provider quota snapshots (`limit` / `remaining` / `resetAt`
  / `sampledAt`), the latest snapshot per provider is kept, and persisted
  atomically to `quota-state.json` in the state dir. `GET /quota` (and
  `/v1/quota`) returns the state; a headerless upstream degrades to an empty
  snapshot.
- Config manager (plan 012): `config enable|disable|status [--json]` owns a
  marker-commented block in `~/.codex/config.toml` (`# BEGIN
  opencode-go-proxy-managed` to `# END opencode-go-proxy-managed`). Enable
  writes `openai_base_url` plus `model_catalog_json` (state-dir catalog) and
  refuses to replace user-owned values; disable removes only the block and
  deletes the file when the block was its only content. Codex Voice realtime
  keys (`experimental_realtime_webrtc_call_base_url` and
  `experimental_realtime_ws_base_url`) are added only when the user has not
  set them, so Voice stays on native endpoints. Tests and CI run against a
  temp file via `OPENCODE_GO_PROXY_CONFIG_PATH`; the real config.toml is a
  gated deploy step.

### Added

- Base-URL overrides (plan 010): the upstream chat base resolves as the
  `--chat-base-url` flag, then `OPENCODE_GO_BASE_URL`, then
  `OPENCODE_ZEN_BASE_URL`, then the legacy `CHAT_COMPLETIONS_BASE_URL`, then the
  built-in default.
- Ops install/status (plan 010): `install` and its `setup` alias copy the
  launchd plist into `~/Library/LaunchAgents` and load the agent on macOS,
  gated on an explicit `--yes` flag (running it is a deploy step); `status`
  reports running state, port ownership, launchd state, and log paths.
- Doctor/keychain seam (plan 010): `secrets.api_key_source()` reports where the
  key would resolve (env or keychain service) without reading the value, so
  doctor never prints the credential.
- Correctness contract (plan 007): an upstream 200 that streams no
  text, tool call, or reasoning is retried once with the identical request
  (terminal events held, ids reused); a second empty stream answers
  `response.error` code `empty_completion`. Upstream `input_tokens: 0` is
  estimated as `max(1000, ceil(prompt_bytes / 3.3))` capped at the model's
  context window and surfaced as `estimatedInputTokens` (kill switch
  `OPENCODE_GO_PROXY_ESTIMATE_ZERO_INPUT=0`; self-disables once the upstream
  reports real tokens again). The keepalive comment thread now runs until the
  stream truly ends, with writes serialized so comments never interleave into
  data frames.
- Auth transport guard (plan 006): zero-config protection for the loopback
  listener - a missing Host header answers `400 invalid_host`, a non-loopback
  Host answers `403 invalid_host` unless `OPENCODE_GO_PROXY_ALLOW_REMOTE=1`,
  requests carrying Origin / Referer / Sec-Fetch-Site answer
  `403 browser_request_rejected`, non-JSON POSTs to the API paths answer
  `415 unsupported_media_type`, and OPTIONS stays unhandled so browser
  preflight is blocked.
- Protocol surface (plan 005): `/v1/chat/completions` (and `/chat/completions`)
  is a verbatim passthrough - non-stream relays the upstream status and JSON
  body byte-for-byte (including the upstream's own error body), stream relays
  the upstream SSE unchanged with the same 15s keepalive comment mechanism the
  responses stream uses and stops on client disconnect; `/v1/messages` (and
  `/messages`) answer an explicit `400 invalid_request_error`; the WebSocket
  426 rejection now has a test asserting the exact response body.
- Vision bridge module (plan 004): `vision.describe()` returns structured
  `Evidence` (summary / text / layout / unreadable); the caption engine auto-picks the
  cheapest image-capable catalog model or a probed local runtime (Ollama, llama.cpp
  server, LM Studio); non-cached caption reads are metered with `kind=vision`; the old
  caption path now lives entirely in `vision.py`.
- Catalog contract with overlay and runtime reload (plan 003): `render_full_catalog`
  projects each model through the canonical key set Codex reads in
  `merged-models.json` (`multi_agent_version` at the model top level, plus
  `comp_hash`, `availability_nux`, and `approvals`/`collaboration_modes`/
  `permissions`/`token_budget`/`auto_review` in `model_messages`), backed by a
  key-set parity test against a sampled fixture.
- Runtime catalog lives under the state dir (`OPENCODE_GO_PROXY_STATE_DIR`,
  default `~/.codex/opencode-go-proxy/`); refresh never writes the repo's
  `contrib/` files, and startup renders the state-dir compact (or the seed)
  instantly while discovery runs in the background.
- `known_models()` replaces the frozen `KNOWN_MODELS`: the live slug set is
  cached by catalog file mtime, so `/v1/models` and model routing pick up a
  refreshed catalog without a restart (`reload_known_models()` forces it).
- User overlay for custom models: `user-models.json` (add / hide / edit display)
  plus hidden-model flags from `model-picker.json`, seven-day `availability_nux`
  announcements tracked in `announced-models.json`.
- Menu bar state contract (plan 013): `GET /state` (and `/v1/state`) returns
  one JSON document - `status`, `port`, `upstream`, the latest quota snapshot
  by `sampledAt` (`{provider, remaining, limit?, resetAt?}` or `null`),
  `usage` with `todayTurns` / `todayTokens` / a stable seven-entry `last7d`
  token list (local calendar days, oldest first), and `model` (most recent
  meter event, else the default). A missing or corrupt meter/quota file
  degrades to zeros or `null`. The Swift menu bar renders the Standard tier
  from that contract: quota card with reset countdown, today's turns/tokens,
  a 7-day usage bar list, and model/upstream rows, keeping the single-port
  guard and existing controls.

## [0.2.0] - 2026-08-10

### Added

- Prefix-cache support for OpenCode Go models: streaming requests now set
  `stream_options.include_usage`, the proxy parses the upstream's cache accounting
  (`prompt_cache_hit_tokens` / `prompt_tokens_details.cached_tokens`), surfaces it to
  Codex in `usage.input_tokens_details.cached_tokens`, logs it per request, and
  exposes aggregate hit/miss/ratio on a `/cache` (and `/metrics`) endpoint.
- `CacheTracker` in `src/cache.py`: thread-safe per-model hit/miss/ratio accounting.
- Key resolution falls back to `OPENCODE_API_KEY` and the `codex-router-opencode-go`
  keychain service, in addition to `OPENCODE_GO_API_KEY` / `opencode-go-api-key`.
- Spawned threads inherit the session model: `create_thread` and
  `send_message_to_thread` function calls get the parent thread's model injected
  when the caller omits one.
- Honest usage meter in `src/meter.py`: append-only `usage-events.jsonl` in the state
  dir with per-turn status, duration, tokens and retries. Truncated streams record
  `streamAborted` and never count as success; empty completions record
  `emptyCompletion`. Metering failures never break a live request.
- Upstream retry with bounded exponential backoff on transient failures
  (429/5xx/network/timeout), configured via `OPENCODE_GO_PROXY_MAX_RETRIES`
  (default 2) and `OPENCODE_GO_PROXY_RETRY_BASE_MS`.
- Ops CLI commands: `opencode-go-proxy doctor` (self-check), `smoke-test` (real
  upstream probe) and `support-bundle` (namespaced tarball of logs, config and
  meter, with secret values redacted).
- Agent skill `skills/opencode-go-proxy/SKILL.md`: orientation for operating the
  proxy (start/stop, ops commands, prefix caching, config, reliability).
- Caption latency: identical screenshot bytes are captioned once per hour (in-process
  byte-keyed cache, 256-entry bound), so repeat tool calls on the same screen skip the
  upstream round trip.
- Caption engine: `OPENCODE_GO_PROXY_CAPTION_MODEL=auto` picks the cheapest
  image-capable catalog model (an enabled local runtime first);
  `CODEX_IMAGE_MODEL` overrides, and `mimo-v2.5` is the fallback when the picked
  engine rejects image input (4xx).
- Caption images are sent with `detail: low` (`OPENCODE_GO_PROXY_CAPTION_DETAIL=none`
  disables); if the upstream rejects the detail value, the proxy retries once with a
  `sips`-downscaled JPEG (or the original URL) and no detail.
- Tests: cache parsing (both DeepSeek and OpenAI usage shapes), tracker accounting,
  `/cache` endpoint, streaming `stream_options`, cache passthrough to Codex,
  meter recording, upstream retry behavior, and the ops commands.

### Changed

- Native macOS menu bar app in `macos/MenuBarApp` (Swift/AppKit, SwiftPM): short status
  icon, live health check, start/stop of the proxy as a child process, open logs,
  reveal log file, copy port. It refuses to Start when port 8787 is already owned
  (single-port guard). Build with `swift build` in `macos/MenuBarApp` (macOS 13+).
- README: document that the proxy exposes a single HTTP port (`OPENCODE_GO_PROXY_PORT`,
  default 8787) with no admin/control channel, how to verify what is listening
  (`lsof -nP -iTCP:8787 -sTCP:LISTEN`), and how to shorten the Codex provider label
  (edit `[model_providers.opencode-go] name` in `~/.codex/config.toml`).

### Fixed

- zstd request bodies: the Codex desktop app sends `/v1/responses` bodies
  zstd-compressed whenever it is authenticated; the proxy now decompresses
  `Content-Encoding: zstd` (and gzip) so the desktop app works. Unsupported
  encodings return an explicit 400 instead of crashing on a magic byte.
- Reference catalog `contrib/opencode-go-catalog.json` now ships with the `ModelsCache` wrapper
  (`fetched_at`/`etag`/`client_version`/`models`). Codex 0.142+ desktop app requires all four
  top-level fields - the previous bare `{"models": [...]}` caused the model picker to fall back
  to "Custom" instead of showing the full list. The CLI tolerated the bare format, so this only
  surfaced in the desktop app.
- SIGTERM graceful shutdown: `serve_forever` now runs on a background thread so the signal
  handler's `server.shutdown()` no longer deadlocks on the main thread, leaving the process
  unkillable via SIGTERM (launchd/systemd stop, menu bar Stop).
- WebSocket upgrade requests (Codex desktop app realtime) are answered with an explicit
  `HTTP/1.1 426 Upgrade Required` instead of the previous HTTP/1.0 404, which surfaced as
  "WebSocket protocol error: HTTP version must be 1.1 or higher" before the app fell back
  to HTTP streaming.
- Image caption budget: default timeout 60s -> 30s, and caption sub-calls no longer
  retry transient failures, so a bad caption leg costs at most ~30s instead of stacking
  retries.

## [0.1.2] - 2026-06-21

Bug fixes + removed AUR packaging.

### Fixed

- `call_upstream_chat` now catches `json.JSONDecodeError` - invalid JSON from upstream returns 502 instead of crashing
- Streaming crash: if `handle_streaming_request` raised after SSE headers sent, sends `response.error` SSE event instead of corrupted HTTP response
- Streaming + missing API key: `resolve_api_key` moved before `response.created` so error event reaches client
- README launchd path: `~/Library/.codex/logs` -> `~/.codex/logs` (matches plist)
- LICENSE copyright year 2025 -> 2026

### Removed

- AUR package (`aur/opencode-go-proxy-git/`) - not launching on AUR
- PyPI - not launching on PyPI; `uvx --from git+...` is the install path

## [0.1.1] - 2026-06-21

Graceful shutdown + launchd service file.

### Added

- SIGTERM handler for graceful shutdown on `launchctl bootout` / `systemctl stop`
- launchd plist at `contrib/launchd/com.opencode-go.proxy.plist`
- README: launchd setup instructions with copy + bootstrap commands

## [0.1.0] - 2026-06-21

Initial public release.

### Added

- Responses `input` to chat `messages` translation
- `instructions` and `developer` roles mapped to system messages
- Function tool schema passthrough
- Custom/freeform tool adaptation (Codex `apply_patch` works)
- `reasoning_content` replay across tool-call turns
- Real-time SSE streaming
- Image captioning via MiMo V2.5 when tools are present
- SSRF protection on image URLs (`data:image/` and `https://` only)
- Configurable body cap, bind address guard
- macOS keychain credential resolution
- Local health and model-list endpoints
- Reference model catalog with all 13 OpenCode Go models
- systemd user service at `contrib/systemd/`
- 41 tests (unit + integration) covering protocol, credentials, HTTP round-trip, alias map, tool calls, streaming tool calls, streaming error handling, streaming crash recovery, invalid upstream JSON, SSRF, and image captioning

### Security

- SSRF validation on image URLs
- Non-negative Content-Length validation
- Generic error messages to client (full bodies only in trace logs)
- No path reflection in 404 responses
- Bind address guard warns on non-localhost

[Unreleased]: https://github.com/jidzhang/opencode-go-proxy/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.4.0
[0.3.1]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.3.1
[0.3.0]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.3.0
[0.1.2]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.1.2
[0.1.1]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.1.1
[0.1.0]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.1.0
[0.4.8]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.4.8
[0.4.7]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.4.7
[0.4.6]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.4.6
[0.4.5]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.4.5
[0.4.4]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.4.4
[0.4.3]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.4.3
[0.4.2]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.4.2
[0.4.1]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.4.1

[0.4.10]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.4.10
