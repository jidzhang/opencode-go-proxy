"""Anthropic Messages client surface: Claude Code -> every certified Go family.

Covers the request translation (Anthropic -> Responses payload), the response
translation (Responses object/SSE -> Anthropic Message/events), the
/v1/messages endpoint dispatch over both translated families and the verbatim
Messages family, the Anthropic error envelope, and count_tokens.
"""

import json
import os
import socket
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest import mock

import pytest

from opencode_go_proxy.anthropic_client import (
    ResponsesToAnthropicStream,
    anthropic_error_payload,
    anthropic_to_responses_payload,
    count_messages_tokens,
    responses_to_anthropic_message,
)
from opencode_go_proxy.app import ProxyConfig, ResponsesProxyHandler
from opencode_go_proxy.errors import ProxyError
from opencode_go_proxy.protocol import responses_payload_to_chat_payload


def make_config(port: int) -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=port,
        chat_base_url="https://mock-upstream.test/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=10,
        max_body_bytes=20 * 1024 * 1024,
    )


class MockUpstreamResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status
        self.headers = {}
        self._lines = body.splitlines(keepends=True)

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = make_config(port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), ResponsesProxyHandler)
    httpd.config = config  # type: ignore[attr-defined]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    yield port, httpd

    httpd.shutdown()
    httpd.server_close()


def post(port: int, path: str, body: bytes) -> tuple:
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", path, body, {"content-type": "application/json"})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp, raw


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------


