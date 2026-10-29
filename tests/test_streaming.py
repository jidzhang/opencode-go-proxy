"""Unit tests for the SSE transport primitives that remain live.

The deep streaming engine (handle_streaming_request and friends) was deleted
with the dead go->zen respawn path; _client_gone and KeepaliveWriter still
carry every live stream, so they keep their coverage.
"""

import io
import socket
import struct
import time

from opencode_go_proxy.streaming import _client_gone


class TestClientGone:
    """_client_gone peeks the client socket; a closed peer must read as gone.

    On macOS a write to a reset peer does not error, so this peek is the only
    way a cancelling client becomes visible before the stream ends.
    """

    @staticmethod
    def _assert_eventually_gone(sock: socket.socket) -> None:
        wfile = sock.makefile("wb", 0)  # same shape as handler.wfile
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if _client_gone(wfile):
                return
            time.sleep(0.005)
        raise AssertionError("closed peer was never detected as gone")

    def test_alive_peer_is_not_gone(self) -> None:
        server, client = socket.socketpair()
        try:
            assert _client_gone(server.makefile("wb", 0)) is False
        finally:
            server.close()
            client.close()

    def test_peer_fin_reads_as_gone(self) -> None:
        server, client = socket.socketpair()
        try:
            client.close()
            self._assert_eventually_gone(server)
        finally:
            server.close()

    def test_peer_rst_reads_as_gone(self) -> None:
        server, client = socket.socketpair()
        try:
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            client.close()
            self._assert_eventually_gone(server)
        finally:
            server.close()

    def test_socketless_wfile_reads_as_alive(self) -> None:
        # Test doubles (io.BytesIO) have no peekable socket; the probe must
        # degrade to the old write-only behavior, never crash the stream.
        assert _client_gone(io.BytesIO()) is False
