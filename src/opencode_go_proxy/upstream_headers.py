"""Shared identity headers for provider requests."""

from __future__ import annotations

import os

from . import __version__


def upstream_user_agent() -> str:
    """Return the adapter identity, with an explicit operator override."""
    return os.environ.get(
        "OPENCODE_GO_PROXY_USER_AGENT",
        f"opencode-go-proxy/{__version__}",
    )


# Every upstream request sends this accept value: the gateways decide SSE vs
# JSON from the body "stream" flag, not content negotiation, and a narrow
# "text/event-stream" / "application/json" accept has coincided with 200s that
# carried no usable payload downstream. One constant so no call site reintroduces
# a per-stream accept on exactly one path.
UPSTREAM_ACCEPT = "*/*"


def base_upstream_headers() -> dict[str, str]:
    """The content-type/accept/user-agent triple every upstream request sends.

    Assembled here and nowhere else so a new call site inherits the widened
    ``accept`` instead of re-deriving a narrower one by hand.
    """
    return {
        "content-type": "application/json",
        "accept": UPSTREAM_ACCEPT,
        "user-agent": upstream_user_agent(),
    }
