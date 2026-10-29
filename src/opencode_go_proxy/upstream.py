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


def record_cache(tracker: Any, model: str | None, usage: Any) -> None:
    """Fold one upstream response's cache accounting into the tracker."""
    stats = cache_stats_from_usage(usage)
    if stats["ratio"] is None:
        return
    tracker.record(model or "unknown", stats["hit"], stats["miss"])
