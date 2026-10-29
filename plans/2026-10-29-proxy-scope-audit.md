# Out-of-Remit Design Audit - 2026-10-29

Status: **remediation applied the same day.** Every finding below that was
actionable in code has been fixed; the verification column states how.

A full-repo review against one rule:

> A proxy forwards faithfully. The model the caller asked for is the model that
> reaches the upstream; the numbers the provider reported are the numbers the
> client sees. Any code that makes a product decision on the caller's behalf is
> out of remit.

Evidence base: `logs/service.err.log` (10-19 through 10-29, 22 NSSM rotation
segments), `git show 91bdcda` (the caption removal), direct execution of the
translation layer, and a per-file pass over all 149 tracked files.

Coverage: `src/` (36 modules, 14,507 lines), `macos/MenuBarApp` (1,447 lines),
`scripts/` (737 lines), 9 root scripts, `contrib/`, every markdown doc,
`.github/workflows/ci.yml`, git config files, and the 51-file test suite.

---

## The original complaint, proven

The deleted caption engine (`git show 91bdcda^:src/opencode_go_proxy/vision.py:321-352`)
ranked image-capable models by name substring - `flash`, `haiku`, `mini`,
`lite`, `small`, `turbo` - then priority, then slug, and dispatched every
image-bearing turn to the winner. Three guesses stacked: price from a model's
name, capability from this repo's seed file, and availability assumed.

The third guess failed. Between 2026-10-27 19:04 and 2026-10-28 14:37 the
auto-pick chose `deepseek-v4-flash`, which the upstream had deprecated:

| Fact | Evidence |
|---|---|
| Failed window | 2026-10-27 19:04:26 -> 2026-10-28 14:37:59 |
| Affected turns | 875 distinct `request_id`s, two warn lines each |
| Upstream answer | `{"type":"ModelDeprecated","message":"Model deepseek-v4-flash has been deprecated."}`, HTTP 410, 841 occurrences |
| Caption side | 886 `split_turn.caption_failed` events, `engine` always `deepseek-v4-flash`, status 502 |
| Overlap | 841 of the 410-request ids coincide exactly with caption failures |

The same slug was also `DEFAULT_MODEL` - one rotting constant deciding the
default model, the compaction model, the caption engine, and the `/state`
display. The seed catalog still listed it as `priority: 10` with the
description "Fast, cheap. Best default for everyday coding."

**Fixed:** commit `91bdcda` removed the caption path; this session removed
`DEFAULT_MODEL` entirely (missing model = 400 on every surface), the
marketing descriptions, and the fabricated capability numbers that fed the
pick.

---

## Findings and remediation status

### HIGH

