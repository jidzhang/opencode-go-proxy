# Pending decisions -- leftover from the 2026-10-13 code review

NOTE (2026-10-29): translated from the Chinese original to comply with the
project's English/ASCII policy. Content is otherwise unchanged; the
"uncommitted" remarks below describe the state on 2026-10-13 -- those changes
have since been committed.

On 2026-10-13 a `/code-review max --fix` run covered the working-tree changes
(empty-response retry feature + caller-token stripping): 16 findings, 11 fixed
and test-verified. This note records the 1 blocking item needing a human
decision and 2 larger skipped items, for resolution on return. The
working-tree changes were uncommitted at the time.

Related files:
- `src/opencode_go_proxy/zen_upstream.py` (retry logic)
- `src/opencode_go_proxy/guards.py`, `src/opencode_go_proxy/passthrough.py` (token validation/stripping)
- `plans/cleanup-progress.md` (review fix batches over time)

---

## Decision 1 (blocking): do reasoning deltas count as "having content"?

**Current state**: `_RESPONSES_CONTENT_MARKERS` in `zen_upstream.py:1742` now
contains only `response.output_text.delta`,
`response.function_call_arguments.delta`, `function_call`, `tool_call`. Before
the review it also contained `reasoning_text.delta` and
`reasoning_summary_text.delta`; the review fix removed them.

**Background**: `_relay_responses_verbatim` buffers the first SSE attempt. If
the stream ends normally but `_responses_buffer_has_content` judges it
content-free, the buffer is discarded and one retry follows (at most one
retry, capped at 8MB). Streams judged to have content are flushed directly, no
retry.

**Conflict**: the review found the old reasoning markers contradicted
`_responses_buffer_has_content`'s own docstring, which states reasoning-only
streams are treated as empty. If reasoning counts as content, a complete
reasoning-only stream never triggers a retry, yet the client SDK drops signed
reasoning parts, so the turn still fails with `empty_model_response` --
exactly the scenario the retry feature was built for.

**Positions**:

| Option | Meaning | Follow-up work |
|---|---|---|
| A. Keep as-is (reasoning is not content) | A complete reasoning-only stream triggers one retry, matching the docstring | None |
| B. Revert to counting as content | Any reasoning delta marks success, never retry | Restore both markers and the explanatory comment at `zen_upstream.py:1748`; update test `test_verbatim_reasoning_only_terminal_retries_once` |

**Deciding factor**: if reasoning entered the marker list by mistake (e.g.
copied from the relay filter's event list), pick A; if it was deliberate
policy ("once upstream starts producing, stop spending on retries"), pick B.
Note that under B, reasoning-only streams still fail on the client, so the
retry gives up its last chance at an automatic save -- a policy question, not
a right-or-wrong question.

---

## Decision 2 (optional, design tradeoff): first-token latency

The buffering policy applies to all openai_responses streams (including the
Go provider -- `go_upstream.py:110` shares
`handle_family_responses_request`). On a healthy stream the client receives
only keepalive comment lines until generation finishes or 8MB accumulates, so
first-token latency equals total generation time.

- Impact: interactive feel of long generations (e.g. 120s); idle watchdogs on
  clients that ignore comment lines may time out by mistake.
- Alternatives: switch to live passthrough after the first content marker
  (hybrid mode, giving up retry for that stream); or buffer only for the zen
  provider.
- Status: unchanged. Acceptable if there is no actual lag/timeout feedback.

## Decision 3 (optional, low-probability race): keepalive double-lock handoff

At the retry handoff, when `ka_thread.join(timeout=interval)` returns, the
first keepalive thread may still be blocked in `handler.wfile.write` (client
send buffer full); then `_relay_stream` writes the same socket under a
different lock. If a stalled client recovers exactly in that window, a late
`: keepalive` comment may land between SSE event lines (per the SSE spec, `:`
comment lines are ignored by clients and do not truncate events -- at most
one stray comment, no frame corruption).

- Low probability (requires the client stall to overlap the handoff
  precisely); fixing needs a structural change to the thread handoff (shared
  lock or thread takeover), risk outweighs benefit, so skipped.
- Status: unchanged.

## Recorded only, no action recommended

- Extracting a shared "SSE relay + empty retry" primitive across
  `streaming.py` / `passthrough._relay_stream` / `_ZenStreamEngine`;
  reusing `quota._headers_map` normalization. Beyond this diff's scope; a
  follow-up refactoring topic.
- Per-line `_client_gone` probing, list-of-bytes buffering: consistent with
  existing code precedent, unchanged.

## Resolutions (2026-10-13)

- Decision 1: A -- reasoning does not count as "having content", keep as-is.
  Rationale: the `reasoning_text.delta` marker existed only in that day's
  uncommitted draft, with zero hits in committed history (`git log -S`), so
  no historical intent needs protecting; in substance a reasoning-only turn
  always fails on the client, and the retry is the last automatic save, so
  A's worst case equals B's outcome. Code comments, docstring, and tests
  already agree; no changes needed.
- Decision 2: skip. Keep full buffering absent lag/timeout feedback; the
  evolution path with feedback is switching to live passthrough after the
  first content marker (giving up that stream's retry right).
- Decision 3: skip. The race is both low-probability and low-impact (see the
  comment-line note above); wording here already downgraded accordingly.
- Follow-up memo (non-blocking): `_responses_buffer_has_content` matches byte
  substrings, so reasoning text literally containing `tool_call` /
  `function_call` is misjudged as content; the proper fix is matching on the
  JSON `type` field, left for later hardening.
- Working-tree changes uncommitted (as required).

## Verification status (2026-10-13)

- 93/93 passed across the 4 affected test files;
  `tests/test_zen_upstream.py` collects 36 items (30 original + 3 migrated +
  3 new regression).
- Full-suite failures match the clean baseline (all pre-existing
  Windows/environment issues, none introduced here).
