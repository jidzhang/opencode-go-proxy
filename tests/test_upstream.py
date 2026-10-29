"""Unit tests for the shared retry policy and quota normalization.

The chat-completions client these once drove was deleted with the dead
/chat/completions translation path; the policy helpers below are still used by
every live family call. Live retry behaviour is covered end-to-end by
test_network_mapping (_open_upstream_stream) and test_zen_upstream (_zen_post).
"""

import json

from opencode_go_proxy.upstream import (
    normalize_usage_limit_error,
    retriable_http_failure,
    retriable_http_status,
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
