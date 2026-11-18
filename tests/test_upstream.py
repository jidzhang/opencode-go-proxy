"""Unit tests for the shared retry policy and quota normalization.

The chat-completions client these once drove was deleted with the dead
/chat/completions translation path; the policy helpers below are still used by
every live family call. Live retry behaviour is covered end-to-end by
test_network_mapping (_open_upstream_stream) and test_zen_upstream (_zen_post).
"""

import json
import os
import time
from unittest import mock

from opencode_go_proxy.upstream import (
    DEFAULT_RETRY_BUDGET_SEC,
    normalize_usage_limit_error,
    retriable_http_failure,
    retriable_http_status,
    retry_allowed,
    retry_budget_sec,
)

GO_QUOTA_BODY = json.dumps({
    "type": "error",
    "error": {"type": "GoUsageLimitError", "message": "Go usage limit exceeded"},
    "metadata": {"workspace": "org-test", "limitName": "weekly"},
}).encode("utf-8")


class TestRetryClassification:
    def test_transient_statuses_are_retriable(self) -> None:
        for code in (429, 500, 502, 503, 504):
            assert retriable_http_status(code)

    def test_client_errors_are_not_retriable(self) -> None:
        for code in (400, 401, 403, 404, 410, 422):
            assert not retriable_http_status(code)

    def test_go_usage_limit_is_terminal(self) -> None:
        assert not retriable_http_failure(429, GO_QUOTA_BODY)

    def test_ordinary_rate_limit_is_retriable(self) -> None:
        assert retriable_http_failure(429, b'{"error":{"type":"rate_limit_error"}}')

    def test_server_errors_ignore_the_quota_envelope(self) -> None:
        # A 5xx is transient even when its body happens to look like a quota
        # error: only 429 is downgraded to terminal.
        assert retriable_http_failure(503, GO_QUOTA_BODY)


class TestRetryBudget:
    """The retry sequence is bounded by wall clock, not only by attempt count.

    A black-holing network (SYN dropped, WinError 10060) spends the full
    per-attempt timeout on every try; two retries measured 126s in production.
    The budget stops starting retries once the request is older than the
    budget, so the turn fails in about a minute instead of two.
    """

    def test_default_budget_is_a_minute(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert retry_budget_sec() == DEFAULT_RETRY_BUDGET_SEC

    def test_env_override_and_garbage_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_RETRY_BUDGET_SEC": "5"}, clear=True):
            assert retry_budget_sec() == 5.0
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_RETRY_BUDGET_SEC": "soon"}, clear=True):
            assert retry_budget_sec() == DEFAULT_RETRY_BUDGET_SEC

    def test_zero_disables_the_cap(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_RETRY_BUDGET_SEC": "0"}, clear=True):
            assert retry_allowed(time.monotonic() - 10_000)

    def test_fresh_request_may_retry(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_RETRY_BUDGET_SEC": "60"}, clear=True):
            assert retry_allowed(time.monotonic())

    def test_request_past_the_budget_may_not_retry(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_RETRY_BUDGET_SEC": "60"}, clear=True):
            assert not retry_allowed(time.monotonic() - 61)


class TestQuotaNormalization:
    def test_go_quota_renders_as_insufficient_quota(self) -> None:
        normalized = normalize_usage_limit_error(GO_QUOTA_BODY)
        assert isinstance(normalized, bytes)
        value = json.loads(normalized)
        assert value["error"]["type"] == "insufficient_quota"
        assert value["error"]["code"] == "insufficient_quota"
        assert value["error"]["message"] == "Go usage limit exceeded (weekly)"
        assert b"org-test" not in normalized

    def test_non_quota_body_passes_through(self) -> None:
        assert normalize_usage_limit_error(b'{"error":{"type":"rate_limit_error"}}') is None

    def test_input_type_is_preserved(self) -> None:
        text = GO_QUOTA_BODY.decode("utf-8")
        assert isinstance(normalize_usage_limit_error(text), str)
