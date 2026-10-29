import gzip
import json
import os
import socket
import subprocess
import unittest
from http import HTTPStatus
from unittest import mock

import zstandard

from opencode_go_proxy.app import (
    ProxyConfig,
    ProxyError,
    _listener_already_active,
    _maybe_dump_request,
    decode_request_body,
)
from opencode_go_proxy.secrets import resolve_api_key
from opencode_go_proxy.trace import _mask_trace_body


def make_config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=8787,
        chat_base_url="https://opencode.ai/zen/go/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=1,
        max_body_bytes=20 * 1024 * 1024,
    )


class CredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reset the module-level cache between tests.
        from opencode_go_proxy.secrets import clear_api_key_cache

        clear_api_key_cache()

    def test_env_key_wins_without_keychain_lookup(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "env-key"}, clear=True), mock.patch("opencode_go_proxy.secrets.subprocess.run") as run:
            self.assertEqual(resolve_api_key(make_config(), "req"), "env-key")

        run.assert_not_called()

    def test_keychain_lookup_uses_first_line(self) -> None:
        completed = subprocess.CompletedProcess(
            ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", "opencode-go-api-key", "-w"],
            0,
            stdout="keychain-key\n",
            stderr="",
        )
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("opencode_go_proxy.secrets.subprocess.run", return_value=completed):
            self.assertEqual(resolve_api_key(make_config(), "req"), "keychain-key")

    def test_missing_key_names_env_and_keychain(self) -> None:
        completed = subprocess.CompletedProcess(
            ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", "opencode-go-api-key", "-w"],
            1,
            stdout="",
            stderr="could not be found",
        )
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "opencode_go_proxy.secrets.subprocess.run", return_value=completed
        ), self.assertRaises(ProxyError) as ctx:
            resolve_api_key(make_config(), "req")

        self.assertEqual(ctx.exception.status, HTTPStatus.UNAUTHORIZED)
        self.assertIn("$OPENCODE_GO_API_KEY", ctx.exception.message)
        self.assertIn("keychain", ctx.exception.message)

    def test_env_falls_back_to_standard_opencode_key(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_API_KEY": "std-key"}, clear=True), mock.patch(
            "opencode_go_proxy.secrets.subprocess.run"
        ) as run:
            self.assertEqual(resolve_api_key(make_config(), "req"), "std-key")

        run.assert_not_called()

    def test_keychain_falls_back_to_codex_router_service(self) -> None:
        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            service = args[args.index("-s") + 1]
            if service == "codex-router-opencode-go":
                return subprocess.CompletedProcess(args, 0, stdout="router-key\n", stderr="")
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "opencode_go_proxy.secrets.subprocess.run", side_effect=fake_run
        ):
            self.assertEqual(resolve_api_key(make_config(), "req"), "router-key")


