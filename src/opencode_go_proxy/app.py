"""Thin HTTP dispatch for the Responses-API shim.

Route handling and request plumbing live here; secrets, the upstream client,
and the per-family translation paths live in their own modules.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import select
import signal
import socket
import sys
import threading
import time
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .anthropic_client import (
    COUNT_TOKENS_PATHS,
    anthropic_error_payload,
    count_messages_tokens,
    handle_anthropic_messages_request,
)
from .compaction import COMPACT_PATHS, handle_compaction, has_compaction_trigger
from .config import ProxyConfig, resolve_chat_base_url
from .errors import ProxyError
from .go_upstream import (
    handle_go_chat_request,
    handle_go_responses_request,
)
from .guards import (
    check_browser_origin,
    check_client,
    check_content_type,
    check_host,
    validate_bind_security,
)
from .passthrough import relay_native_request
from .protocol import known_models, require_model
from .quota import read_quota_state
from .routing import is_known_model_slug, route_target
from .state import build_state
from .trace import trace
from .zen_upstream import (
    handle_zen_chat_request,
    handle_zen_responses_request,
)

Json = dict[str, Any]

RESPONSES_PATHS = {"/responses", "/v1/responses", "/responses/compact", "/v1/responses/compact"}
CHAT_COMPLETIONS_PATHS = {"/chat/completions", "/v1/chat/completions"}
MESSAGES_PATHS = {"/messages", "/v1/messages"}
_KNOWN_POST_PATHS = RESPONSES_PATHS | CHAT_COMPLETIONS_PATHS | MESSAGES_PATHS | COUNT_TOKENS_PATHS
_ANTHROPIC_SURFACE_PATHS = MESSAGES_PATHS | COUNT_TOKENS_PATHS
class ProxyHTTPServer(ThreadingHTTPServer):
    """HTTP server that does not perform reverse DNS during bind."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])


