"""Semantically unchanged relay of native-model requests to the ChatGPT Codex backend.

The JSON payload is parsed and re-serialized (whitespace and key order are
not preserved), the client's auth and request headers are forwarded, safe
upstream response headers are relayed back, and the backend base URL is
required to be https unless the local-development escape hatch is set.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

from .config import ProxyConfig
from .errors import ProxyError
from .guards import caller_bearer_matches, caller_xapi_key_matches
from .meter import record_usage_event
from .streaming import KeepaliveWriter
from .trace import trace

Json = dict[str, Any]

NATIVE_BASE_URL_DEFAULT = "https://chatgpt.com/backend-api/codex"
NATIVE_BASE_URL_ENV = "OPENCODE_GO_PROXY_NATIVE_BASE_URL"
NATIVE_INSECURE_ENV = "OPENCODE_GO_PROXY_NATIVE_ALLOW_INSECURE"
NATIVE_PROVIDER = "native"

# The fixed headers relayed from the client request, plus every x-* header
# that is not an x-opencode-go-* proxy control header.
FORWARD_HEADERS = ("authorization", "content-type", "accept", "user-agent")

# Headers that must never be forwarded end-to-end (RFC 9110 hop-by-hop plus
# anything a relay owns itself).
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def resolve_native_base_url(explicit: str | None = None) -> str:
    """Native backend base URL: explicit value, env override, then default."""
    if explicit:
        return explicit.rstrip("/")
    value = os.environ.get(NATIVE_BASE_URL_ENV)
    if value:
        return value.rstrip("/")
    return NATIVE_BASE_URL_DEFAULT


def is_stream_terminal(line: bytes) -> bool:
    """True when an SSE line ends the stream: [DONE] or response.completed.

    The completed match is on the quoted event value, so both compact JSON
    (``{"type":"response.completed",...}``) and spaced serialization
    (``{"type": "response.completed",...}``) count as terminal.
    """
    return b"[DONE]" in line or b'"response.completed"' in line


def _forwarded_headers(handler: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name in FORWARD_HEADERS:
        value = handler.headers.get(name)
        if value:
            headers[name] = value
    for name, value in handler.headers.items():
        lowered = name.lower()
        if lowered.startswith("x-") and not lowered.startswith("x-opencode-go-"):
            headers[lowered] = value
    # Never forward a caller-token Bearer or x-api-key to the native backend:
    # generic clients (zcode via Authorization Bearer, Claude Code via
    # x-api-key) authenticate remote proxy access with the caller token in
    # place of the custom X-OpenCode-Go-Proxy-Token header they cannot send.
    # That value is a proxy capability, not a provider credential, so a
    # native-model request carrying it must fail closed, not leak it. The
    # pops run after the x-* loop above so a stripped x-api-key cannot be
    # re-added by it.
    if caller_bearer_matches(handler.headers):
        headers.pop("authorization", None)
    if caller_xapi_key_matches(handler.headers):
        headers.pop("x-api-key", None)
    return headers


def _require_https(base_url: str) -> None:
    """Refuse to relay the client's Authorization header to a plain-http backend."""
    scheme = urllib.parse.urlsplit(base_url).scheme.lower()
    if scheme != "https" and os.environ.get(NATIVE_INSECURE_ENV) != "1":
        raise ProxyError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            f"native backend must use https (got {base_url!r}); "
            f"set {NATIVE_INSECURE_ENV}=1 only for local testing",
        )


def _relay_response_headers(handler: Any, headers: Any, *, streaming: bool) -> None:
    """Forward safe upstream response headers; owned and hop-by-hop ones are skipped."""
    skip = HOP_BY_HOP_HEADERS | {"content-type"}
    if streaming:
        skip |= {"content-length", "cache-control"}
    for name, value in headers.items():
        if name.lower() not in skip:
            handler.send_header(name, value)


def _relay_status(handler: Any, status: int, content_type: str, body: bytes, headers: Any = None) -> None:
    handler.send_response(status)
    if headers is not None:
        _relay_response_headers(handler, headers, streaming=False)
    handler.send_header("content-type", content_type)
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()

def _write_empty_stream_error(ka: KeepaliveWriter) -> bool:
    """Terminate a contentless stream with an explicit client-visible error.

    Replaces the former silent empty completion: a well-formed empty turn made
    agents log "no text, no tool calls, no usage" and stop mid-task as if the
    work were done. The ``response.error`` shape matches the established zen
    error terminal, so SSE-heads-already-committed clients surface a failure.
    """
    event: dict[str, Any] = {
        "type": "response.error",
        "error": {
            "code": "empty_stream",
            "message": "upstream stream ended without any output (empty after retries)",
        },
    }
    payload = (
        b"data: " + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n\n"
        b"data: [DONE]\n\n"
    )
    return ka.write(payload)