| # | Finding | Status | Verification |
|---|---|---|---|
| H1 | Hardcoded default model, deprecated upstream; 17 `or DEFAULT_MODEL` sites across every surface | **Fixed** | `grep DEFAULT_MODEL src/` returns nothing; `tests/test_integration.py::test_missing_model_is_rejected` pins the 400 |
| H2 | `inject_session_model` rewrote caller `create_thread` arguments silently | **Fixed (visible policy)** | On by default as a routing decision (owner's call: the proxy knows the native fallback is unusable here, the client does not); every rewrite traced; `OPENCODE_GO_PROXY_INJECT_SESSION_MODEL=0` is the kill switch; `test_relay.py` covers both states |
| H3 | Go models' reasoning levels clamped against the native Codex vocabulary | **Fixed** | `_clamp_efforts` deleted; `test_go_reasoning_levels_are_forwarded_verbatim` pins passthrough |
| H4 | Fabricated capability numbers: unknown windows rendered as 1,000,000, zen entries with synthesized descriptions | **Fixed** | Unknown stays `null`; `auto_compact_token_limit` only when the window is known; zen description removed |
| H5 | Usage overwritten: estimate replaced `input_tokens`/`total_tokens` | **Fixed** | Estimate is a separate `estimatedInputTokens` field only |
| H6 | Caller generation params silently dropped (`text.format`, `stop`, `seed`, penalties, `parallel_tool_calls`) | **Fixed** | All forwarded; `text.format` maps to `response_format` so structured output survives |
| H7 | History silently deleted; the assistant-text loss was a genuine bug (write-only accumulator) | **Fixed** | `pending_assistant_content` now carried into the message; `test_assistant_text_between_tool_call_and_output_is_preserved` |
| H8 | Tools the caller never sent were injected (`codex_app` snapshot merge) | **Fixed (opt-in)** | Default off; `OPENCODE_GO_PROXY_MERGE_CODEX_APP_TOOLS` enables |
| H9 | Gemini family appended `"Continue."` user turn | **Kept, made visible** | Wire-shape necessity (the gateway 400s those tails); now traced per occurrence and switchable via `OPENCODE_GO_PROXY_GEMINI_CONTINUE_TAIL` |
| H10 | ~960 lines of dead code carrying the go->zen re-dispatch, the two-strike auto-hide, and 4xx->502 relabelling, with 7 test files validating it | **Fixed** | `handle_responses_request`, `handle_streaming_request`, `handle_chat_stream_passthrough`, `call_upstream_chat(_verbatim)`, the fallback trio, and the picker writers deleted; their tests deleted or narrowed to live primitives |

### MEDIUM

| # | Finding | Status |
|---|---|---|
| M1 | Compaction: own prompt, tool traffic flattened away, 80k tail truncation, vacuous placeholder summary | **Fixed** (tool calls/results now rendered; empty summary is an explicit error; truncation documented) |
| M2 | Thinking budget rewrites: `max_tokens` inflated, mid-loop thinking silently off, temperature dropped | **Fixed** (explicit caller cap never inflated; every adjustment traced) |
| M3 | Numeric thinking budget quantized to a coarse ladder | Kept - inherent to the zen anthropic wire family, which accepts effort levels only |
| M4 | Truncation reported as `end_turn` | **Fixed** (`finish_reason` forwarded; non-stop becomes `status: incomplete`) |
| M5 | Seed catalog shipped "cheapest / best default" marketing and hand ranking | **Fixed** (descriptions neutralized; ranking is now this fork's editorial data) |
| M6 | Certified allowlist filters upstream `/models` | Kept - required to route wire families; documented |
| M7 | Operator-precedence bug hijacked native `/responses/compact` into local compaction | **Fixed** (explicit parenthesization) |
| M8 | Non-stream 5xx relabelled 502 | **Fixed** (upstream status forwarded verbatim) |
| M9 | Menu bar hardcoded `--chat-base-url`, silently defeating `OPENCODE_GO_BASE_URL` | **Fixed** (flag removed; proxy resolves its own chain) |
| M10 | Port 8787 baked into the supervisor | Kept - the supervisor owns its child; acceptable scope |
| M11 | Update flow installed upstream-repo code over this fork | **Fixed** (all pins point at this repo; update dialog shows the exact source) |
| M12 | Menu bar displayed the deprecated default as "Model" | **Fixed** via H1 (`/state` reports the last used model or null) |
| M13 | `make-compact-catalog.py` unconditionally overwrote visibility/capability fields and could not reproduce the committed seed | **Fixed** (fill-if-absent semantics; divergence assertion on `shared_instructions`) |
| M14 | `scripts/diag/README.md` claimed the files were uncommitted | **Fixed** |
| M15 | Restart/install scripts killed whatever PID held :19999 | **Fixed** (kill scoped to this proxy's process name; foreign holders reported) |
| M16 | CI validated the dead code and never ran on Windows | **Fixed** (ubuntu+windows, py3.11/3.12 matrix, Swift build+test, separate lint job, pinned ruff config) |
| M17 | SECURITY.md understated credential sources and remote-auth transports | **Fixed** (three transports documented; registry caveat recorded) |

### Cleared as in-scope (with evidence)

- `macos/MenuBarApp` performs no routing and no model selection: its only
  proxy calls are four read-only GETs (`/health`, `/state`, `/version`,
  `/v1/models` counted, not parsed). It never writes `--model` or touches
  picker files; `config.toml` access is read-only in-app and delegated to the
  guarded CLI otherwise.
- `scripts/diag/*` are operator-run repro tools; their `--model` defaults are
  CLI arguments, not proxy decisions.
- `quota.py` / `usage_poller.py` never invent numbers; `guards.py`,
  `routing.py`, `trace.py`, `secrets.py`, `ops.py`, `state.py`,
  `config_manager.py`, `agents_sync.py`, `models_cmd.py` contain no
  caller-side product decisions.
- `stop-service.bat`, `remove-service.bat`, `restart-service.bat` operate by
  service name with a pause before destructive action.

---

## Operational note

The Windows service process was started 2026-10-28 23:51:49, before commit
`91bdcda` (2026-10-29 00:04:42) and before this session's changes. It must be
restarted (`restart-service.bat`, elevated) to load the remediated code.

## Language policy

Per the owner's decision the project is English/ASCII throughout: source,
comments, docs, and data are pure ASCII (verified by byte scan); the Chinese
working note `plans/2026-10-13-review-pending-decisions.md` predates this
policy and awaits the owner's call on translate-or-delete.
