"""Request guards for the proxy's local credential boundary."""

from __future__ import annotations

import hmac
import ipaddress
import os
from http import HTTPStatus
from typing import Any

from .errors import ProxyError

BROWSER_HEADERS = ("origin", "referer", "sec-fetch-site")
REMOTE_ENV = "OPENCODE_GO_PROXY_ALLOW_REMOTE"
CALLER_TOKEN_ENV = "OPENCODE_GO_PROXY_CALLER_TOKEN"
CALLER_TOKEN_HEADER = "X-OpenCode-Go-Proxy-Token"
MIN_CALLER_TOKEN_LENGTH = 32


def _host_name(host: str | None) -> str | None:
    """Normalize a Host header to its bare host, dropping the port."""
    if host is None:
        return None
    host = host.strip()
    if not host:
        return None
    if host.startswith("["):  # [::1]:port or bare [::1]
        end = host.find("]")
        return host if end == -1 else host[1:end]
    if host.count(":") == 1:  # host:port; unbracketed IPv6 keeps multiple colons
        return host.split(":", 1)[0]
    return host


def _is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def check_host(host: str | None) -> None:
    """400 for a missing Host header, 403 for a non-loopback Host."""
    name = _host_name(host)
    if name is None:
        raise ProxyError(HTTPStatus.BAD_REQUEST, "missing Host header", error_type="invalid_host")
    if not _is_loopback(name) and os.environ.get(REMOTE_ENV) != "1":
        raise ProxyError(HTTPStatus.FORBIDDEN, "request host is not allowed", error_type="invalid_host")


def caller_token_matches(supplied: str, expected: str) -> bool:
    """Timing-safe caller-token equality, safe for non-ASCII values.

    ``hmac.compare_digest`` rejects non-ASCII str (TypeError), which would
    turn a valid non-ASCII token into a 500 in :func:`check_client` and --
    worse -- into a silently skipped caller-token strip (fail-open leak
    upstream) in the native relay. Comparing UTF-8 bytes avoids both.
    """
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _bearer_token(headers: Any) -> str:
    """Extract a Bearer token from Authorization, else empty string.

    Generic OpenAI-compatible clients (e.g. zcode, whose custom-provider
    JSON only exposes an apiKey field and cannot send custom headers)
    carry credentials only as Authorization Bearer. Accepting the caller
    token there as a fallback lets those clients authenticate remote
    access without a custom header slot. Lookup uses the lowercase key,
    matching FORWARD_HEADERS convention; HTTPMessage .get is
    case-insensitive, and plain-dict test doubles use lowercase too.
    """
    auth = (headers.get("authorization") or "").strip()
    if auth[:7].lower() != "bearer ":
        return ""
    return auth[7:].strip()


def caller_bearer_matches(headers: Any) -> bool:
    """True when the Authorization Bearer equals the configured caller token.

    Single source of the "is this Bearer the caller capability?" decision:
    :func:`check_client` accepts it and the native relay strips it before
    forwarding, and both must agree on what counts as the caller token or
    the capability leaks upstream.
    """
    expected = os.environ.get(CALLER_TOKEN_ENV, "")
    if len(expected) < MIN_CALLER_TOKEN_LENGTH:
        return False
    return caller_token_matches(_bearer_token(headers), expected)


def check_client(client_host: str, headers) -> None:
    """Require a separate caller capability for non-loopback clients.

    Primary credential is X-OpenCode-Go-Proxy-Token. As a fallback for
    clients that cannot send custom headers, Authorization Bearer with the
    caller token is accepted too. Upstream auth never reuses this value.
    """
    if _is_loopback(client_host):
        return
    if os.environ.get(REMOTE_ENV) != "1":
        raise ProxyError(
            HTTPStatus.FORBIDDEN,
            "remote clients are not allowed",
            error_type="invalid_client",
        )
    expected = os.environ.get(CALLER_TOKEN_ENV, "")
    supplied = headers.get(CALLER_TOKEN_HEADER) or ""
    if len(expected) < MIN_CALLER_TOKEN_LENGTH:
        raise ProxyError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "remote access is not securely configured",
            error_type="remote_auth_not_configured",
        )
    if caller_token_matches(supplied, expected) or caller_bearer_matches(headers):
        return
    raise ProxyError(
        HTTPStatus.UNAUTHORIZED,
        "valid remote caller token required",
        error_type="invalid_caller_token",
    )


def validate_bind_security(bind: str) -> None:
    """Refuse a non-loopback listener unless remote capability auth is ready."""
    if _is_loopback(bind):
        return
    if os.environ.get(REMOTE_ENV) != "1":
        raise ValueError(f"non-loopback bind requires {REMOTE_ENV}=1")
    if len(os.environ.get(CALLER_TOKEN_ENV, "")) < MIN_CALLER_TOKEN_LENGTH:
        raise ValueError(
            f"non-loopback bind requires {CALLER_TOKEN_ENV} with at least "
            f"{MIN_CALLER_TOKEN_LENGTH} characters"
        )


def check_browser_origin(headers) -> None:
    """403 when any browser marker (Origin / Referer / Sec-Fetch-Site) is present."""
    if any(headers.get(h) for h in BROWSER_HEADERS):
        raise ProxyError(
            HTTPStatus.FORBIDDEN,
            "Browser-originated requests are not accepted by the local proxy.",
            error_type="browser_request_rejected",
        )


def check_content_type(content_type: str | None) -> None:
    """415 unless the media type is application/json (params like charset allowed)."""
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise ProxyError(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            "Proxy requests require Content-Type: application/json.",
            error_type="unsupported_media_type",
        )