class TestRequestTranslation:
    def test_system_string_becomes_instructions(self):
        out, _stats = anthropic_to_responses_payload({
            "model": "opencode-go/glm-5.2",
            "system": "be terse",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert out["instructions"] == "be terse"
        assert out["max_output_tokens"] == 64
        assert out["input"] == [{"role": "user", "content": "hi"}]
        assert out["stream"] is False

    def test_system_blocks_flatten_and_cache_control_dropped(self):
        out, _ = anthropic_to_responses_payload({
            "model": "m",
            "system": [
                {"type": "text", "text": "part one", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "part two"},
            ],
            "messages": [],
        })
        assert out["instructions"] == "part one\n\npart two"

    def test_tool_use_and_result_roundtrip_with_ids(self):
        out, stats = anthropic_to_responses_payload({
            "model": "m",
            "messages": [
                {"role": "user", "content": "read it"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "using the tool"},
                        {
                            "type": "tool_use",
                            "id": "toolu_abc",
                            "name": "read_file",
                            "input": {"path": "a.py"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_abc",
                            "content": "file body",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
            ],
        })
        items = out["input"]
        assert items[0] == {"role": "user", "content": "read it"}
        assert items[1] == {
            "type": "function_call",
            "call_id": "toolu_abc",
            "name": "read_file",
            "arguments": '{"path":"a.py"}',
        }
        assert items[2] == {"role": "assistant", "content": "using the tool"}
        assert items[3] == {
            "type": "function_call_output",
            "call_id": "toolu_abc",
            "output": "file body",
        }
        assert stats["tool_uses"] == 1
        assert stats["tool_results"] == 1

    def test_orphan_tool_result_is_dropped(self):
        out, stats = anthropic_to_responses_payload({
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_none", "content": "x"}],
                }
            ],
        })
        assert out["input"] == []
        assert stats["tool_results"] == 0

    def test_thinking_blocks_are_dropped(self):
        out, stats = anthropic_to_responses_payload({
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                        {"type": "text", "text": "answer"},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "redacted_thinking", "data": "blob"},
                        {"type": "text", "text": "go on"},
                    ],
                },
            ],
        })
        assert out["input"][0] == {"role": "assistant", "content": "answer"}
        assert out["input"][1] == {"role": "user", "content": [{"type": "input_text", "text": "go on"}]}
        assert stats["thinking_blocks_dropped"] == 2

    def test_image_blocks_become_input_image(self):
        out, stats = anthropic_to_responses_payload({
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "aGk=",
                            },
                        },
                        {
                            "type": "image",
                            "source": {"type": "url", "url": "https://example.com/x.png"},
                        },
                        {
                            "type": "image",
                            "source": {"type": "url", "url": "http://example.com/y.png"},
                        },
                    ],
                }
            ],
        })
        parts = out["input"][0]["content"]
        assert parts[0] == {"type": "input_text", "text": "look"}
        assert parts[1] == {"type": "input_image", "image_url": "data:image/png;base64,aGk="}
        assert parts[2] == {"type": "input_image", "image_url": "https://example.com/x.png"}
        assert len(parts) == 3
        assert stats["images"] == 2

    def test_document_flattened_to_placeholder(self):
        out, stats = anthropic_to_responses_payload({
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "x"}},
                        {"type": "text", "text": "summarize"},
                    ],
                }
            ],
        })
        parts = out["input"][0]["content"]
        assert parts[0]["type"] == "input_text"
        assert "document omitted" in parts[0]["text"]
        assert parts[1] == {"type": "input_text", "text": "summarize"}
        assert stats["documents_flattened"] == 1

    def test_tools_and_tool_choice_map_to_responses_shapes(self):
        out, _ = anthropic_to_responses_payload({
            "model": "m",
            "tool_choice": {"type": "tool", "name": "read_file"},
            "tools": [
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": "go"}],
        })
        assert out["tools"] == [
            {
                "type": "function",
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ]
        assert out["tool_choice"] == {"type": "function", "name": "read_file"}
        # The translated payload must survive the existing Responses->chat
        # translation, which is what a chat-family upstream will run it through.
        chat, _model, _stats = responses_payload_to_chat_payload(out)
        assert chat["tools"][0]["type"] == "function"
        assert chat["tool_choice"] == {"type": "function", "function": {"name": "read_file"}}

    def test_tool_choice_variants(self):
        from opencode_go_proxy.anthropic_client import _translate_tool_choice

        assert _translate_tool_choice({"type": "auto"}) == "auto"
        assert _translate_tool_choice({"type": "any"}) == "required"
        assert _translate_tool_choice({"type": "none"}) == "none"
        assert _translate_tool_choice({"type": "tool", "name": "t"}) == {"type": "function", "name": "t"}
        assert _translate_tool_choice({"type": "tool"}) is None
        assert _translate_tool_choice("bogus") is None

    def test_stop_sequences_and_metadata_are_not_forwarded(self):
        out, _ = anthropic_to_responses_payload({
            "model": "m",
            "stop_sequences": ["END"],
            "metadata": {"user_id": "u"},
            "temperature": 0.5,
            "top_p": 0.9,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert "stop_sequences" not in out
        assert "metadata" not in out
        assert out["temperature"] == 0.5
        assert out["top_p"] == 0.9


# ---------------------------------------------------------------------------
# Non-stream response translation
# ---------------------------------------------------------------------------


class TestResponseTranslation:
    def test_text_and_tool_use_with_usage(self):
        message = responses_to_anthropic_message(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "thinking"}],
                    },
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "hello"}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_xyz",
                        "name": "read_file",
                        "arguments": '{"path": "a.py"}',
                    },
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "input_tokens_details": {"cached_tokens": 6},
                },
            },
            "opencode-go/glm-5.2",
        )
        assert message["type"] == "message"
        assert message["role"] == "assistant"
        assert message["model"] == "opencode-go/glm-5.2"
        assert message["id"].startswith("msg_")
        assert message["content"][0] == {"type": "thinking", "thinking": "thinking"}
        assert message["content"][1] == {"type": "text", "text": "hello"}
        assert message["content"][2] == {
            "type": "tool_use",
            "id": "call_xyz",
            "name": "read_file",
            "input": {"path": "a.py"},
        }
        assert message["stop_reason"] == "tool_use"
        assert message["usage"] == {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_read_input_tokens": 6,
        }

    def test_empty_output_gets_empty_text_block_and_end_turn(self):
        message = responses_to_anthropic_message(
            {"status": "completed", "output": [], "usage": None},
            "m",
        )
        assert message["content"] == [{"type": "text", "text": ""}]
        assert message["stop_reason"] == "end_turn"

    def test_incomplete_maps_to_max_tokens(self):
        message = responses_to_anthropic_message(
            {"status": "incomplete", "output": [], "usage": None},
            "m",
        )
        assert message["stop_reason"] == "max_tokens"


# ---------------------------------------------------------------------------
# Stream translation
# ---------------------------------------------------------------------------


