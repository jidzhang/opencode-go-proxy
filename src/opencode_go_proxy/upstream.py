"""Shared retry policy and error normalization for upstream calls.

One retry policy (bounded backoff on 429 / 5xx / network / timeout, excluding
terminal account quota failures) and one normalization of the Go quota
envelope, used by every per-family call path in zen_upstream and go_upstream.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .protocol import cache_stats_from_usage
from .trace import trace

# Upstream retry policy. Transient failures (429 / 5xx / network / timeout) are
# retried a bounded number of times with a small exponential backoff before the
# error is surfaced, so a flaky upstream does not kill a turn that a retry
# would have completed. Tuned small: localhost proxy, latency-sensitive client.
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BASE_SLEEP_MS = 150


def default_max_retries() -> int:
    try:
        return max(0, int(os.environ.get("OPENCODE_GO_PROXY_MAX_RETRIES", str(DEFAULT_MAX_RETRIES))))
    except ValueError:
        return DEFAULT_MAX_RETRIES


def retriable_http_status(code: int) -> bool:
    return code in (429, 500, 502, 503, 504)


def _usage_limit_details(body: bytes | str) -> tuple[str, str] | None:
    """Return (message, limit_name) for OpenCode Go's terminal quota envelope."""
    try:
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    error = value.get("error")
    if not isinstance(error, dict) or error.get("type") != "GoUsageLimitError":
        return None
    message = error.get("message")
    metadata = value.get("metadata")
    limit_name = metadata.get("limitName") if isinstance(metadata, dict) else None
    if not isinstance(message, str) or not message:
        message = "Go usage limit exceeded"
    if not isinstance(limit_name, str) or not limit_name:
        limit_name = "usage"
    return message, limit_name


def retriable_http_failure(status: int, body: bytes | str) -> bool:
    """Whether an HTTP failure is transient, accounting for terminal quotas."""
    if not retriable_http_status(status):
        return False
    return status != 429 or _usage_limit_details(body) is None


def normalize_usage_limit_error(body: bytes | str) -> bytes | str | None:
    """Render Go's quota envelope as Codex's terminal OpenAI error shape.

    OpenCode returns ``GoUsageLimitError`` as an Anthropic-style top-level
    ``type``. Codex treats an unrecognized 429 as capacity pacing and waits for
    ``Retry-After``; ``insufficient_quota`` is explicitly terminal. Preserve the
    input type (bytes or text) for verbatim relay paths.
    """
    details = _usage_limit_details(body)
    if details is None:
        return None
    message, limit_name = details
    rendered = json.dumps(
        {
            "error": {
                "type": "insufficient_quota",
                "code": "insufficient_quota",
                "message": f"{message} ({limit_name})",
            }
        },
        separators=(",", ":"),
    )
    return rendered.encode("utf-8") if isinstance(body, bytes) else rendered


def retry_sleep(attempt: int) -> None:
    """Sleep for a bounded exponential backoff before retry attempt N (1-based)."""
    base = DEFAULT_RETRY_BASE_SLEEP_MS
    try:
        base = max(0, int(os.environ.get("OPENCODE_GO_PROXY_RETRY_BASE_MS", str(base))))
    except ValueError:
        pass
    delay = min(base * (2 ** (attempt - 1)), 2000) / 1000.0
    time.sleep(delay)


def usage_tokens(usage: Any) -> tuple[Any, Any, Any]:
    """Return (input, output, total) token counts from an upstream usage dict."""
    if not isinstance(usage, dict):
        return None, None, None
    return usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens")


def record_cache(
    tracker: Any,
    model: str | None,
    usage: Any,
    session: str | None = None,
    request_id: str | None = None,
) -> None:
    """Fold one upstream response's cache accounting into the tracker.

    ``session`` is the affinity id sent upstream (``x-opencode-session``). The
    tracker uses it to tell a cold start from a prefix that broke
    mid-conversation, so a call site that knows the id should always pass it.
    ``request_id`` only adds the per-request debug trace, which is what makes
    a single turn's hit/miss inspectable in the log.
    """
    stats = cache_stats_from_usage(usage)
    if stats["ratio"] is None:
        return
    tracker.record(model or "unknown", stats["hit"], stats["miss"], session=session)
    if request_id:
        trace(
            "cache.recorded", level="debug",
            request_id=request_id,
            model=model,
            session=session,
            cache={"hit": stats["hit"], "miss": stats["miss"], "ratio": stats["ratio"]},
        )


def with_include_usage(body: dict[str, Any], *, stream: bool) -> dict[str, Any]:
    """Return a chat body with streamed usage reporting turned on.

    Chat-completions endpoints omit usage from a stream unless
    ``stream_options.include_usage`` is set, and without usage there is no
    prefix-cache accounting to report or forward. Only that one flag is added:
    a client-supplied ``stream_options`` keeps its other keys, and an explicit
    ``include_usage`` is left alone because the caller already decided.
    """
    if not stream:
        return body
    options = body.get("stream_options")
    if options is None:
        updated = dict(body)
        updated["stream_options"] = {"include_usage": True}
        return updated
    if isinstance(options, dict) and "include_usage" not in options:
        updated = dict(body)
        updated["stream_options"] = {**options, "include_usage": True}
        return updated
    return body


class UsageTap:
    """Capture the usage object from relayed chat-completions SSE lines.

    With ``include_usage`` set, a chat stream ends with one extra chunk whose
    ``choices`` is empty and whose ``usage`` carries the token counts. The tap
    keeps the last non-null usage it sees and returns every line byte-for-byte,
    so it can sit in the relay's transform slot without changing the stream.
    """

    def __init__(self) -> None:
        self.usage: dict[str, Any] | None = None

    def __call__(self, line: bytes) -> bytes:
        if self.usage is not None or b'"usage"' not in line:
            return line
        payload = line.strip()
        if payload.startswith(b"data:"):
            payload = payload[len(b"data:"):].strip()
        if not payload or payload == b"[DONE]":
            return line
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return line
        usage = event.get("usage") if isinstance(event, dict) else None
        if isinstance(usage, dict) and usage:
            self.usage = usage
        return line


def record_cache_from_body(
    tracker: Any,
    model: str | None,
    response_body: bytes,
    session: str | None = None,
    request_id: str | None = None,
) -> None:
    """Fold a non-stream chat response's usage into the cache tracker.

    A non-stream chat completion carries usage in the JSON body itself; a body
    that is not JSON (an error page, a truncated read) is simply not counted.
    """
    try:
        value = json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    usage = value.get("usage") if isinstance(value, dict) else None
    record_cache(tracker, model, usage, session=session, request_id=request_id)
