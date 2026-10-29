"""SSE transport primitives shared by the per-family streaming paths.

Owns the keepalive thread, the single-writer lock for one SSE client, the
client-liveness peek, and the bounded-retry stream opener. Response framing
and event translation live with each wire family (zen_upstream,
anthropic_client, passthrough), which is where the protocol shape is known.
"""

from __future__ import annotations

import http.client
import os
import socket
import threading
import urllib.error
import urllib.request
from typing import Any

from .config import ProxyConfig
from .quota import record_quota_from_headers
from .trace import _mask_trace_body, trace
from .upstream import retriable_http_failure, retry_sleep

DEFAULT_KEEPALIVE_SEC = 15.0


def keepalive_sec() -> float:
    """Keepalive comment interval; override so tests can tick without a 15s wait."""
    try:
        return max(0.05, float(os.environ.get("OPENCODE_GO_PROXY_KEEPALIVE_SEC", str(DEFAULT_KEEPALIVE_SEC))))
    except ValueError:
        return DEFAULT_KEEPALIVE_SEC


def _client_gone(wfile: Any) -> bool:
    """Peek the client socket to detect a peer that closed its read side.

    On macOS a write to a reset peer does not error, so a cancelling client is
    invisible to write-failure detection and the turn would meter as a full
    stream. A non-blocking MSG_PEEK recv observes the peer state directly:
    EOF (b'') when the client closed, an error when it reset. The peek is
    best-effort: any unexpected failure degrades to "alive" so a liveness
    probe can never break a live stream.
    """
    try:
        sock = getattr(wfile, "_sock", None)
        if sock is None:
            # A buffered wfile (makefile with default buffering) hides the
            # socket behind .raw; the real handler uses the unbuffered form.
            sock = getattr(getattr(wfile, "raw", None), "_sock", None)
        if sock is None:
            return False
        was_blocking = sock.getblocking()
        sock.setblocking(False)
        try:
            try:
                data = sock.recv(1, socket.MSG_PEEK)
            except (AttributeError, NotImplementedError):
                # Platform without MSG_PEEK: a plain recv still observes EOF.
                data = sock.recv(1)
            except BlockingIOError:
                return False  # alive: no pending data, connection open
            except (ConnectionResetError, ConnectionAbortedError, OSError):
                return True  # peer reset its connection
            return data == b""  # EOF: peer closed its read side
        finally:
            # Restore blocking mode; a concurrent close makes the restore
            # itself fail, which the outer guard degrades to "alive".
            sock.setblocking(was_blocking)
    except (AttributeError, OSError, ValueError):
        # No peekable socket (test doubles, already closed, unsupported
        # flags): fall back to the old write-only detection.
        return False


class KeepaliveWriter:
    """Keepalive comment thread plus the single write lock for one SSE client.

    Owns the stop event, the write lock, and the liveness flag that relay
    loops share with the keepalive thread, so ``write``/``writelines`` are
    the only paths that touch ``handler.wfile`` and keepalive comments
    interleave safely with stream lines. Both return False once the client
    connection has failed, which also stops further keepalive writes. One
    instance per client stream: hand it to passthrough._relay_stream to keep
    a single thread and lock across a multi-attempt relay instead of one
    hand-rolled copy per relay site.
    """

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._interval = keepalive_sec()
        self._alive = True

    @property
    def alive(self) -> bool:
        return self._alive

    def start(self) -> None:
        threading.Thread(target=self._keepalive, daemon=True).start()

    def _keepalive(self) -> None:
        while not self._stop.wait(self._interval):
            if not self._alive:
                return
            if not self._write_all([b": keepalive\n\n"]):
                return

    def _write_all(self, chunks: list[bytes]) -> bool:
        try:
            with self._lock:
                wfile = self._handler.wfile
                for chunk in chunks:
                    wfile.write(chunk)
                wfile.flush()
            return True
        except (BrokenPipeError, OSError):
            self._alive = False
            return False

    def write(self, data: bytes) -> bool:
        """Write one line and flush; False when the client is gone."""
        return self._write_all([data])

    def writelines(self, lines: list[bytes]) -> bool:
        """Write buffered lines under one lock with a single flush; False when gone."""
        return self._write_all(lines)

    def stop(self) -> None:
        """Signal the keepalive thread to exit; it is a daemon, so no join."""
        self._stop.set()


class _ConnectFailed(Exception):
    """Upstream connect failed after bounded transient retries.

    Carries the terminal exception (HTTPError / URLError / TimeoutError), the
    number of retry attempts burned, and the response body for HTTP errors so
    the caller can decide how to surface it (error event + meter, or a
    verbatim relay of the upstream body).
    """

    def __init__(self, exc: Exception, attempts: int, body: bytes = b"") -> None:
        super().__init__(str(exc))
        self.exc = exc
        self.attempts = attempts
        self.body = body


def _open_upstream_stream(
    req: urllib.request.Request, config: ProxyConfig, request_id: str, max_retries: int
) -> tuple[Any, int]:
    """Open the upstream stream with bounded transient retries.

    Returns ``(response, attempts)``; after retries are exhausted raises
    :class:`_ConnectFailed` with the terminal exception. Quota headers are
    harvested from the successful response.
    """
    attempts = 0
    while True:
        try:
            response = urllib.request.urlopen(req, timeout=config.timeout_sec)
            record_quota_from_headers(getattr(response, "headers", None))
            return response, attempts
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            body = raw.decode("utf-8", errors="replace")
            trace(
                "upstream.error", level="warn",
                request_id=request_id,
                status=exc.code,
                body=_mask_trace_body(body),
                body_len=len(raw),
            )
            if retriable_http_failure(exc.code, body) and attempts < max_retries:
                attempts += 1
                trace("upstream.retry", level="warn", request_id=request_id, attempt=attempts, status=exc.code)
                retry_sleep(attempts)
                continue
            raise _ConnectFailed(exc, attempts, body.encode("utf-8", errors="replace")) from exc
        except (OSError, http.client.HTTPException) as exc:
            # Transient connect-phase failures. OSError covers URLError,
            # timeouts, connection resets, and SSLError (e.g. a bad-record-mac
            # blip that used to escape as an unhandled crash); HTTPException
            # covers the pure-http.client failures incl. RemoteDisconnected (a
            # ConnectionResetError/BadStatusLine hybrid, not a URLError).
            trace("upstream.network_error", level="warn", request_id=request_id, reason=str(getattr(exc, "reason", exc)))
            if attempts < max_retries:
                attempts += 1
                trace("upstream.retry", level="warn", request_id=request_id, attempt=attempts, reason=str(getattr(exc, "reason", exc)))
                retry_sleep(attempts)
                continue
            raise _ConnectFailed(exc, attempts) from exc