class FakeWrite:
    def __init__(self):
        self.chunks: list[bytes] = []
        self.fail = False

    def __call__(self, data: bytes) -> bool:
        if self.fail:
            return False
        self.chunks.append(data)
        return True

    def events(self) -> list[dict]:
        out = []
        for chunk in self.chunks:
            for line in chunk.split(b"\n"):
                if line.startswith(b"data: "):
                    out.append(json.loads(line[6:]))
        return out

    def names(self) -> list[str]:
        names = []
        for chunk in self.chunks:
            for line in chunk.split(b"\n"):
                if line.startswith(b"event: "):
                    names.append(line[7:].decode())
        return names


def engine_like_events() -> list[dict]:
    """The Responses event sequence _ZenStreamEngine emits for a text turn."""
    return [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "reasoning", "id": "rs_1", "summary": []},
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs_1",
            "delta": "hmm",
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "hmm"}]},
        },
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {"type": "message", "id": "msg_1", "role": "assistant", "content": []},
        },
        {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "he"},
        {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "y"},
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": {"type": "message", "id": "msg_1", "content": [{"type": "output_text", "text": "hey"}]},
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 7, "output_tokens": 2},
            },
        },
    ]


class TestStreamTranslation:
    def test_text_turn_event_sequence(self):
        write = FakeWrite()
        translator = ResponsesToAnthropicStream(write, "opencode-go/glm-5.2")
        for event in engine_like_events():
            assert translator.consume(event) is True

        assert write.names() == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]
        events = write.events()
        start = events[0]
        assert start["type"] == "message_start"
        assert start["message"]["role"] == "assistant"
        assert start["message"]["model"] == "opencode-go/glm-5.2"
        assert start["message"]["usage"] == {"input_tokens": 0, "output_tokens": 0}
        assert events[1] == {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }
        assert events[2]["delta"] == {"type": "thinking_delta", "thinking": "hmm"}
        assert events[4] == {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": ""},
        }
        assert events[5]["delta"] == {"type": "text_delta", "text": "he"}
        assert events[6]["delta"] == {"type": "text_delta", "text": "y"}
        assert events[7] == {"type": "content_block_stop", "index": 1}
        delta = events[8]
        assert delta["type"] == "message_delta"
        assert delta["delta"] == {"stop_reason": "end_turn", "stop_sequence": None}
        assert delta["usage"] == {"input_tokens": 7, "output_tokens": 2}
        assert events[9] == {"type": "message_stop"}

    def test_function_call_arguments_arrive_at_done(self):
        write = FakeWrite()
        translator = ResponsesToAnthropicStream(write, "m")
        for event in [
            {"type": "response.created", "response": {"id": "r"}},
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "t", "arguments": ""},
            },
            {
                "type": "response.output_item.done",
                "item": {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "t", "arguments": '{"x": 1}'},
            },
            {
                "type": "response.completed",
                "response": {"status": "completed", "output": [{"type": "function_call"}], "usage": None},
            },
        ]:
            assert translator.consume(event) is True

        events = write.events()
        start = events[1]
        assert start["content_block"] == {"type": "tool_use", "id": "call_9", "name": "t", "input": {}}
        assert events[2]["delta"] == {"type": "input_json_delta", "partial_json": '{"x": 1}'}
        assert events[3]["type"] == "content_block_stop"
        assert events[4]["delta"]["stop_reason"] == "tool_use"

    def test_verbatim_arguments_delta_stream(self):
        write = FakeWrite()
        translator = ResponsesToAnthropicStream(write, "m")
        for event in [
            {"type": "response.created", "response": {"id": "r"}},
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "t", "arguments": ""},
            },
            {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"x":'},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": " 1}'"},
            {
                "type": "response.output_item.done",
                "item": {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "t", "arguments": '{"x": 1}\''},
            },
            {
                "type": "response.completed",
                "response": {"status": "completed", "output": [], "usage": None},
            },
        ]:
            assert translator.consume(event) is True

        deltas = [
            e["delta"]["partial_json"]
            for e in write.events()
            if e["type"] == "content_block_delta"
        ]
        assert deltas == ["{" + '"x":', " 1}'"]

    def test_incomplete_maps_to_max_tokens(self):
        write = FakeWrite()
        translator = ResponsesToAnthropicStream(write, "m")
        translator.consume({"type": "response.created", "response": {"id": "r"}})
        translator.consume(
            {
                "type": "response.completed",
                "response": {"status": "incomplete", "output": [], "usage": {"input_tokens": 5, "output_tokens": 9}},
            }
        )
        delta = next(e for e in write.events() if e["type"] == "message_delta")
        assert delta["delta"]["stop_reason"] == "max_tokens"
        assert delta["usage"] == {"input_tokens": 5, "output_tokens": 9}

    def test_error_event_emits_anthropic_error_and_no_message_stop(self):
        write = FakeWrite()
        translator = ResponsesToAnthropicStream(write, "m")
        translator.consume({"type": "response.created", "response": {"id": "r"}})
        translator.consume({"type": "response.error", "error": {"message": "boom", "code": "empty_completion"}})
        assert write.names()[-1] == "error"
        assert "message_stop" not in write.names()
        error = next(e for e in write.events() if e["type"] == "error")
        assert error["error"] == {"type": "empty_completion", "message": "boom"}

    def test_client_gone_stops_further_events(self):
        write = FakeWrite()
        translator = ResponsesToAnthropicStream(write, "m")
        translator.consume({"type": "response.created", "response": {"id": "r"}})
        write.fail = True
        assert translator.consume(
            {"type": "response.output_text.delta", "item_id": "x", "delta": "y"}
        ) is False
        assert translator.alive is False

    def test_finish_without_blocks_synthesizes_empty_text_block(self):
        # Upstream ended without response.completed and no content: finish()
        # must still produce a valid single-block Anthropic message.
        write = FakeWrite()
        translator = ResponsesToAnthropicStream(write, "m")
        assert translator.finish() is True
        names = write.names()
        assert names == [
            "message_start",
            "content_block_start",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]
        events = write.events()
        assert events[1]["content_block"] == {"type": "text", "text": ""}
        assert events[3]["delta"]["stop_reason"] == "end_turn"

    def test_non_text_delta_on_unopened_block_is_dropped(self):
        # A thinking or input_json delta whose added event never arrived must
        # not be attached to a lazily-opened text block.
        write = FakeWrite()
        translator = ResponsesToAnthropicStream(write, "m")
        translator.consume({"type": "response.created", "response": {"id": "r"}})
        assert translator.consume(
            {"type": "response.reasoning_summary_text.delta", "item_id": "rs_x", "delta": "hmm"}
        ) is True
        # Only message_start was emitted; no content_block frames.
        assert write.names() == ["message_start"]