def _relay_stream(
    response: Any,
    handler: Any,
    request_id: str,
    transform_line: Callable[[bytes], bytes] | None = None,
    *,
    keepalive: KeepaliveWriter | None = None,
    require_content: tuple[bytes, ...] | None = None,
) -> str:
    """Relay upstream SSE lines with a keepalive comment thread.

    Returns the outcome: "done" when the upstream stream reached its terminal
    marker ([DONE] or response.completed), "aborted" when it ended without
    one (upstream error or truncation), "gone" when the client disconnected,
    and "exhausted" when ``require_content`` was set and the stream reached
    its terminal without ever carrying a content marker -- in that case the
    client receives an explicit ``response.error`` terminal instead of a
    silent empty completion.
    The 200 head is already committed by the caller in every case, so the
    meter must record the outcome rather than a naive 200. Pass ``keepalive``
    to share one writer across relay attempts; otherwise a private one is
    created and stopped here.
    """
    ka = keepalive if keepalive is not None else KeepaliveWriter(handler)
    if keepalive is None:
        ka.start()
    outcome = "aborted"
    saw_content = require_content is None
    try:
        with response as resp:
            for line in resp:
                if not ka.alive:
                    outcome = "gone"
                    break
                if transform_line is not None:
                    line = transform_line(line)
                if require_content is not None and not saw_content:
                    saw_content = any(marker in line for marker in require_content)
                if (
                    require_content is not None
                    and not saw_content
                    and is_stream_terminal(line)
                ):
                    if not _write_empty_stream_error(ka):
                        outcome = "gone"
                        trace(
                            "client.disconnected", level="info",
                            request_id=request_id,
                            message="client closed connection during stream",
                        )
                    else:
                        outcome = "exhausted"
                    break
                if not ka.write(line):
                    outcome = "gone"
                    trace(
                        "client.disconnected", level="info",
                        request_id=request_id,
                        message="client closed connection during stream",
                    )
                    break
                if is_stream_terminal(line):
                    outcome = "done"
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        outcome = "aborted"
        trace(
            "upstream.stream_aborted", level="warn",
            request_id=request_id,
            status=getattr(exc, "code", None) or getattr(exc, "reason", str(exc)),
        )
    finally:
        if keepalive is None:
            ka.stop()
    return outcome


def relay_native_request(handler: Any, payload: Json, config: ProxyConfig, request_id: str) -> None:
    """POST the payload to {base}/v1/responses and relay the response.

    The JSON payload is sent semantically unchanged (parsed and re-serialized;
    whitespace and key order are not preserved). Non-stream: upstream status,
    safe headers, and raw body. Stream: the SSE head is committed only after
    the backend answers 200, then every line is relayed unchanged. A non-200
    upstream answer is relayed with its own status and body before any SSE is
    committed. The backend base URL must be https unless the local-testing
    escape hatch is set.
    """
    started = time.time()
    model = payload.get("model") or "unknown"
    base_url = resolve_native_base_url()
    _require_https(base_url)
    url = f"{base_url}/v1/responses"
    raw_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=raw_payload,
        headers=_forwarded_headers(handler),
        method="POST",
    )
    trace(
        "native.start", level="debug",
        request_id=request_id,
        url=url,
        bytes=len(raw_payload),
        stream=payload.get("stream") is True,
    )
    try:
        response = urllib.request.urlopen(request, timeout=config.timeout_sec)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        content_type = (exc.headers.get("content-type") if exc.headers else None) or "application/json"
        _relay_status(handler, exc.code, content_type, body, exc.headers)
        record_usage_event(
            model=model, status=exc.code,
            duration_ms=int((time.time() - started) * 1000), provider=NATIVE_PROVIDER,
        )
        return
    except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError) as exc:
        # RemoteDisconnected (upstream closed before response headers) belongs in
        # the transient bucket: it is a ConnectionResetError/BadStatusLine hybrid,
        # not a URLError, and used to escape as an unhandled crash.
        status = HTTPStatus.GATEWAY_TIMEOUT if isinstance(exc, TimeoutError) else HTTPStatus.BAD_GATEWAY
        record_usage_event(
            model=model, status=int(status),
            duration_ms=int((time.time() - started) * 1000), provider=NATIVE_PROVIDER,
        )
        if isinstance(exc, TimeoutError):
            raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "native backend timeout") from exc
        raise ProxyError(HTTPStatus.BAD_GATEWAY, f"native backend network error: {getattr(exc, 'reason', exc)}") from exc

    elapsed_ms = int((time.time() - started) * 1000)
    if payload.get("stream") is True:
        handler.send_response(HTTPStatus.OK)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("cache-control", "no-cache")
        _relay_response_headers(handler, response.headers, streaming=True)
        handler.end_headers()
        outcome = _relay_stream(response, handler, request_id)
        if outcome == "aborted":
            trace("native.done", level="debug", request_id=request_id, status=502, outcome=outcome, elapsed_ms=elapsed_ms)
            record_usage_event(
                model=model, status=502, duration_ms=elapsed_ms,
                stream_aborted=True, provider=NATIVE_PROVIDER,
            )
            return
        if outcome == "gone":
            trace("native.done", level="debug", request_id=request_id, status=0, outcome=outcome, elapsed_ms=elapsed_ms)
            record_usage_event(
                model=model, status=0, duration_ms=elapsed_ms,
                stream_aborted=True, provider=NATIVE_PROVIDER,
            )
            return
        trace("native.done", level="debug", request_id=request_id, status=200, outcome=outcome, elapsed_ms=elapsed_ms)
        record_usage_event(
            model=model, status=200, duration_ms=elapsed_ms, provider=NATIVE_PROVIDER,
        )
        return

    body = response.read()
    content_type = response.headers.get("content-type") or "application/json"
    _relay_status(handler, response.status, content_type, body, response.headers)
    trace("native.done", level="debug", request_id=request_id, status=response.status, elapsed_ms=elapsed_ms)
    record_usage_event(
        model=model, status=response.status,
        duration_ms=elapsed_ms, provider=NATIVE_PROVIDER,
    )
