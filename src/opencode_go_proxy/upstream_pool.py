"""Keep-alive connection pool for upstream HTTPS calls (stdlib only).

Every upstream POST in this proxy goes through ``urllib.request.urlopen``,
and the stdlib opener deliberately sends ``Connection: close`` and tears the
socket down after each response (see ``AbstractHTTPHandler.do_open``). That
means every turn pays a fresh DNS + TCP + TLS handshake to opencode.ai --
typically 100-300ms of time-to-first-token tax that dwarfs all in-process
overhead.

This module provides pooled ``urllib`` handlers that keep idle upstream
connections alive and reuse them across turns. The hot-path call sites stay
untouched: :func:`install` swaps the process-global opener once at server
startup, and every existing ``urllib.request.urlopen`` call transparently
gets a pooled connection. Tests that mock ``urlopen`` are unaffected because
they never reach the handler layer.

Reuse is conservative:

- only connections whose response was fully consumed **and** whose server
  allows persistence (no ``Connection: close``, i.e. ``will_close`` false)
  return to the pool; partially-read or error-cut streams close the socket;
- a pooled connection that died while idle (server-side idle timeout) is
  retried **once** on a fresh connection, transparently;
- pool entries expire after ``OPENCODE_GO_PROXY_POOL_IDLE_SEC`` (default 60s)
  and each origin holds at most ``OPENCODE_GO_PROXY_POOL_SIZE`` idle
  connections (default 8);
- ``OPENCODE_GO_PROXY_POOL=0`` disables pooling entirely (stdlib behaviour).

Only stdlib is used: ``http.client`` connections plus threads.
"""

from __future__ import annotations

import http.client
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from .errors import env_flag

POOL_ENV = "OPENCODE_GO_PROXY_POOL"
POOL_SIZE_ENV = "OPENCODE_GO_PROXY_POOL_SIZE"
POOL_IDLE_ENV = "OPENCODE_GO_PROXY_POOL_IDLE_SEC"
POOL_REPORT_ENV = "OPENCODE_GO_PROXY_POOL_REPORT_EVERY"
_DEFAULT_REPORT_EVERY = 100

_DEFAULT_SIZE = 8
_DEFAULT_IDLE_SEC = 60.0

# Failures that mark a *reused* connection as stale (the server closed it
# while idle) and justify one transparent retry on a fresh connection. Timeouts
# are deliberately excluded: a timeout on a live connection may mean the
# request is already executing upstream, and resending a billed POST could
# double-execute the turn.
_STALE_ERRORS: tuple[type[BaseException], ...] = (
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
    http.client.BadStatusLine,
    http.client.NotConnected,
    http.client.RemoteDisconnected,
)

_lock = threading.Lock()
# Best-effort opens counter for periodic reporting (exactness irrelevant).
_opens_since_report = 0
# key -> list of (connection, last_returned_monotonic)
_pools: dict[tuple[Any, ...], list[tuple[Any, float]]] = {}
_installed = False
_stat_fresh = 0
_stat_reused = 0


def pool_enabled() -> bool:
    """Whether keep-alive pooling is on (default on; ``OPENCODE_GO_PROXY_POOL=0`` off)."""
    return env_flag(POOL_ENV, default_on=True)


def pool_max_size() -> int:
    """Max idle connections kept per origin."""
    try:
        return max(0, int(os.environ.get(POOL_SIZE_ENV, str(_DEFAULT_SIZE))))
    except ValueError:
        return _DEFAULT_SIZE


def pool_idle_sec() -> float:
    """Seconds an idle connection is kept before expiry."""
    try:
        return max(1.0, float(os.environ.get(POOL_IDLE_ENV, str(_DEFAULT_IDLE_SEC))))
    except ValueError:
        return _DEFAULT_IDLE_SEC


def pool_stats() -> dict[str, Any]:
    """Observability snapshot: counters plus current idle holdings."""
    with _lock:
        idle = {f"{key[0]}://{key[1]}": len(conns) for key, conns in _pools.items()}
        return {
            "enabled": pool_enabled(),
            "installed": _installed,
            "fresh_connections": _stat_fresh,
            "reused_connections": _stat_reused,
            "max_size": pool_max_size(),
            "idle_sec": pool_idle_sec(),
            "idle": idle,
        }


def reset_pool() -> None:
    """Close every idle connection and clear counters (tests / re-exec)."""
    global _stat_fresh, _stat_reused
    with _lock:
        for conns in _pools.values():
            for conn, _ in conns:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
        _pools.clear()
        _stat_fresh = 0
        _stat_reused = 0


def _timeout_value(req: urllib.request.Request) -> float | None:
    timeout = req.timeout
    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:  # type: ignore[attr-defined]
        timeout = socket.getdefaulttimeout()
    return timeout