# ---------------------------------------------------------------------------
# Endpoint behavior
# ---------------------------------------------------------------------------


def chat_completion_body() -> bytes:
    return json.dumps({
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "glm-5.2",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello there"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }).encode("utf-8")


class TestMessagesEndpointTranslation:
    def test_chat_model_non_stream_returns_anthropic_message(self, server):
        port, _ = server
        body = json.dumps({
            "model": "opencode-go/glm-5.2",
            "max_tokens": 128,
            "system": "be brief",
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(chat_completion_body())):
            resp, raw = post(port, "/v1/messages", body)

        assert resp.status == 200
        message = json.loads(raw)
        assert message["type"] == "message"
        assert message["role"] == "assistant"
        assert message["model"] == "opencode-go/glm-5.2"
        assert message["content"] == [{"type": "text", "text": "hello there"}]
        assert message["stop_reason"] == "end_turn"
        assert message["usage"]["input_tokens"] == 5

    def test_chat_model_stream_returns_anthropic_frames(self, server):
        port, _ = server
        body = json.dumps({
            "model": "opencode-go/glm-5.2",
            "max_tokens": 128,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        sse = b"".join(
            [
                b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
                b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n',
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":1,"total_tokens":6}}\n\n',
                b"data: [DONE]\n\n",
            ]
        )

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(sse)):
            resp, raw = post(port, "/v1/messages", body)

        assert resp.status == 200
        assert "text/event-stream" in resp.getheader("content-type", "")
        names = [
            line[7:].decode()
            for line in raw.split(b"\n")
            if line.startswith(b"event: ")
        ]
        assert names == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]
        events = [
            json.loads(line[6:])
            for line in raw.split(b"\n")
            if line.startswith(b"data: ")
        ]
        delta = next(e for e in events if e["type"] == "message_delta")
        assert delta["delta"]["stop_reason"] == "end_turn"
        assert delta["usage"]["output_tokens"] == 1

    def test_unknown_model_returns_anthropic_error_envelope(self, server):
        port, _ = server
        resp, raw = post(port, "/v1/messages", b'{"model":"claude-sonnet-4","messages":[]}')
        assert resp.status == 400
        envelope = json.loads(raw)
        assert envelope["type"] == "error"
        assert envelope["error"]["type"] == "not_found_error"

    def test_missing_key_returns_anthropic_auth_error(self, server):
        from opencode_go_proxy.secrets import clear_api_key_cache

        port, _ = server
        body = json.dumps({
            "model": "opencode-go/glm-5.2",
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        # resolve_api_key caches process-wide; earlier tests seeded it.
        clear_api_key_cache()
        env = {k: v for k, v in os.environ.items() if k not in {"OPENCODE_GO_API_KEY", "OPENCODE_API_KEY"}}
        try:
            with mock.patch.dict(os.environ, env, clear=True):
                resp, raw = post(port, "/v1/messages", body)
        finally:
            clear_api_key_cache()
        assert resp.status == 401
        envelope = json.loads(raw)
        assert envelope["error"]["type"] == "authentication_error"

    def test_count_tokens_returns_estimate(self, server):
        port, _ = server
        body = json.dumps({
            "model": "opencode-go/glm-5.2",
            "messages": [{"role": "user", "content": "count me"}],
        }).encode()
        resp, raw = post(port, "/v1/messages/count_tokens", body)
        assert resp.status == 200
        payload = json.loads(raw)
        assert payload["input_tokens"] >= 1

    def test_count_tokens_error_is_anthropic_shaped(self, server):
        port, _ = server
        resp, raw = post(port, "/v1/messages/count_tokens", b"not-json")
        assert resp.status == 400
        assert json.loads(raw)["type"] == "error"


# ---------------------------------------------------------------------------
# Error envelope + token estimate units
# ---------------------------------------------------------------------------


class TestErrorEnvelope:
    def test_error_type_mapping(self):
        assert anthropic_error_payload(ProxyError(400, "bad", error_type="model_not_found")) == {
            "type": "error",
            "error": {"type": "not_found_error", "message": "bad"},
        }
        assert anthropic_error_payload(ProxyError(429, "slow"))["error"]["type"] == "rate_limit_error"
        assert anthropic_error_payload(ProxyError(503, "down"))["error"]["type"] == "overloaded_error"
        assert anthropic_error_payload(ProxyError(500, "x"))["error"]["type"] == "api_error"


class TestCountTokensEstimate:
    def test_estimate_grows_with_content_and_never_zero(self):
        small = count_messages_tokens({"messages": [{"role": "user", "content": "hi"}]})
        big = count_messages_tokens({
            "messages": [{"role": "user", "content": "word " * 5000}],
        })
        assert small >= 1
        assert big > small


# ---------------------------------------------------------------------------
# Responses-family translated stream: transparent empty retry
# ---------------------------------------------------------------------------


def _responses_sse(*events: dict) -> bytes:
    body = b"".join(
        b"data: " + json.dumps(e, separators=(",", ":")).encode() + b"\n\n" for e in events
    )
    return body + b"data: [DONE]\n\n"


def _responses_content_stream(text: str = "hello") -> bytes:
    return _responses_sse(
        {"type": "response.created", "response": {"id": "r1"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "message", "id": "msg_1", "role": "assistant", "content": []},
        },
        {"type": "response.output_text.delta", "item_id": "msg_1", "delta": text},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": "msg_1",
                "content": [{"type": "output_text", "text": text}],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "r1",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        },
    )


def _responses_empty_stream() -> bytes:
    return _responses_sse(
        {"type": "response.created", "response": {"id": "r0"}},
        {
            "type": "response.completed",
            "response": {
                "id": "r0",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 5, "output_tokens": 0},
            },
        },
    )


def _responses_reasoning_only_stream() -> bytes:
    return _responses_sse(
        {"type": "response.created", "response": {"id": "r0"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "reasoning", "id": "rs_1"},
        },
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "delta": "hmm"},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {"type": "reasoning", "id": "rs_1", "summary": []},
        },
        {
            "type": "response.completed",
            "response": {
                "id": "r0",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 5, "output_tokens": 0},
            },
        },
    )


