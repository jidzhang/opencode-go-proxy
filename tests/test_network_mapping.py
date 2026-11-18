"""Network exception mapping: nothing reachable from urlopen may escape raw.

Regression cover for 74 historical request.crashed 500s (ssl.SSLError
BAD_RECORD_MAC, http.client.RemoteDisconnected). Every request-path opener
must map these into _ConnectFailed / ProxyError; TimeoutError must keep its
distinct 504 mapping after the except-clause reorder.
"""

from __future__ import annotations

import http.client
import os
import ssl
import urllib.error
import urllib.request
from http import HTTPStatus
from types import SimpleNamespace
from unittest import mock

from opencode_go_proxy import zen_upstream as zu
from opencode_go_proxy.errors import ProxyError
from opencode_go_proxy.streaming import _ConnectFailed, _open_upstream_stream


def _req():
    return urllib.request.Request("https://opencode.ai/x", data=b"{}",
                                  headers={}, method="POST")


def _config():
    return SimpleNamespace(timeout_sec=5, chat_base_url="https://opencode.ai",
                           api_key_env="OPENCODE_GO_API_KEY")


def _raise(exc):
    def fake_urlopen(request, *args, **kwargs):
        raise exc
    return mock.patch("urllib.request.urlopen", side_effect=fake_urlopen)


class TestOpenStreamMapping:
    def test_ssl_error_becomes_connect_failed(self) -> None:
        with _raise(ssl.SSLError("SSLV3_ALERT_BAD_RECORD_MAC")):
            try:
                _open_upstream_stream(_req(), _config(), "r", 0)
            except _ConnectFailed as fail:
                assert isinstance(fail.exc, ssl.SSLError)
            else:  # pragma: no cover
                raise AssertionError("SSLError leaked raw")

    def test_bad_status_line_becomes_connect_failed(self) -> None:
        with _raise(http.client.BadStatusLine("garbage")):
            try:
                _open_upstream_stream(_req(), _config(), "r", 0)
            except _ConnectFailed:
                pass
            else:  # pragma: no cover
                raise AssertionError("BadStatusLine leaked raw")

    def test_timeout_stays_timeout(self) -> None:
        with _raise(TimeoutError("timed out")):
            try:
                _open_upstream_stream(_req(), _config(), "r", 0)
            except _ConnectFailed as fail:
                assert isinstance(fail.exc, TimeoutError)
            else:  # pragma: no cover
                raise AssertionError("TimeoutError leaked raw")


class TestRetryBudgetStopsTheSequence:
    """A black-holed connect must not be retried past the wall-clock budget.

    The 2026-11-17 logs show three connect attempts of ~42s each against a
    network dropping SYNs (WinError 10060): 126s before the client saw a 502.
    With the budget set, the sequence stops as soon as a retry would start
    after the deadline, so the failure surfaces in roughly one attempt.
    """

    def test_budget_exhausted_after_first_attempt_does_not_retry(self) -> None:
        attempts = 0

        def fake_urlopen(request, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            raise urllib.error.URLError(TimeoutError("connect timed out"))

        # A 0.01s budget with a clock that jumps past it after the first
        # attempt: the first try is allowed, the retry is not.
        readings = iter([0.0, 999.0, 999.0])
        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_PROXY_RETRY_BUDGET_SEC": "0.01"}, clear=False
        ), mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), mock.patch(
            "time.monotonic", side_effect=lambda: next(readings, 999.0)
        ):
            try:
                _open_upstream_stream(_req(), _config(), "r", 5)
            except _ConnectFailed as fail:
                assert fail.attempts == 0
            else:  # pragma: no cover
                raise AssertionError("budget did not stop the retry sequence")
        assert attempts == 1

    def test_budget_absent_keeps_the_attempt_count_bound(self) -> None:
        attempts = 0

        def fake_urlopen(request, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            raise urllib.error.URLError(TimeoutError("connect timed out"))

        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_PROXY_RETRY_BUDGET_SEC": "0"}, clear=False
        ), mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), mock.patch(
            "opencode_go_proxy.streaming.retry_sleep"
        ):
            try:
                _open_upstream_stream(_req(), _config(), "r", 2)
            except _ConnectFailed as fail:
                assert fail.attempts == 2
            else:  # pragma: no cover
                raise AssertionError("attempt count bound was not applied")
        assert attempts == 3


class TestZenMapping:
    def test_remote_disconnected_becomes_502(self) -> None:
        with _raise(http.client.RemoteDisconnected("closed")):
            try:
                zu._zen_post("https://x", b"{}", {"a": "b"}, _config(), "r",
                             max_retries=0)
            except ProxyError as exc:
                assert int(exc.status) == int(HTTPStatus.BAD_GATEWAY)
            else:  # pragma: no cover
                raise AssertionError("RemoteDisconnected leaked raw")

    def test_timeout_stays_504(self) -> None:
        with _raise(TimeoutError("timed out")):
            try:
                zu._zen_post("https://x", b"{}", {"a": "b"}, _config(), "r",
                             max_retries=0)
            except ProxyError as exc:
                assert int(exc.status) == int(HTTPStatus.GATEWAY_TIMEOUT)
            else:  # pragma: no cover
                raise AssertionError("TimeoutError leaked raw")