def _pool_key(
    scheme: str, host: str, timeout: float | None, conn_args: dict[str, Any]
) -> tuple[Any, ...]:
    context = conn_args.get("context")
    return (scheme, host.lower(), timeout, id(context) if context is not None else None)


def _checkout(key: tuple[Any, ...]) -> Any | None:
    """Pop a live idle connection, closing expired or dead ones."""
    now = time.monotonic()
    idle = pool_idle_sec()
    with _lock:
        conns = _pools.get(key)
        if not conns:
            _pools.pop(key, None)
            return None
        try:
            while conns:
                conn, returned = conns.pop()
                if now - returned > idle or getattr(conn, "sock", None) is None:
                    _quiet_close(conn)
                    continue
                return conn
            return None
        finally:
            if not conns:
                # Do not accumulate empty per-origin lists: distinct hosts,
                # timeouts, and contexts would otherwise grow the dict
                # without bound over a long-lived process.
                _pools.pop(key, None)


def _quiet_close(obj: Any) -> None:
    """Best-effort close; never raises (pool bookkeeping must not fail a request)."""
    try:
        obj.close()
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


# Discarding a connection is just closing it quietly.
_discard = _quiet_close


def _return_conn(key: tuple[Any, ...], conn: Any) -> None:
    """Park an idle connection, or close it when pooling is off/full."""
    if not pool_enabled() or getattr(conn, "sock", None) is None:
        _quiet_close(conn)
        return
    with _lock:
        conns = _pools.setdefault(key, [])
        if len(conns) >= pool_max_size():
            closed = True
        else:
            conns.append((conn, time.monotonic()))
            closed = False
    if closed:
        _quiet_close(conn)


class _PooledResponse:
    """An ``http.client.HTTPResponse`` wrapper that parks the connection.

    Delegates every attribute to the real response; :meth:`close` parks the
    connection back in the pool when the body was fully consumed and the
    server allows persistence, and closes the socket otherwise. A full
    unbounded :meth:`read` also parks eagerly, because error envelopes
    (``HTTPError``) are read but never closed by the callers -- without the
    eager path every upstream 4xx/5xx would leak its socket.
    """

    def __init__(self, conn: Any, resp: Any, key: tuple[Any, ...]) -> None:
        self._conn = conn
        self._resp = resp
        self._key = key
        self._done = False
        self._broken = False

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_resp"), name)

    def __enter__(self) -> _PooledResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __iter__(self) -> _PooledResponse:
        return self

    def _guarded(self, fn: Any, *args: Any) -> Any:
        """Call `fn`, marking the stream broken on any failure (never pooled)."""
        try:
            return fn(*args)
        except Exception:
            self._broken = True
            raise

    def __next__(self) -> bytes:
        line = self._guarded(self._resp.readline)
        if line == b"":
            raise StopIteration
        return line

    def read(self, amt: int | None = None) -> bytes:
        data = self._guarded(self._resp.read) if amt is None else self._guarded(self._resp.read, amt)
        if amt is None or (isinstance(amt, int) and amt < 0):
            # Unbounded read consumed the whole body; park now so
            # read-but-never-closed error envelopes do not leak sockets.
            self.close()
        return data

    def readline(self, size: int = -1) -> bytes:
        return self._guarded(self._resp.readline, size)

    def close(self) -> None:
        if self._done:
            return
        self._done = True
        resp, conn = self._resp, self._conn
        # A Content-Length response cut short by the server still detaches
        # its file object at EOF (http.client calls _close_conn there), so
        # isclosed() alone cannot prove the stream is complete: an unsatisfied
        # length means the connection is mid-garbage and must not be pooled
        # (a stale reuse would surface as one transparent retry at worst).
        remaining = getattr(resp, "length", None) if resp is not None else None
        if (
            resp is not None
            and conn is not None
            and not self._broken
            and pool_enabled()
            and resp.isclosed()
            and (remaining is None or remaining <= 0)
            and not getattr(resp, "will_close", True)
            and getattr(conn, "sock", None) is not None
        ):
            _return_conn(self._key, conn)
            return
        if conn is not None:
            _quiet_close(conn)
        if resp is not None:
            _quiet_close(resp)


def _request_headers(req: urllib.request.Request) -> dict[str, str] | None:
    """Merge request headers like the stdlib opener; None means bypass pool.

    Returns None when the caller explicitly asked for ``Connection: close``,
    in which case the stdlib one-shot path is used verbatim.
    """
    headers = dict(req.unredirected_hdrs)
    headers.update({k: v for k, v in req.headers.items() if k not in headers})
    headers = {name.title(): val for name, val in headers.items()}
    if headers.get("Connection", "").lower() == "close":
        return None
    headers.pop("Connection", None)
    return headers


def _send_on(
    conn: Any,
    method: str,
    selector: str,
    data: bytes | None,
    headers: dict[str, str],
    encode_chunked: bool,
) -> Any:
    conn.request(method, selector, data, headers, encode_chunked=encode_chunked)
    return conn.getresponse()