def _responses_error_stream() -> bytes:
    return _responses_sse(
        {"type": "response.created", "response": {"id": "r0"}},
        {"type": "response.error", "error": {"message": "boom", "code": "server_error"}},
    )


def _messages_stream_body(model: str = "opencode-go/grok-4.7") -> bytes:
    return json.dumps({
        "model": model,
        "max_tokens": 64,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()


def _stream_event_names(raw: bytes) -> list:
    return [line[7:].decode() for line in raw.split(b"\n") if line.startswith(b"event: ")]


class TestResponsesFamilyEmptyRetry:
    def test_empty_attempt_is_retried_transparently(self, server):
        port, _ = server
        env = {"OPENCODE_GO_API_KEY": "test-key", "OPENCODE_GO_PROXY_EMPTY_RETRY_WAIT_MS": "0"}
        with mock.patch.dict(os.environ, env), \
             mock.patch(
                 "urllib.request.urlopen",
                 side_effect=[
                     MockUpstreamResponse(_responses_empty_stream()),
                     MockUpstreamResponse(_responses_content_stream()),
                 ],
             ) as urlopen:
            resp, raw = post(port, "/v1/messages", _messages_stream_body())

        assert resp.status == 200
        assert urlopen.call_count == 2
        # The discarded empty attempt leaves no trace: exactly one message.
        assert _stream_event_names(raw).count("message_start") == 1
        assert _stream_event_names(raw)[-2:] == ["message_delta", "message_stop"]
        assert b'"text":"hello"' in raw.replace(b" ", b"")

    def test_reasoning_only_attempt_counts_as_empty(self, server):
        port, _ = server
        env = {"OPENCODE_GO_API_KEY": "test-key", "OPENCODE_GO_PROXY_EMPTY_RETRY_WAIT_MS": "0"}
        with mock.patch.dict(os.environ, env), \
             mock.patch(
                 "urllib.request.urlopen",
                 side_effect=[
                     MockUpstreamResponse(_responses_reasoning_only_stream()),
                     MockUpstreamResponse(_responses_content_stream()),
                 ],
             ) as urlopen:
            resp, raw = post(port, "/v1/messages", _messages_stream_body())

        assert resp.status == 200
        assert urlopen.call_count == 2
        assert _stream_event_names(raw).count("message_start") == 1

    def test_three_empties_still_end_with_terminal_message(self, server):
        port, _ = server
        env = {"OPENCODE_GO_API_KEY": "test-key", "OPENCODE_GO_PROXY_EMPTY_RETRY_WAIT_MS": "0"}
        with mock.patch.dict(os.environ, env), \
             mock.patch(
                 "urllib.request.urlopen",
                 side_effect=[MockUpstreamResponse(_responses_empty_stream()) for _ in range(3)],
             ) as urlopen:
            resp, raw = post(port, "/v1/messages", _messages_stream_body())

        assert resp.status == 200
        assert urlopen.call_count == 3
        names = _stream_event_names(raw)
        assert names[0] == "message_start"
        assert names[-2:] == ["message_delta", "message_stop"]

    def test_upstream_error_is_delivered_not_retried(self, server):
        port, _ = server
        env = {"OPENCODE_GO_API_KEY": "test-key", "OPENCODE_GO_PROXY_EMPTY_RETRY_WAIT_MS": "0"}
        with mock.patch.dict(os.environ, env), \
             mock.patch(
                 "urllib.request.urlopen",
                 return_value=MockUpstreamResponse(_responses_error_stream()),
             ) as urlopen:
            resp, raw = post(port, "/v1/messages", _messages_stream_body())

        assert resp.status == 200
        assert urlopen.call_count == 1
        assert _stream_event_names(raw) == ["message_start", "error"]
        assert b"boom" in raw


# ---------------------------------------------------------------------------
# Go google-native family (gemini-4-argon-contributor)
# ---------------------------------------------------------------------------


def _gemini_response_body(text: str = "hi there") -> bytes:
    return json.dumps({
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 4,
            "candidatesTokenCount": 2,
            "totalTokenCount": 6,
        },
    }).encode()


class TestGoGoogleFamily:
    def test_builder_points_at_go_google_endpoint_with_bearer(self):
        from opencode_go_proxy.go_models import GOOGLE_GEMINI
        from opencode_go_proxy.zen_upstream import (
            _build_family_request,
            go_google_base_url,
        )

        url, body, headers = _build_family_request(
            {"model": "opencode-go/gemini-4-argon-contributor", "input": "hi"},
            GOOGLE_GEMINI,
            "gemini-4-argon-contributor",
            "test-key",
            stream=False,
            session_model="opencode-go/gemini-4-argon-contributor",
            base_url=go_google_base_url(),
            google_auth="bearer",
        )
        assert url == (
            "https://opencode.ai/inference/go/google/v1beta"
            "/models/gemini-4-argon-contributor:generateContent"
        )
        assert headers["authorization"] == "Bearer test-key"
        assert "x-goog-api-key" not in headers
        assert body["contents"][0]["parts"][0]["text"] == "hi"

    def test_builder_stream_url_uses_sse_action(self):
        from opencode_go_proxy.go_models import GOOGLE_GEMINI
        from opencode_go_proxy.zen_upstream import (
            _build_family_request,
            go_google_base_url,
        )

        url, _body, _headers = _build_family_request(
            {"model": "m", "input": "hi"},
            GOOGLE_GEMINI,
            "gemini-4-argon-contributor",
            "test-key",
            stream=True,
            session_model="m",
            base_url=go_google_base_url(),
            google_auth="bearer",
        )
        assert url.endswith("/models/gemini-4-argon-contributor:streamGenerateContent?alt=sse")

    def test_zen_google_still_uses_key_header(self):
        from opencode_go_proxy.zen_upstream import _build_family_request, zen_base_url

        _url, _body, headers = _build_family_request(
            {"model": "zen/gemini-4-argon", "input": "hi"},
            "google_gemini",
            "gemini-4-argon",
            "test-key",
            stream=False,
            session_model="zen/gemini-4-argon",
            base_url=zen_base_url(),
        )
        assert headers["x-goog-api-key"] == "test-key"
        assert "authorization" not in headers

    def test_messages_non_stream_returns_anthropic_message(self, server, tmp_path):
        port, _ = server
        (tmp_path / "user-models.json").write_text(json.dumps({
            "version": 1,
            "models": [{"slug": "gemini-4-argon-contributor", "display_name": "Gemini 4 Argon"}],
        }))
        body = json.dumps({
            "model": "opencode-go/gemini-4-argon-contributor",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        env = {"OPENCODE_GO_API_KEY": "test-key", "OPENCODE_GO_PROXY_STATE_DIR": str(tmp_path)}
        with mock.patch.dict(os.environ, env), \
             mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(_gemini_response_body())):
            resp, raw = post(port, "/v1/messages", body)

        assert resp.status == 200
        message = json.loads(raw)
        assert message["type"] == "message"
        assert message["content"] == [{"type": "text", "text": "hi there"}]
        assert message["usage"]["input_tokens"] == 4
        assert message["usage"]["output_tokens"] == 2

    def test_responses_non_stream_returns_response_object(self, server, tmp_path):
        port, _ = server
        (tmp_path / "user-models.json").write_text(json.dumps({
            "version": 1,
            "models": [{"slug": "gemini-4-argon-contributor", "display_name": "Gemini 4 Argon"}],
        }))
        body = json.dumps({
            "model": "opencode-go/gemini-4-argon-contributor",
            "input": "hi",
        }).encode()
        env = {"OPENCODE_GO_API_KEY": "test-key", "OPENCODE_GO_PROXY_STATE_DIR": str(tmp_path)}
        with mock.patch.dict(os.environ, env), \
             mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(_gemini_response_body())):
            resp, raw = post(port, "/v1/responses", body)

        assert resp.status == 200
        result = json.loads(raw)
        assert result["object"] == "response"
        assert result["output_text"] == "hi there"


class TestGeminiSchemaSanitizer:
    def test_unsupported_keys_stripped_const_becomes_enum(self):
        from opencode_go_proxy.zen_upstream import _sanitize_gemini_schema

        cleaned = _sanitize_gemini_schema({
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "a": {"const": "z", "type": "string", "default": "z"},
                "b": {"$ref": "#/x", "one_of": [{"type": "string"}], "type": "integer"},
            },
            "required": ["a"],
        })
        assert cleaned == {
            "type": "object",
            "properties": {
                "a": {"enum": ["z"], "type": "string"},
                "b": {"type": "integer"},
            },
            "required": ["a"],
        }

    def test_lists_recursed_scalars_untouched(self):
        from opencode_go_proxy.zen_upstream import _sanitize_gemini_schema

        assert _sanitize_gemini_schema({"items": [{"$id": "x", "type": "string"}, 1, "s"]}) == {
            "items": [{"type": "string"}, 1, "s"]
        }
