"""Regression tests for the P0/P1/P2 efficiency patch.

P0 (upstream_pool): keep-alive reuse across turns, stale-connection retry,
no pooling for half-read or will-close responses, explicit Connection: close
bypass, and the POOL=0 kill switch. The fakes below stand in for
http.client connections so no real socket ever opens.
P1 (trace): non-blocking enqueue, full-queue drop accounting, TRACE=0 kill.
P2 (SSE fast path): byte-identical output with and without the fast path.
"""

from __future__ import annotations

import json
import os
import queue
import time
import urllib.request
from unittest import mock

from opencode_go_proxy import trace as trace_mod
from opencode_go_proxy import upstream_pool as pool
from opencode_go_proxy.zen_upstream import _restore_responses_sse_line


class FakeSock:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeResp:
    """Minimal http.client.HTTPResponse stand-in."""

    def __init__(self, body: bytes = b"{}", *, will_close: bool = False) -> None:
        self._remaining = body
        self.will_close = will_close
        self.status = 200
        self.reason = "OK"
        self.headers = {"content-type": "application/json"}
        self.fp = object()  # non-None while body unread
        self.closed = False

    def isclosed(self) -> bool:
        return self.fp is None

    def read(self, amt: int | None = None) -> bytes:
        if self.fp is None:
            return b""
        if amt is None or amt < 0:
            data, self._remaining = self._remaining, b""
            self.fp = None
            return data
        data, self._remaining = self._remaining[:amt], self._remaining[amt:]
        if not self._remaining:
            self.fp = None
        return data

    def readline(self, size: int = -1) -> bytes:
        if self.fp is None:
            return b""
        nl = self._remaining.find(b"\n")
        if nl == -1:
            return self.read()
        end = nl + 1
        if size >= 0:
            end = min(end, size if size else len(self._remaining))
        return self.read(end - 0 if False else len(self._remaining[:end]))

    def close(self) -> None:
        self.closed = True
        self.fp = None


class FakeConn:
    instances: list["FakeConn"] = []
    next_failures: list[BaseException] = []
    resp_kwargs: dict = {}

    def __init__(self, host: str, timeout=None, **kwargs) -> None:
        self.host = host
        self.timeout = timeout
        self.requests: list[tuple] = []
        self.sock: FakeSock | None = FakeSock()
        self.closed = False
        type(self).instances.append(self)

    def set_debuglevel(self, level: int) -> None:
        pass

    def request(self, method, selector, data, headers, encode_chunked=False) -> None:
        self.requests.append((method, selector, data, dict(headers)))
        if type(self).next_failures:
            raise type(self).next_failures.pop(0)

    def getresponse(self) -> FakeResp:
        return FakeResp(**type(self).resp_kwargs)

    def close(self) -> None:
        self.closed = True
        if self.sock is not None:
            self.sock.close()
            self.sock = None