def _listener_already_active(bind: str, port: int) -> bool:
    """True when another socket already accepts connections on this port.

    TCP connect probe (no proxies, short timeout): SO_REUSEADDR on Windows
    lets a second bind silently succeed alongside a live listener, so the
    only reliable double-start signal is somebody actually answering.
    """
    probe_host = "127.0.0.1" if bind in {"0.0.0.0", ""} else bind
    try:
        with socket.create_connection((probe_host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _decompress_bounded(reader: Any, cap: int) -> bytes:
    """Read `reader` in chunks, raising 413 once the output exceeds `cap`.

    Both zstd and gzip frames can declare an output size that is only a hint,
    so a zip bomb must be bounded by counting bytes as they come out, never by
    trusting the frame header or decompressing the whole body at once.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = reader.read(1 << 16)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise ProxyError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "decompressed request body exceeds the size cap",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def decode_request_body(raw: bytes, content_encoding: str, max_body_bytes: int) -> bytes:
    """Decode an HTTP request body according to its Content-Encoding header.

    The Codex desktop app sends /v1/responses bodies zstd-compressed whenever it
    is authenticated (codex-rs: `Compression::Zstd`), so a proxy that serves the
    app must decompress. gzip is supported as well; anything else is rejected
    explicitly rather than crashing on a magic byte.
    """
    encoding = (content_encoding or "").strip().lower()
    if not encoding or encoding in {"identity", "utf-8"}:
        return raw
    if encoding == "zstd":
        try:
            import zstandard as zstd
        except ImportError as exc:  # pragma: no cover - env without the dep
            raise ProxyError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "zstd request bodies require the 'zstandard' package (pip install zstandard)",
            ) from exc
        cap = max(max_body_bytes * 4, 1 << 20)
        try:
            with zstd.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as reader:
                return _decompress_bounded(reader, cap)
        except ProxyError:
            raise
        except Exception as exc:
            raise ProxyError(HTTPStatus.BAD_REQUEST, f"failed to decompress zstd request body: {exc}") from exc
    if encoding in {"gzip", "x-gzip"}:
        # Same bounded decompression as zstd: a tiny gzip wire body must not be
        # able to balloon into a multi-GB allocation (gzip.decompress has no cap).
        cap = max(max_body_bytes * 4, 1 << 20)
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as reader:
                return _decompress_bounded(reader, cap)
        except ProxyError:
            raise
        except Exception as exc:
            raise ProxyError(HTTPStatus.BAD_REQUEST, f"failed to decompress gzip request body: {exc}") from exc
    raise ProxyError(HTTPStatus.BAD_REQUEST, f"unsupported content-encoding: {content_encoding}")


def _maybe_dump_request(path: str, request_id: str, payload: Json) -> None:
    """Persist the exact client request body when debugging is opted in.

    Debug only, for offline replay (bisecting gateway 400s): explicit opt-in
    via ``OPENCODE_GO_PROXY_DUMP_DIR``; the body carries no credentials (auth
    travels in headers and is never dumped). Best-effort: never break a request.
    """
    dump_dir = os.environ.get("OPENCODE_GO_PROXY_DUMP_DIR", "").strip()
    if not dump_dir:
        return
    try:
        os.makedirs(dump_dir, exist_ok=True)
        with open(
            os.path.join(dump_dir, f"{request_id}.json"),
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                {
                    "ts": time.time(),
                    "request_id": request_id,
                    "path": path,
                    "model": payload.get("model"),
                    "stream": payload.get("stream", False),
                    "payload": payload,
                },
                handle,
                ensure_ascii=False,
            )
    except OSError:
        pass


# Upper bound on how much unread request body an error response drains before
# the connection closes; past this the client is hostile or oversized anyway.
_ERROR_BODY_DRAIN_CAP = 1024 * 1024
# How long the drain waits for body bytes still in flight before giving up:
# clients send headers and body as separate writes, so the body can lag the
# guard decision by a scheduling beat, but a declared-and-never-sent remainder
# (oversized-body rejection) must be answered, not waited for.
_ERROR_BODY_DRAIN_WAIT_SEC = 0.05


class ResponsesProxyHandler(BaseHTTPRequestHandler):
    # How much of the declared request body has been consumed; reset per
    # request so the error-path drain knows what is still unread.
    _body_bytes_read = 0

    def log_message(self, format: str, *args: Any) -> None:
        """Route the stdlib access log through the leveled trace stream.

        BaseHTTPRequestHandler writes one plain-text line per request straight
        to stderr, bypassing every level filter: it could not be silenced by
        OPENCODE_GO_PROXY_LOG_LEVEL or OPENCODE_GO_PROXY_TRACE and made up
        roughly half of the log volume. Emitting it as a debug-level record
        puts it under the same switch as everything else and keeps the
        machine-readable one-object-per-line format.
        """
        trace("http.access", level="debug", message=format % args)

    def _guard_request(self) -> None:
        """Enforce the local or explicitly authenticated remote boundary."""
        check_host(self.headers.get("Host"))
        check_browser_origin(self.headers)
        check_client(self.client_address[0], self.headers)

    @staticmethod
    def _error_payload(exc: ProxyError) -> Json:
        return {"error": {"type": exc.error_type or "proxy_error", "message": exc.message}}

    def _drain_request_body(self) -> None:
        """Consume request bytes already received so the close is a clean FIN.

        Writing an error while unread body bytes sit in the kernel receive
        buffer turns the close into an RST, and on Windows that RST can
        discard the already-queued response before the client reads it
        (observed as WinError 10053 instead of the status code). Drain only
        what has actually arrived - never waiting for a declared-but-unsent
        remainder (an oversized-body rejection must answer immediately), and
        stop as soon as no more data is instantly available. Bytes already
        buffered in ``rfile`` are already off the kernel buffer and cannot
        trigger the RST, so reading the socket directly is sufficient.
        """
        try:
            declared = int(self.headers.get("content-length", "0"))
        except ValueError:
            return
        remaining = min(max(declared - self._body_bytes_read, 0), _ERROR_BODY_DRAIN_CAP)
        try:
            while remaining > 0:
                if not select.select([self.request], [], [], _ERROR_BODY_DRAIN_WAIT_SEC)[0]:
                    return
                chunk = self.request.recv(min(remaining, 65536))
                if not chunk:
                    return
                remaining -= len(chunk)
        except OSError:
            return

    def _send_proxy_error(self, exc: ProxyError, *, payload: Json | None = None) -> None:
        """Write the proxy error envelope, forwarding upstream retry-after.

        Only the upstream's retry-after header is copied (lowercase key); the
        rest of the upstream response stays out of the envelope. ``payload``
        replaces the default envelope body (the /v1/messages surface speaks
        the Anthropic error shape).
        """
        self._drain_request_body()
        headers = None
        if exc.headers and exc.headers.get("retry-after"):
            headers = {"retry-after": exc.headers["retry-after"]}
        self._send_json(payload or self._error_payload(exc), status=exc.status, headers=headers)

    @staticmethod
    def _request_model(payload: Json) -> str:
        raw_model = require_model(payload)
        if not is_known_model_slug(raw_model):
            raise ProxyError(
                HTTPStatus.BAD_REQUEST,
                f"unknown model {raw_model!r}; refresh the catalog or add it to user-models.json",
                error_type="model_not_found",
            )
        return raw_model

    def _reject_websocket_upgrade(self) -> bool:
        """Reject a realtime WebSocket upgrade with HTTP/1.1 426.

        The Codex app opens a WebSocket upgrade for realtime; BaseHTTPRequestHandler
        defaults to HTTP/1.0 and answers with an HTTP/1.0 status line, which the client
        rejects as "WebSocket protocol error: HTTP version must be 1.1 or higher".
        Answering the raw upgrade with HTTP/1.1 426 Upgrade Required makes the client
        fall back to plain HTTP streaming cleanly (mirrors codex-router src/router.mjs).
        """
        connection = (self.headers.get("Connection") or "").lower()
        upgrade = (self.headers.get("Upgrade") or "").lower()
        if "upgrade" not in connection or upgrade != "websocket":
            return False
        self.wfile.write(
            b"HTTP/1.1 426 Upgrade Required\r\n"
            b"Connection: close\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        self.wfile.flush()
        self.close_connection = True
        return True

    def do_GET(self) -> None:
        # A realtime WebSocket upgrade must answer 426 before any auth guard:
        # upgrades carry browser markers (Origin) that would otherwise 403.
        if self._reject_websocket_upgrade():
            return
        try:
            self._guard_request()
        except ProxyError as exc:
            trace("request.failed", level="warn", status=exc.status, message=exc.message)
            self._send_proxy_error(exc)
            return
        if self.path in {"/health", "/v1/health"}:
            self._send_json({"status": "ok"})
            return
        if self.path in {"/quota", "/v1/quota"}:
            self._send_json(read_quota_state())
            return
        if self.path in {"/state", "/v1/state"}:
            config = self._config()
            self._send_json(build_state(port=config.port, upstream=config.chat_base_url))
            return
        version_path = self.path.split("?", 1)[0]
        if version_path in {"/version", "/v1/version"}:
            from opencode_go_proxy import updates

            force = "1" in parse_qs(urlsplit(self.path).query).get("force", [])
            self._send_json(updates.version_payload(force=force))
            return
        if self.path in {"/cache", "/v1/cache", "/metrics", "/v1/metrics"}:
            config = self._config()
            self._send_json(config.cache_tracker.snapshot())
            return
        if self.path in {"/models", "/v1/models"}:
            from opencode_go_proxy import catalog as _catalog

            slugs = _catalog.merged_model_slugs() or sorted(known_models())
            self._send_json({
                "object": "list",
                "data": [{"id": slug, "object": "model"} for slug in slugs],
            })
            return
        if self.path in MESSAGES_PATHS:
            self._send_json(
                {"error": {"message": "method not allowed"}},
                status=HTTPStatus.METHOD_NOT_ALLOWED,
            )
            return
        self._send_json({"error": {"message": "not found"}}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        request_id = uuid.uuid4().hex[:12]
        self._body_bytes_read = 0
        # The Codex app may send ?compact=true on the responses path; the query
        # string must not turn a known path into a 404. The payload translates
        # through the normal path either way (compact is just a hint).
        path = self.path.split("?", 1)[0]
        # The /v1/messages surface speaks the Anthropic error taxonomy, so its
        # ProxyError failures render in that shape instead of proxy_error.
        anthropic_surface = path in _ANTHROPIC_SURFACE_PATHS

        try:
            self._guard_request()
            if path not in _KNOWN_POST_PATHS:
                self._drain_request_body()
                self._send_json({"error": {"message": "not found"}}, status=HTTPStatus.NOT_FOUND)
                return
            check_content_type(self.headers.get("content-type"))
            config = self._config()
            payload = self._read_json(config)
            _maybe_dump_request(path, request_id, payload)
            if path in COUNT_TOKENS_PATHS:
                self._send_json({"input_tokens": count_messages_tokens(payload)})
                return
            model = self._request_model(payload)
            trace(
                "request.received", level="debug",
                request_id=request_id,
                path=self.path,
                model=model,
                stream=payload.get("stream", False),
            )
            # Remote compaction (v1: the dedicated /responses/compact path;
            # v2: a responses request whose input ends in a compaction trigger
            # item). Native models fall through to the ChatGPT backend relay,
            # which handles compaction itself.
            is_compaction = (path in COMPACT_PATHS) or (
                path in RESPONSES_PATHS and has_compaction_trigger(payload)
            )
            if is_compaction and route_target(model) != "native":
                handle_compaction(self, payload, config, request_id, path=path)
                return
            if path in MESSAGES_PATHS:
                if route_target(model) != "opencode_go":
                    raise ProxyError(
                        HTTPStatus.BAD_REQUEST,
                        "/messages is available only for opencode-go models",
                        error_type="invalid_request_error",
                    )
                # Family dispatch inside: Messages-family models relay
                # verbatim; Chat/Responses families translate both directions.
                handle_anthropic_messages_request(self, payload, config, request_id)
            elif path in CHAT_COMPLETIONS_PATHS:
                handle_chat_completions_request(self, payload, config, request_id)
            elif path in RESPONSES_PATHS and route_target(model) == "native":
                # Native models relay whole to the ChatGPT backend: the body,
                # the client's own auth, and the stream are forwarded verbatim
                # and never touch the OpenCode Go key or alias logic.
                relay_native_request(self, payload, config, request_id)
            elif path in RESPONSES_PATHS and route_target(model) == "zen":
                # Zen models translate per wire family inside the zen client;
                # they must never reach the opencode-go translation path.
                handle_zen_responses_request(self, payload, config, request_id)
            else:
                handle_go_responses_request(self, payload, config, request_id)
        except ProxyError as exc:
            trace("request.failed", level="warn", request_id=request_id, status=exc.status, message=exc.message)
            self._send_proxy_error(
                exc, payload=anthropic_error_payload(exc) if anthropic_surface else None
            )
        except BrokenPipeError:
            trace("client.disconnected", level="info", request_id=request_id, message="client closed connection during stream")
        except Exception as exc:  # pragma: no cover - defensive crash trace  # noqa: BLE001
            trace("request.crashed", level="error", request_id=request_id, message=str(exc), traceback=traceback.format_exc())
            crash = ProxyError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "proxy crashed; see stderr trace",
                error_type="proxy_crash",
            )
            try:
                self._send_proxy_error(
                    crash, payload=anthropic_error_payload(crash) if anthropic_surface else None
                )
            except BrokenPipeError:
                pass

    def _config(self) -> ProxyConfig:
        return self.server.config  # type: ignore[attr-defined]

    def _read_json(self, config: ProxyConfig) -> Json:
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            raise ProxyError(HTTPStatus.BAD_REQUEST, "invalid content-length header")
        if length < 0:
            raise ProxyError(HTTPStatus.BAD_REQUEST, "negative content-length")
        if length > config.max_body_bytes:
            raise ProxyError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"request body exceeds {config.max_body_bytes // (1024*1024)}MB cap")
        raw = self.rfile.read(length)
        self._body_bytes_read = len(raw)
        if not raw:
            return {}
        body = decode_request_body(raw, self.headers.get("content-encoding", ""), config.max_body_bytes)
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProxyError(HTTPStatus.BAD_REQUEST, "request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ProxyError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
        return value

    def _send_json(self, payload: Json, status: HTTPStatus = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(payload, separators=(",",":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def handle_chat_completions_request(handler: ResponsesProxyHandler, payload: Json, config: ProxyConfig, request_id: str) -> None:
    """Verbatim /chat/completions passthrough for both stream modes.

    Non-stream relays the upstream status and JSON body verbatim, including the
    upstream's own error body on 4xx/5xx (never a ``proxy_error`` envelope).
    Stream commits SSE only after the upstream answers 200 and relays its bytes
    unchanged. A missing key surfaces the same 401 proxy error the responses
    path uses.
    """
    if route_target(payload.get("model") or "") == "zen":
        # Zen models relay to the zen chat-completions surface with the zen
        # key and base; the opencode-go upstream is never involved.
        handle_zen_chat_request(handler, payload, config, request_id)
        return
    handle_go_chat_request(handler, payload, config, request_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex Responses API shim for OpenAI Chat Completions upstreams")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--bind", default=os.environ.get("OPENCODE_GO_PROXY_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("OPENCODE_GO_PROXY_PORT", "8787")))
    parser.add_argument(
        "--chat-base-url",
        dest="chat_base_url",
        default=resolve_chat_base_url(),
    )
    parser.add_argument("--api-key-env", default=os.environ.get("OPENCODE_GO_PROXY_API_KEY_ENV", "OPENCODE_GO_API_KEY"))
    parser.add_argument("--timeout-sec", type=float, default=float(os.environ.get("OPENCODE_GO_PROXY_TIMEOUT_SEC", "180")))
    parser.add_argument("--max-body-mb", type=int, default=int(os.environ.get("OPENCODE_GO_PROXY_MAX_BODY_MB", "20")))
    return parser


CATALOG_REFRESH_INTERVAL_HOURS = 6


def _refresh_catalog_once(reason: str) -> None:
    """Run one TTL-gated runtime catalog refresh; failures are logged, never fatal."""
    trace("catalog.refresh.start", level="debug", reason=reason)
    try:
        from opencode_go_proxy import catalog as _catalog
        from opencode_go_proxy.zen_catalog import capture_zen_models

        # Zen capture is TTL-gated and never raises (network failure falls back
        # to cache); without it the merged catalog has no zen/ slugs.
        capture_zen_models()
        _catalog.refresh_runtime_catalog()
        _catalog.render_merged_catalog()
    except Exception as exc:  # noqa: BLE001 - background refresh is best-effort
        trace("catalog.refresh.skipped", level="warn", error=str(exc))


def _refresh_catalog_in_background() -> None:
    """Run the startup catalog refresh in a daemon thread; failures are logged, never fatal."""
    _refresh_catalog_once(reason="startup")


def _catalog_refresh_enabled() -> bool:
    from opencode_go_proxy import catalog as _catalog

    return os.environ.get(_catalog.CATALOG_REFRESH_ENV) != "0"


def _refresh_catalog_daemon(interval_hours: float = CATALOG_REFRESH_INTERVAL_HOURS) -> None:
    """Re-run the catalog refresh on an interval until the process exits.

    The refresh is TTL-gated and best-effort, so each tick is cheap: a fresh
    compact skips discovery entirely, and a stale one issues a conditional GET
    against models.dev. The thread is daemon=True, so it dies with the
    process; when catalog refresh is disabled via OPENCODE_GO_CATALOG_REFRESH
    the loop stays quiet.
    """
    while True:
        time.sleep(interval_hours * 3600)
        if not _catalog_refresh_enabled():
            continue
        _refresh_catalog_once(reason="timer")


def _start_catalog_refresh() -> None:
    """Start the startup refresh plus the interval timer, both daemon threads.

    The startup refresh always runs (refresh_catalog itself self-gates on the
    env); the timer only when catalog refresh is enabled. daemon=True lets both
    die with the process.
    """
    threading.Thread(target=_refresh_catalog_in_background, daemon=True, name="catalog-refresh").start()
    if _catalog_refresh_enabled():
        threading.Thread(
            target=_refresh_catalog_daemon, daemon=True, name="catalog-refresh-timer"
        ).start()


def main(argv: list[str] | None = None) -> None:
    args_list = list(sys.argv[1:] if argv is None else argv)
    # Operational subcommands must be reachable from the installed console
    # script (entry point is app:main), not only via python -m. Dispatch them
    # before any server startup.
    if "--refresh-catalog" in args_list or os.environ.get("OPENCODE_GO_PROXY_REFRESH_CATALOG") == "1":
        args_list = [a for a in args_list if a != "--refresh-catalog"]
        from . import catalog

        sys.exit(catalog.main_refresh(args_list))
    if args_list and args_list[0] == "doctor":
        from . import ops

        sys.exit(ops.doctor(args_list[1:]))
    if args_list and args_list[0] == "smoke-test":
        from . import ops

        sys.exit(ops.smoke_test(args_list[1:]))
    if args_list and args_list[0] in {"setup", "install"}:
        from . import ops

        sys.exit(ops.install(args_list[1:]))
    if args_list and args_list[0] == "install-skills":
        from . import ops

        sys.exit(ops.install_skills(args_list[1:]))
    if args_list and args_list[0] == "agents-sync":
        from . import agents_sync

        sys.exit(agents_sync.agents_sync_cmd(args_list[1:]))
    if args_list and args_list[0] == "status":
        from . import ops

        sys.exit(ops.status(args_list[1:]))
    if args_list and args_list[0] == "support-bundle":
        from . import ops

        sys.exit(ops.support_bundle(args_list[1:]))
    if args_list and args_list[0] == "config":
        from . import config_manager

        sys.exit(config_manager.config_cmd(args_list[1:]))
    if args_list and args_list[0] == "models":
        from . import models_cmd

        sys.exit(models_cmd.models_cmd(args_list[1:]))
    if args_list and args_list[0] == "native-capture":
        from . import native_models

        sys.exit(native_models.native_capture_cmd(args_list[1:]))
    if args_list and args_list[0] == "refresh-runtime":
        from . import catalog, native_models

        # The menu bar's Refresh Catalog: re-capture the native set, then
        # force a runtime render under the state dir (the catalog the proxy
        # actually serves and the config block points Codex at).
        try:
            native_models.capture_native_models()
        except native_models.NativeCaptureError as exc:
            print(f"warning: native capture skipped: {exc}")
        rendered = catalog.refresh_runtime_catalog(force=True)
        print(f"runtime catalog refreshed: {len(rendered.get('models', []))} models")
        catalog.render_merged_catalog()
        sys.exit(0)
    if args_list and args_list[0] == "update":
        from . import ops

        sys.exit(ops.update_cmd(args_list[1:]))
    args = build_parser().parse_args(args_list)
    if args.timeout_sec <= 0:
        sys.stderr.write(f"error: --timeout-sec must be positive, got {args.timeout_sec}\n")
        sys.exit(2)
    try:
        validate_bind_security(args.bind)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        sys.exit(2)
    try:
        from opencode_go_proxy import catalog as _catalog
        from opencode_go_proxy import native_models

        # Startup fast path: render the state-dir compact (or the seed) with
        # no network, capture the native catalog so a fresh install serves
        # official GPT models immediately (best-effort: a missing or logged-
        # out codex binary keeps the last snapshot), then render the merged
        # native + opencode-go catalog. The full refresh below runs in the
        # background.
        _catalog.prepare_runtime_catalog()
        native_models.capture_native_models()
        _catalog.render_merged_catalog()
    except Exception as exc:  # noqa: BLE001 - startup catalog render is best-effort
        trace("catalog.refresh.skipped", level="warn", error=str(exc))
    config = ProxyConfig(
        bind=args.bind,
        port=args.port,
        chat_base_url=args.chat_base_url,
        api_key_env=args.api_key_env,
        timeout_sec=args.timeout_sec,
        max_body_bytes=args.max_body_mb * 1024 * 1024,
    )
    if config.bind not in {"127.0.0.1", "localhost", "::1"}:
        trace(
            "security.remote_enabled", level="info",
            bind=config.bind,
            message="non-loopback listener protected by a separate caller token",
        )
    from .upstream_pool import install as _install_upstream_pool

    # Keep-alive upstream connections: one process-wide opener install removes
    # the per-turn DNS + TCP + TLS handshake tax on every urlopen below
    # (chat completions, Responses-family relay, zen, captions). Disabled with
    # OPENCODE_GO_PROXY_POOL=0; never fatal to startup.
    try:
        _install_upstream_pool()
    except Exception as exc:  # noqa: BLE001 - fall back to unpooled stdlib
        trace("upstream.pool_install_failed", level="warn", error=str(exc))
    if _listener_already_active(config.bind, config.port):
        # Refuse to become a second listener (see helper docstring).
        trace(
            "server.start_refused", level="error",
            bind=config.bind,
            port=config.port,
            reason="another instance is already listening on this port",
        )
        return 1
    server = ProxyHTTPServer((config.bind, config.port), ResponsesProxyHandler)
    server.config = config  # type: ignore[attr-defined]
    trace(
        "server.start", level="info",
        bind=config.bind,
        port=config.port,
        chat_base_url=config.chat_base_url,
        api_key_env=config.api_key_env,
    )
    # serve_forever in a background thread: shutdown() from a signal handler
    # running on the main thread would otherwise deadlock (both need the main
    # thread), leaving the process unkillable via SIGTERM.
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    signal.signal(signal.SIGTERM, lambda *_: server.shutdown())
    try:
        serve_thread.start()
        # Network catalog refresh starts only after the listener is ready, so
        # slow discovery cannot delay local health checks or client startup.
        _start_catalog_refresh()
        serve_thread.join()
    except KeyboardInterrupt:
        trace("server.stop", level="info", reason="keyboard_interrupt")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