def _pooled_do_open(
    handler: Any, http_class: Any, req: urllib.request.Request, http_conn_args: dict[str, Any]
) -> Any:
    global _stat_fresh, _stat_reused
    scheme = urlsplit(req.get_full_url()).scheme.lower()
    host = req.host
    if not host:
        raise urllib.error.URLError("no host given")
    timeout = _timeout_value(req)
    key = _pool_key(scheme, host, timeout, http_conn_args)
    headers = _request_headers(req)
    if headers is None:
        # Explicit `Connection: close`: stdlib one-shot semantics, verbatim.
        # NOTE: do NOT use super(type(handler), handler) here -- the MRO
        # routes back through _PooledOpenMixin.do_open and recurses forever.
        return urllib.request.AbstractHTTPHandler.do_open(
            handler, http_class, req, **http_conn_args
        )
    method = req.get_method()
    selector = req.selector
    data = req.data
    encode_chunked = req.has_header("Transfer-encoding")
    debuglevel = getattr(handler, "_debuglevel", 0)

    conn = _checkout(key)
    if conn is not None:
        try:
            resp = _send_on(conn, method, selector, data, headers, encode_chunked)
        except _STALE_ERRORS:
            # The server closed this idle connection first; one transparent
            # retry on a fresh connection (the request was never processed).
            _quiet_close(conn)
            conn = None
        except OSError as err:
            _quiet_close(conn)
            raise urllib.error.URLError(err)
        except Exception:
            _quiet_close(conn)
            raise
        else:
            with _lock:
                _stat_reused += 1
            wrapped = _wrap(req, key, conn, resp)
            _note_open()
            return wrapped

    conn = http_class(host, timeout=timeout, **http_conn_args)
    try:
        conn.set_debuglevel(debuglevel)
    except Exception:  # noqa: BLE001 - debug level is best-effort
        pass
    try:
        resp = _send_on(conn, method, selector, data, headers, encode_chunked)
    except OSError as err:
        _quiet_close(conn)
        raise urllib.error.URLError(err)
    except Exception:
        _quiet_close(conn)
        raise
    with _lock:
        _stat_fresh += 1
    wrapped = _wrap(req, key, conn, resp)
    _note_open()
    return wrapped


def _report_every() -> int:
    """Opens between ``upstream.pool`` trace reports."""
    try:
        return max(1, int(os.environ.get(POOL_REPORT_ENV, str(_DEFAULT_REPORT_EVERY))))
    except ValueError:
        return _DEFAULT_REPORT_EVERY


def _note_open() -> None:
    """Trace pool counters every N successful opens.

    Answers "was pooling worth it" from the logs without a separate
    dashboard: fresh vs reused counts plus idle holdings.
    """
    global _opens_since_report
    _opens_since_report += 1
    if _opens_since_report >= _report_every():
        _opens_since_report = 0
        from .trace import trace

        trace("upstream.pool", level="debug", **pool_stats())


def _wrap(
    req: urllib.request.Request, key: tuple[Any, ...], conn: Any, resp: Any
) -> _PooledResponse:
    wrapped = _PooledResponse(conn, resp, key)
    wrapped.url = req.get_full_url()
    wrapped.msg = resp.reason
    return wrapped


class _PooledOpenMixin:
    """Route ``do_open`` through the pool, else fall back to stdlib."""

    def do_open(self, http_class: Any, req: urllib.request.Request, **http_conn_args: Any) -> Any:
        if not pool_enabled() or getattr(req, "_tunnel_host", None):
            # Same MRO note as above: super() from the mixin would be
            # correct here (it lands past the mixin), but spell out the
            # stdlib target explicitly so a future MRO shuffle cannot
            # reintroduce the recursion.
            return urllib.request.AbstractHTTPHandler.do_open(
                self, http_class, req, **http_conn_args
            )
        return _pooled_do_open(self, http_class, req, http_conn_args)


class PooledHTTPHandler(_PooledOpenMixin, urllib.request.HTTPHandler):
    """HTTP handler with keep-alive pooling (plain HTTP origins)."""


class PooledHTTPSHandler(_PooledOpenMixin, urllib.request.HTTPSHandler):
    """HTTPS handler with keep-alive pooling (all upstream origins)."""


def install() -> bool:
    """Install the pooled opener process-wide; returns False when disabled.

    Idempotent. Every ``urllib.request.urlopen`` call in this process -- the
    chat completions path, the Responses-family relay, the zen client, the
    caption sub-calls -- transparently reuses keep-alive connections from
    here on. Call once at server startup.
    """
    global _installed
    if not pool_enabled():
        return False
    opener = urllib.request.build_opener(PooledHTTPHandler(), PooledHTTPSHandler())
    urllib.request.install_opener(opener)
    _installed = True
    return True