def _make_request() -> urllib.request.Request:
    req = urllib.request.Request(
        "https://opencode.ai/zen/go/v1/chat/completions",
        data=b'{"model":"m"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    req.timeout = 30.0
    return req


def _pool_env(**extra: str):
    base = {pool.POOL_ENV: "1", trace_mod.TRACE_ENV: "0"}
    base.update(extra)
    return mock.patch.dict(os.environ, base, clear=False)


class TestKeepAliveReuse:
    def setup_method(self) -> None:
        pool.reset_pool()
        FakeConn.instances.clear()
        FakeConn.next_failures.clear()
        FakeConn.resp_kwargs = {}

    def teardown_method(self) -> None:
        pool.reset_pool()
        FakeConn.instances.clear()
        FakeConn.next_failures.clear()
        FakeConn.resp_kwargs = {}

    def _round_trip(self, handler) -> bytes:
        resp = handler.do_open(FakeConn, _make_request())
        body = resp.read()
        resp.close()
        return body

    def test_second_turn_reuses_connection(self) -> None:
        with _pool_env():
            handler = pool.PooledHTTPSHandler()
            assert self._round_trip(handler) == b"{}"
            assert self._round_trip(handler) == b"{}"
            assert len(FakeConn.instances) == 1
            stats = pool.pool_stats()
            assert stats["fresh_connections"] == 1
            assert stats["reused_connections"] == 1

    def test_no_connection_close_header_sent(self) -> None:
        with _pool_env():
            handler = pool.PooledHTTPSHandler()
            self._round_trip(handler)
            _, _, _, headers = FakeConn.instances[0].requests[0]
            assert "Connection" not in headers

    def test_will_close_response_is_not_pooled(self) -> None:
        with _pool_env():
            FakeConn.resp_kwargs = {"will_close": True}
            handler = pool.PooledHTTPSHandler()
            self._round_trip(handler)
            self._round_trip(handler)
            assert len(FakeConn.instances) == 2
            assert pool.pool_stats()["reused_connections"] == 0

    def test_partial_read_is_not_pooled(self) -> None:
        with _pool_env():
            FakeConn.resp_kwargs = {"body": b'{"a":12345}'}
            handler = pool.PooledHTTPSHandler()
            resp = handler.do_open(FakeConn, _make_request())
            assert resp.read(4) == b'{"a"'
            resp.close()  # body left unread: socket must close, not pool
            assert FakeConn.instances[0].closed is True
            stats = pool.pool_stats()
            assert stats["idle"] == {}

    def test_stale_pooled_connection_retries_once_on_fresh(self) -> None:
        with _pool_env():
            handler = pool.PooledHTTPSHandler()
            self._round_trip(handler)  # parks one idle connection
            FakeConn.next_failures.append(BrokenPipeError(32, "stale idle close"))
            assert self._round_trip(handler) == b"{}"
            assert len(FakeConn.instances) == 2
            assert FakeConn.instances[0].closed is True  # stale one discarded
            assert FakeConn.instances[1].closed is False

    def test_non_stale_error_on_reused_conn_propagates(self) -> None:
        with _pool_env():
            handler = pool.PooledHTTPSHandler()
            self._round_trip(handler)
            FakeConn.next_failures.append(ValueError("boom"))
            try:
                self._round_trip(handler)
            except ValueError:
                pass
            else:  # pragma: no cover - must raise
                raise AssertionError("expected ValueError")
            assert pool.pool_stats()["idle"] == {}

    def test_explicit_connection_close_bypasses_pool(self) -> None:
        with _pool_env():
            handler = pool.PooledHTTPSHandler()
            req = _make_request()
            req.add_header("Connection", "close")
            resp = handler.do_open(FakeConn, req)
            body = resp.read()
            resp.close()
            assert body == b"{}"
            _, _, _, headers = FakeConn.instances[0].requests[0]
            assert headers.get("Connection") == "close"
            # stdlib one-shot semantics close the socket, not the conn object
            assert FakeConn.instances[0].sock is None

    def test_pool_disabled_uses_stdlib_semantics(self) -> None:
        with _pool_env(**{pool.POOL_ENV: "0"}):
            handler = pool.PooledHTTPSHandler()
            resp = handler.do_open(FakeConn, _make_request())
            assert isinstance(resp, FakeResp)  # unwrapped: stdlib path
            assert resp.read() == b"{}"
            _, _, _, headers = FakeConn.instances[0].requests[0]
            assert headers.get("Connection") == "close"
            assert pool.pool_stats()["fresh_connections"] == 0

    def test_error_envelope_read_parks_without_close(self) -> None:
        """HTTPError-style read-but-never-close must not leak the socket."""
        with _pool_env():
            handler = pool.PooledHTTPSHandler()
            resp = handler.do_open(FakeConn, _make_request())
            assert resp.read() == b"{}"  # eager park, no close() call
            assert FakeConn.instances[0].closed is False
            resp2 = handler.do_open(FakeConn, _make_request())
            assert len(FakeConn.instances) == 1
            resp2.read()
            resp2.close()

    def test_truncated_length_is_not_pooled(self) -> None:
        """A Content-Length response cut short detaches silently on readline
        EOF; the unsatisfied length must keep the socket out of the pool."""
        with _pool_env():
            handler = pool.PooledHTTPSHandler()
            resp = handler.do_open(FakeConn, _make_request())
            # Simulate http.client's silent detach on truncated readline EOF:
            # file gone, but 5 bytes still outstanding (read() would have
            # raised IncompleteRead; readline just detaches).
            resp._resp.fp = None  # noqa: SLF001 - white-box pool contract
            resp._resp.length = 5  # noqa: SLF001 - white-box pool contract
            resp.close()
            assert FakeConn.instances[0].closed is True
            assert pool.pool_stats()["idle"] == {}

    def test_streaming_iteration_then_close_pools(self) -> None:
        with _pool_env():
            FakeConn.resp_kwargs = {"body": b'data: {"a":1}\n\n'}
            handler = pool.PooledHTTPSHandler()
            resp = handler.do_open(FakeConn, _make_request())
            chunks = [line for line in resp]
            resp.close()
            assert b"".join(chunks) == b'data: {"a":1}\n\n'
            assert len(FakeConn.instances) == 1
            assert pool.pool_stats()["reused_connections"] == 0
            resp2 = handler.do_open(FakeConn, _make_request())
            resp2.read()
            resp2.close()
            assert len(FakeConn.instances) == 1  # second turn reused it


class TestPoolReporting:
    def setup_method(self) -> None:
        pool.reset_pool()
        pool._opens_since_report = 0
        FakeConn.instances.clear()
        FakeConn.next_failures.clear()
        FakeConn.resp_kwargs = {}

    def teardown_method(self) -> None:
        pool.reset_pool()
        pool._opens_since_report = 0
        FakeConn.instances.clear()
        FakeConn.next_failures.clear()
        FakeConn.resp_kwargs = {}

    def test_report_every_n_opens(self) -> None:
        with mock.patch.dict(os.environ, {pool.POOL_REPORT_ENV: "3"}):
            with mock.patch("opencode_go_proxy.trace.trace") as traced:
                handler = pool.PooledHTTPSHandler()
                for _ in range(3):
                    resp = handler.do_open(FakeConn, _make_request())
                    resp.read()
                    resp.close()
        kinds = [c.args[0] for c in traced.call_args_list]
        assert kinds.count("upstream.pool") == 1
        payload = traced.call_args_list[kinds.index("upstream.pool")].kwargs
        assert payload["fresh_connections"] + payload["reused_connections"] == 3

    def test_report_env_fallback_on_garbage(self) -> None:
        with mock.patch.dict(os.environ, {pool.POOL_REPORT_ENV: "bogus"}):
            assert pool._report_every() == 100


class TestTraceAsync:
    def test_disabled_trace_emits_nothing(self) -> None:
        with mock.patch.dict(os.environ, {trace_mod.TRACE_ENV: "0"}):
            before = trace_mod._queue.qsize()
            trace_mod.trace("unit.test_event", foo="bar")
            assert trace_mod._queue.qsize() == before

    def test_enabled_trace_drains(self) -> None:
        with mock.patch.dict(os.environ, {trace_mod.TRACE_ENV: "1", trace_mod.LEVEL_ENV: "debug"}):
            trace_mod.trace("unit.drain_probe", marker="p0p1p2")
            deadline = time.time() + 5.0
            while not trace_mod._queue.empty() and time.time() < deadline:
                time.sleep(0.01)
            assert trace_mod._queue.empty()

    def test_full_queue_drops_without_blocking(self) -> None:
        with mock.patch.dict(os.environ, {trace_mod.TRACE_ENV: "1", trace_mod.LEVEL_ENV: "debug"}):
            with trace_mod._dropped_lock:
                trace_mod._dropped = 0
            started = time.time()
            with mock.patch.object(
                trace_mod._queue, "put_nowait", side_effect=queue.Full
            ):
                trace_mod.trace("unit.drop_probe")
            assert time.time() - started < 2.0
            assert trace_mod.trace_dropped_count() == 1


class TestRestoreFastPath:
    def test_text_delta_is_byte_identical(self) -> None:
        line = b'data: {"type":"response.output_text.delta","output_index":0,"delta":" hello"}\n'
        assert _restore_responses_sse_line(line) is line

    def test_done_marker_untouched(self) -> None:
        assert _restore_responses_sse_line(b"data: [DONE]\n\n") == b"data: [DONE]\n\n"

    def test_non_data_line_untouched(self) -> None:
        assert _restore_responses_sse_line(b": keepalive\n\n") == b": keepalive\n\n"

    def test_namespaced_call_still_restored(self) -> None:
        event = {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": {"type": "function_call", "name": "codex_app__create_thread", "arguments": "{}"},
        }
        line = b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n"
        out = _restore_responses_sse_line(line)
        parsed = json.loads(out[len(b"data: "):])
        assert parsed["item"]["name"] == "create_thread"
        assert parsed["item"]["namespace"] == "codex_app"

    def test_text_mentioning_both_markers_is_unchanged(self) -> None:
        # Contains "function_call" and "__" as prose, but is not a
        # function_call item: slow path runs and must no-op.
        event = {
            "type": "response.output_text.delta",
            "output_index": 0,
            "delta": "call function_call like foo__bar in code",
        }
        line = b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n"
        out = _restore_responses_sse_line(line)
        assert json.loads(out[len(b"data: "):]) == event
