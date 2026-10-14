"""Network exception mapping: nothing reachable from urlopen may escape raw.

Regression cover for 74 historical request.crashed 500s (ssl.SSLError
BAD_RECORD_MAC, http.client.RemoteDisconnected). Every request-path opener
must map these into _ConnectFailed / ProxyError; TimeoutError must keep its
distinct 504 mapping after the except-clause reorder.
"""

from __future__ import annotations

import http.client
import ssl
import urllib.request
from http import HTTPStatus
from types import SimpleNamespace
from unittest import mock

from opencode_go_proxy import upstream as up
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


class TestChatMapping:
    def test_ssl_error_becomes_502(self) -> None:
        with mock.patch.dict("os.environ", {"OPENCODE_GO_API_KEY": "test"}), \
             _raise(ssl.SSLError("SSLV3_ALERT_BAD_RECORD_MAC")):
            try:
                up.call_upstream_chat({"model": "m", "messages": []},
                                      _config(), "r", max_retries=0)
            except ProxyError as exc:
                assert int(exc.status) == int(HTTPStatus.BAD_GATEWAY)
            else:  # pragma: no cover
                raise AssertionError("SSLError leaked raw")

    def test_timeout_stays_504(self) -> None:
        with mock.patch.dict("os.environ", {"OPENCODE_GO_API_KEY": "test"}), \
             _raise(TimeoutError("timed out")):
            try:
                up.call_upstream_chat({"model": "m", "messages": []},
                                      _config(), "r", max_retries=0)
            except ProxyError as exc:
                assert int(exc.status) == int(HTTPStatus.GATEWAY_TIMEOUT)
            else:  # pragma: no cover
                raise AssertionError("TimeoutError leaked raw")


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
