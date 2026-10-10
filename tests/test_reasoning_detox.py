"""Reasoning-detox retry: an expired-reference 400 retries once without blobs."""

from __future__ import annotations

import io
import json
import urllib.error
from http import HTTPStatus
from unittest import mock

from opencode_go_proxy import zen_upstream as zu
from opencode_go_proxy.errors import ProxyError
from opencode_go_proxy.protocol import (
    drop_reasoning_items,
    expired_reasoning_reference,
    maybe_detox_payload,
    reasoning_detox_enabled,
)
from opencode_go_proxy.streaming import _ConnectFailed


EXPIRED = ("Referenced reasoning item 'rs_033d0a4cae9d4a8a8ccad0e3f45ed970' "
           "was not found or has expired.")
GENERIC = "The request contains invalid parameters. Check the request body."


class TestDetector:
    def test_real_message_str(self) -> None:
        assert expired_reasoning_reference(400, EXPIRED) == "rs_033d0a4cae9d4a8a8ccad0e3f45ed970"

    def test_real_message_bytes(self) -> None:
        body = json.dumps({"error": {"message": EXPIRED}}).encode()
        assert expired_reasoning_reference(400, body) == "rs_033d0a4cae9d4a8a8ccad0e3f45ed970"

    def test_generic_400_not_matched(self) -> None:
        assert expired_reasoning_reference(400, GENERIC) is None

    def test_non_400_not_matched(self) -> None:
        assert expired_reasoning_reference(429, EXPIRED) is None
        assert expired_reasoning_reference(500, EXPIRED) is None

    def test_empty_body_not_matched(self) -> None:
        assert expired_reasoning_reference(400, None) is None
        assert expired_reasoning_reference(400, "") is None

    def test_status_enum_accepted(self) -> None:
        assert expired_reasoning_reference(HTTPStatus.BAD_REQUEST, EXPIRED) is not None


class TestDrop:
    def test_drops_only_reasoning(self) -> None:
        payload = {"model": "m", "input": [
            {"type": "reasoning", "id": "rs_1"},
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "function_call", "call_id": "c", "name": "t", "arguments": "{}"},
        ]}
        cleaned, n = drop_reasoning_items(payload)
        assert n == 1
        assert [i.get("type") for i in cleaned["input"]] == ["message", "function_call"]
        assert len(payload["input"]) == 3  # input untouched

    def test_no_reasoning_no_copy(self) -> None:
        payload = {"model": "m", "input": [{"type": "message"}]}
        cleaned, n = drop_reasoning_items(payload)
        assert cleaned is payload and n == 0

    def test_non_list_passthrough(self) -> None:
        for bad in ({"model": "m"}, {"model": "m", "input": "hi"}, "x", None):
            assert drop_reasoning_items(bad) == (bad, 0)


def _payload():
    return {"model": "opencode-go/muse-spark-1.3-contributor", "stream": False, "input": [
        {"type": "reasoning", "id": "rs_old", "encrypted_content": "blob"},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    ]}


class FakeHandler:
    def __init__(self) -> None:
        self.status = None
        self.body = b""

    def send_response(self, status) -> None:
        self.status = int(status)

    def send_header(self, *args) -> None:
        pass

    def end_headers(self) -> None:
        pass

    @property
    def wfile(self):
        return self

    def write(self, data: bytes) -> None:
        self.body += data

    def flush(self) -> None:
        pass


def _expired_exc():
    return ProxyError(HTTPStatus.BAD_REQUEST, "bad", upstream_status=400, body=EXPIRED)


class TestMaybeDetox:
    def _payload(self):
        return {"model": "m", "input": [
            {"type": "reasoning", "id": "rs_1"},
            {"type": "message", "role": "user", "content": "hi"},
        ]}

    def test_match_returns_tuple(self) -> None:
        out = maybe_detox_payload(self._payload(), 400, EXPIRED)
        assert out is not None
        detoxed, ref, dropped = out
        assert (ref, dropped) == ("rs_033d0a4cae9d4a8a8ccad0e3f45ed970", 1)
        assert [i.get("type") for i in detoxed["input"]] == ["message"]

    def test_no_match_returns_none(self) -> None:
        assert maybe_detox_payload(self._payload(), 400, GENERIC) is None
        assert maybe_detox_payload(self._payload(), 500, EXPIRED) is None

    def test_nothing_to_strip_returns_none(self) -> None:
        payload = {"model": "m", "input": [{"type": "message"}]}
        assert maybe_detox_payload(payload, 400, EXPIRED) is None

    def test_disabled_returns_none(self) -> None:
        import os
        from unittest import mock as _mock

        with _mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_REASONING_DETOX": "0"}):
            assert maybe_detox_payload(self._payload(), 400, EXPIRED) is None