class RequestBodyDecodeTests(unittest.TestCase):
    def test_identity_body_passes_through(self) -> None:
        body = b'{"a": 1}'
        self.assertEqual(decode_request_body(body, "", 10 * 1024 * 1024), body)
        self.assertEqual(decode_request_body(body, "identity", 10 * 1024 * 1024), body)

    def test_zstd_body_is_decompressed(self) -> None:
        raw = b'{"model": "deepseek-v4.1-flash", "input": "hi"}'
        compressed = zstandard.ZstdCompressor().compress(raw)
        self.assertEqual(decode_request_body(compressed, "zstd", 10 * 1024 * 1024), raw)

    def test_gzip_body_is_decompressed(self) -> None:
        raw = b'{"model": "deepseek-v4.1-flash"}'
        self.assertEqual(decode_request_body(gzip.compress(raw), "gzip", 10 * 1024 * 1024), raw)

    def test_bad_zstd_body_raises_400(self) -> None:
        with self.assertRaises(ProxyError) as ctx:
            decode_request_body(b"not-zstd-data", "zstd", 10 * 1024 * 1024)
        self.assertEqual(ctx.exception.status, HTTPStatus.BAD_REQUEST)

    def test_unsupported_encoding_raises_400(self) -> None:
        with self.assertRaises(ProxyError) as ctx:
            decode_request_body(b"{}", "br", 10 * 1024 * 1024)
        self.assertEqual(ctx.exception.status, HTTPStatus.BAD_REQUEST)

    def test_decompression_is_bounded(self) -> None:
        # A tiny compressed body that would decompress past the cap must fail
        # instead of exhausting memory.
        raw = b"x" * (8 * 1024 * 1024)
        compressed = zstandard.ZstdCompressor().compress(raw)
        with self.assertRaises(ProxyError) as ctx:
            decode_request_body(compressed, "zstd", 1024)
        self.assertEqual(ctx.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

    def test_gzip_decompression_is_bounded(self) -> None:
        # A small gzip body that decompresses past the cap must 413 instead of
        # ballooning into a huge allocation (gzip.decompress has no output cap).
        raw = b"x" * (8 * 1024 * 1024)
        compressed = gzip.compress(raw)
        with self.assertRaises(ProxyError) as ctx:
            decode_request_body(compressed, "gzip", 1024)
        self.assertEqual(ctx.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)


class MaskTraceBodyTests(unittest.TestCase):
    def test_masks_sk_tokens_and_authorization_header(self) -> None:
        masked = _mask_trace_body(
            '{"error":"invalid","Authorization": "Bearer sk-abc123DEF456ghi789jkl"} sk-abc123DEF456ghi789jkl'
        )
        self.assertNotIn("sk-abc123DEF456ghi789jkl", masked)
        self.assertNotIn("Bearer", masked)

    def test_masks_block_form_authorization_header(self) -> None:
        masked = _mask_trace_body("Authorization: Bearer sk-abc123DEF456ghi789jkl")
        self.assertNotIn("sk-abc123DEF456ghi789jkl", masked)
        self.assertNotIn("Bearer", masked)

    def test_leaves_plain_body_untouched(self) -> None:
        body = "upstream is having a bad day"
        self.assertEqual(_mask_trace_body(body), body)

    def test_truncates_to_limit(self) -> None:
        masked = _mask_trace_body("x" * 5000, limit=100)
        self.assertEqual(len(masked), 100)


class CatalogRefreshTimerTests(unittest.TestCase):
    """The background catalog refresh must also run on an interval; the timer
    thread must be daemon (dies with the process) and quiet when refresh is
    disabled via env. Asserted via monkeypatched Thread/sleep, never real
    sleeps."""

    def test_start_catalog_refresh_threads_are_daemon(self) -> None:
        from opencode_go_proxy import app as proxy_app

        with mock.patch.object(proxy_app.threading, "Thread") as thread_cls, mock.patch.dict(
            os.environ, {}, clear=False
        ):
            os.environ.pop("OPENCODE_GO_CATALOG_REFRESH", None)
            thread_cls.return_value = mock.Mock()
            proxy_app._start_catalog_refresh()

        self.assertTrue(all(call.kwargs.get("daemon") is True for call in thread_cls.call_args_list))
        names = [call.kwargs["name"] for call in thread_cls.call_args_list]
        self.assertIn("catalog-refresh", names)
        self.assertIn("catalog-refresh-timer", names)

    def test_start_catalog_refresh_skips_timer_when_disabled(self) -> None:
        from opencode_go_proxy import app as proxy_app

        with mock.patch.object(proxy_app.threading, "Thread") as thread_cls, mock.patch.dict(
            os.environ, {"OPENCODE_GO_CATALOG_REFRESH": "0"}, clear=False
        ):
            thread_cls.return_value = mock.Mock()
            proxy_app._start_catalog_refresh()

        targets = [call.kwargs["target"] for call in thread_cls.call_args_list]
        self.assertIn(proxy_app._refresh_catalog_in_background, targets)
        self.assertNotIn(proxy_app._refresh_catalog_daemon, targets)

    def test_refresh_daemon_loop_ticks_on_interval(self) -> None:
        from opencode_go_proxy import app as proxy_app

        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise KeyboardInterrupt

        with mock.patch.object(proxy_app.time, "sleep", side_effect=fake_sleep), mock.patch.object(
            proxy_app, "_refresh_catalog_once"
        ) as once, mock.patch.dict(
            os.environ, {}, clear=False
        ):
            os.environ.pop("OPENCODE_GO_CATALOG_REFRESH", None)
            with self.assertRaises(KeyboardInterrupt):
                proxy_app._refresh_catalog_daemon(interval_hours=6)

        # Each tick sleeps the full interval before refreshing; the second
        # sleep raises, so exactly one refresh ran.
        self.assertEqual(sleeps, [6 * 3600, 6 * 3600])
        self.assertEqual(once.call_count, 1)

    def test_refresh_daemon_stays_quiet_when_disabled(self) -> None:
        from opencode_go_proxy import app as proxy_app

        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_CATALOG_REFRESH": "0"}, clear=False
        ), mock.patch.object(proxy_app.time, "sleep", side_effect=KeyboardInterrupt), mock.patch.object(
            proxy_app, "_refresh_catalog_once"
        ) as once, self.assertRaises(KeyboardInterrupt):
            proxy_app._refresh_catalog_daemon()

        once.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestMaybeDumpRequest(unittest.TestCase):
    def test_no_env_no_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "r1.json")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OPENCODE_GO_PROXY_DUMP_DIR", None)
                _maybe_dump_request("/v1/responses", "r1", {"model": "m"})
            self.assertFalse(os.path.exists(target))

    def test_env_set_writes_envelope(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "dump")
            payload = {"model": "m", "stream": True, "input": []}
            with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_DUMP_DIR": dest}):
                _maybe_dump_request("/v1/responses", "r9", payload)
            with open(os.path.join(dest, "r9.json"), encoding="utf-8") as handle:
                record = json.load(handle)
        self.assertEqual(record["request_id"], "r9")
        self.assertEqual(record["payload"], payload)
        self.assertNotIn("authorization", json.dumps(record).lower())


class DoubleStartGuardTests(unittest.TestCase):
    """_listener_already_active must see a live listener and stay quiet otherwise."""

    def test_detects_live_listener(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        port = server.getsockname()[1]
        try:
            self.assertTrue(_listener_already_active("0.0.0.0", port))
            self.assertTrue(_listener_already_active("127.0.0.1", port))
        finally:
            server.close()

    def test_free_port_reports_false(self) -> None:
        # Grab then release an ephemeral port: the kernel may hand it back out,
        # but a bind+close race is unlikely to matter for a one-shot probe test.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.assertFalse(_listener_already_active("127.0.0.1", port))