class TestNonStreamDetox:
    def _call(self, **kw):
        base = dict(model="opencode-go/muse-spark-1.3-contributor", bare_id="muse-spark-1.3-contributor",
                    family="openai_responses", api_key="k", base_url="https://x", provider="opencode-go")
        base.update(kw)
        return base

    def test_expired_400_retries_without_reasoning(self) -> None:
        calls = []

        def fake_call(payload, *args, **kwargs):
            calls.append(payload)
            if len(calls) == 1:
                raise _expired_exc()
            return {"object": "response", "output": []}

        handler = FakeHandler()
        with mock.patch.object(zu, "call_family_responses", side_effect=fake_call):
            zu.handle_family_responses_request(handler, _payload(), object(), "req", **self._call())
        assert handler.status == 200
        assert len(calls) == 2
        assert all(i.get("type") != "reasoning" for i in calls[1]["input"])
        assert any(i.get("type") == "reasoning" for i in calls[0]["input"])

    def test_generic_400_not_retried(self) -> None:
        calls = []

        def fake_call(payload, *args, **kwargs):
            calls.append(payload)
            raise ProxyError(HTTPStatus.BAD_REQUEST, "bad", upstream_status=400, body=GENERIC)

        handler = FakeHandler()
        try:
            with mock.patch.object(zu, "call_family_responses", side_effect=fake_call):
                zu.handle_family_responses_request(handler, _payload(), object(), "req", **self._call())
        except ProxyError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected ProxyError")
        assert len(calls) == 1

    def test_second_failure_propagates(self) -> None:
        def fake_call(payload, *args, **kwargs):
            raise _expired_exc()

        handler = FakeHandler()
        try:
            with mock.patch.object(zu, "call_family_responses", side_effect=fake_call):
                zu.handle_family_responses_request(handler, _payload(), object(), "req", **self._call())
        except ProxyError as exc:
            assert exc.body == EXPIRED
        else:  # pragma: no cover
            raise AssertionError("expected ProxyError")


class TestStreamDetox:
    def _http_error(self, body: bytes):
        return urllib.error.HTTPError(
            "https://x/responses", 400, "Bad Request", {"content-type": "application/json"},
            io.BytesIO(body),
        )

    def test_expired_400_retries_stream(self) -> None:
        opened = []

        def fake_open(req, config, request_id, max_retries):
            opened.append(req)
            if len(opened) == 1:
                raise _ConnectFailed(self._http_error(
                    json.dumps({"error": {"message": EXPIRED}}).encode()), 0,
                    json.dumps({"error": {"message": EXPIRED}}).encode())
            return mock.sentinel.response, 0

        relayed = {}

        def fake_relay(response, handler, request_id, transform=None):
            relayed["transform"] = transform
            return "done"

        handler = FakeHandler()
        payload = _payload()
        payload["stream"] = True
        with mock.patch.object(zu, "_open_upstream_stream", side_effect=fake_open), \
             mock.patch.object(zu, "_relay_stream", side_effect=fake_relay), \
             mock.patch.object(zu, "_meter_provider"), \
             mock.patch.object(zu, "record_quota_from_headers", create=True):
            zu.handle_family_responses_request(
                handler, payload, object(), "req",
                model="opencode-go/muse-spark-1.3-contributor", bare_id="muse-spark-1.3-contributor",
                family="openai_responses", api_key="k", base_url="https://x",
                provider="opencode-go", restore_namespaces=True,
            )
        assert len(opened) == 2
        assert relayed  # detoxed stream relayed to client
        assert handler.status == 200
