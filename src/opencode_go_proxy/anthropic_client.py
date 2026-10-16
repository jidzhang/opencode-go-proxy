"""Anthropic Messages client surface: Claude Code → any certified Go family.

Claude Code speaks the Anthropic Messages protocol (``POST /v1/messages``).
Messages-family Go models keep the existing byte-verbatim relay; Chat- and
Responses-family models are served by translating both directions here:

- Request → Responses payload, reusing the family dispatch's retry, metering,
  prefix caching, image captioning, and dangling-call cleanup.
- The Responses result — object or SSE event sequence — back to the Anthropic
  Message shape (``msg_`` id, content blocks, ``message_start`` …
  ``message_stop``).

Deliberate Phase 1 behavior differences, documented in the README:
``thinking``/``redacted_thinking`` input blocks are dropped (Go models have no
Anthropic-signed thinking replay), ``stop_sequences`` and ``cache_control``
are not forwarded, and ``/v1/messages/count_tokens`` answers a byte-based
estimate rather than a real tokenizer count.
"""

from __future__ import annotations

import http.client
import json
import math
import time
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from typing import Any

from .config import ProxyConfig
from .errors import ProxyError
from .go_models import ANTHROPIC_MESSAGES
from .go_upstream import (
    GO_PROVIDER,
    _client_status,
    _network_error,
    _resolve_go_key,
    go_family_target,
    go_request_identity,
    handle_go_messages_request,
)
from .meter import BYTES_PER_TOKEN_ESTIMATE, record_usage_event
from .opencode_session import opencode_session_headers
from .protocol import (
    DEFAULT_MODEL,
    _as_int,
    drop_dangling_function_calls,
    new_response_id,
)
from .streaming import KeepaliveWriter, _ConnectFailed, _open_upstream_stream
from .trace import trace
from .upstream import default_max_retries
from .zen_upstream import (
    _build_family_request,
    _empty_retry_waits,
    _relay_upstream_error,
    _responses_buffer_has_content,
    _send_json,
    _ZenStreamEngine,
    call_family_responses,
)

Json = dict[str, Any]

COUNT_TOKENS_PATHS = {"/messages/count_tokens", "/v1/messages/count_tokens"}

# Proxy error identities to the Anthropic error taxonomy. Everything unmapped
# falls through to the status-based defaults in anthropic_error_payload.
_ANTHROPIC_ERROR_TYPES = {
    "model_not_found": "not_found_error",
    "invalid_request_error": "invalid_request_error",
    "unsupported_media_type": "invalid_request_error",
    "invalid_host": "invalid_request_error",
    "browser_request_rejected": "permission_error",
    "invalid_client": "permission_error",
    "invalid_caller_token": "authentication_error",
    "remote_auth_not_configured": "authentication_error",
    "insufficient_quota": "rate_limit_error",
}


def anthropic_error_payload(exc: ProxyError) -> Json:
    """One Anthropic-shaped error envelope for the /v1/messages surface."""
    mapped = _ANTHROPIC_ERROR_TYPES.get(exc.error_type or "")
    if mapped is None:
        status = int(exc.status)
        if status == 429:
            mapped = "rate_limit_error"
        elif status == 401:
            mapped = "authentication_error"
        elif status == 403:
            mapped = "permission_error"
        elif status == 413:
            mapped = "request_too_large"
        elif status == 503:
            mapped = "overloaded_error"
        else:
            mapped = "api_error"
    return {"type": "error", "error": {"type": mapped, "message": exc.message}}


def count_messages_tokens(payload: Json) -> int:
    """Byte-based input-token estimate for /v1/messages/count_tokens.

    The real endpoint runs the provider tokenizer; this proxy has no tokenizer,
    so it applies the same bytes/3.3 heuristic the meter uses and the README
    documents the result as an estimate. Claude Code only reads this to pick a
    compaction point, so a conservative approximation is sufficient.
    """
    blob = json.dumps(
        {
            "system": payload.get("system"),
            "messages": payload.get("messages"),
            "tools": payload.get("tools"),
        },
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return max(1, math.ceil(len(blob) / BYTES_PER_TOKEN_ESTIMATE))


# ---------------------------------------------------------------------------
# Request translation: Anthropic Messages → Responses payload
# ---------------------------------------------------------------------------


def _system_text(value: Any) -> str:
    """Flatten the top-level ``system`` parameter (string or blocks) to text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n\n".join(part for part in parts if part)
    return ""


def _image_part(block: Json) -> Json | None:
    """Anthropic image block to a Responses input_image part.

    Base64 sources become data URLs (data:image only, mirroring the proxy's
    SSRF guard); https URL sources pass through. Everything else is dropped.
    """
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    source_type = source.get("type")
    if source_type == "base64":
        data = source.get("data")
        if not isinstance(data, str) or not data:
            return None
        media = str(source.get("media_type") or "image/png")
        url = media if media.startswith("data:") else f"data:{media};base64,{data}"
        if url.startswith("data:image/"):
            return {"type": "input_image", "image_url": url}
        return None
    if source_type == "url":
        url = source.get("url")
        if isinstance(url, str) and url.startswith("https://"):
            return {"type": "input_image", "image_url": url}
    return None


def _tool_result_output(content: Any) -> str:
    """Anthropic tool_result content to the flat text a function_call_output carries."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return json.dumps(content, separators=(",", ":"))


def _translate_tool_choice(choice: Any) -> Any:
    """Anthropic tool_choice to the Responses tool_choice shape; None = omit (auto)."""
    if isinstance(choice, dict):
        choice_type = choice.get("type")
        if choice_type == "auto":
            return "auto"
        if choice_type == "any":
            return "required"
        if choice_type == "none":
            return "none"
        if choice_type == "tool":
            name = choice.get("name")
            if isinstance(name, str) and name:
                return {"type": "function", "name": name}
    return None


def anthropic_to_responses_payload(payload: Json) -> tuple[Json, Json]:
    """Translate an Anthropic Messages request to a Responses payload.

    Returns ``(responses_payload, stats)``. The Responses payload keeps the
    caller's (prefixed) model slug: the family dispatch rewrites it to the
    bare id at the wire boundary. tool_use blocks become function_call items
    and tool_result blocks become function_call_output items; a tool_result
    whose id was never preceded by a tool_use is dropped as an orphan rather
    than handed to a strict gateway that would reject the whole turn.
    """
    stats: Json = {
        "messages": 0,
        "tool_uses": 0,
        "tool_results": 0,
        "thinking_blocks_dropped": 0,
        "images": 0,
        "documents_flattened": 0,
        "unsupported_blocks_dropped": 0,
    }
    input_items: list[Json] = []
    known_call_ids: set[str] = set()

    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        stats["messages"] += 1

        if role == "assistant":
            text_parts: list[str] = []
            blocks = content if isinstance(content, list) else [content] if isinstance(content, str) else []
            for block in blocks:
                if isinstance(block, str):
                    if block:
                        text_parts.append(block)
                    continue
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and isinstance(block.get("text"), str) and block["text"]:
                    text_parts.append(block["text"])
                elif block_type == "tool_use":
                    call_id = block.get("id") or f"call_{uuid.uuid4().hex}"
                    tool_input = block.get("input")
                    if isinstance(tool_input, str):
                        arguments = tool_input
                    else:
                        arguments = json.dumps(tool_input or {}, separators=(",", ":"))
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": str(call_id),
                            "name": str(block.get("name") or ""),
                            "arguments": arguments,
                        }
                    )
                    known_call_ids.add(str(call_id))
                    stats["tool_uses"] += 1
                elif block_type in {"thinking", "redacted_thinking"}:
                    stats["thinking_blocks_dropped"] += 1
                else:
                    stats["unsupported_blocks_dropped"] += 1
            if text_parts:
                input_items.append({"role": "assistant", "content": "\n\n".join(text_parts)})
            continue

        # user (or any non-assistant role) message
        if isinstance(content, str):
            input_items.append({"role": "user", "content": content})
            continue
        parts: list[Json] = []
        for block in content if isinstance(content, list) else []:
            if isinstance(block, str):
                if block:
                    parts.append({"type": "input_text", "text": block})
                continue
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str) and block["text"]:
                parts.append({"type": "input_text", "text": block["text"]})
            elif block_type == "image":
                image = _image_part(block)
                if image is not None:
                    parts.append(image)
                    stats["images"] += 1
                else:
                    stats["unsupported_blocks_dropped"] += 1
            elif block_type == "tool_result":
                call_id = str(block.get("tool_use_id") or "")
                if call_id and call_id in known_call_ids:
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": _tool_result_output(block.get("content")),
                        }
                    )
                    stats["tool_results"] += 1
                else:
                    stats["unsupported_blocks_dropped"] += 1
            elif block_type in {"thinking", "redacted_thinking"}:
                stats["thinking_blocks_dropped"] += 1
            elif block_type == "document":
                title = block.get("title") or "document"
                parts.append({"type": "input_text", "text": f"[document omitted: {title}]"})
                stats["documents_flattened"] += 1
            else:
                stats["unsupported_blocks_dropped"] += 1
        if parts:
            input_items.append({"role": "user", "content": parts})

    out: Json = {
        "model": payload.get("model") or DEFAULT_MODEL,
        "input": input_items,
        "stream": payload.get("stream") is True,
    }
    instructions = _system_text(payload.get("system"))
    if instructions:
        out["instructions"] = instructions

    raw_tools = payload.get("tools")
    if isinstance(raw_tools, list):
        tools = []
        for tool in raw_tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]:
                continue
            tools.append(
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description") or "",
                    "parameters": tool.get("input_schema")
                    or {"type": "object", "properties": {}},
                }
            )
        if tools:
            out["tools"] = tools
            choice = _translate_tool_choice(payload.get("tool_choice"))
            if choice is not None:
                out["tool_choice"] = choice

    max_tokens = payload.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        out["max_output_tokens"] = max_tokens
    if payload.get("temperature") is not None:
        out["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        out["top_p"] = payload["top_p"]
    return out, stats


# ---------------------------------------------------------------------------
# Response translation: Responses object → Anthropic Message
# ---------------------------------------------------------------------------


def _text_parts(seq: Any) -> list[str]:
    """Text values of the ``{"text": ...}`` entries in a block sequence."""
    parts: list[str] = []
    for part in seq if isinstance(seq, list) else []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return parts


def _summary_text(item: Json) -> str:
    """Flat text from a Responses reasoning item (summary parts or content)."""
    parts = _text_parts(item.get("summary"))
    if parts:
        return "\n".join(part for part in parts if part)
    content = item.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(part for part in _text_parts(content) if part)


def _anthropic_usage(usage: Any) -> Json:
    """Responses usage object to the Anthropic usage shape (cache read kept)."""
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0}
    result: Json = {
        "input_tokens": _as_int(usage.get("input_tokens")),
        "output_tokens": _as_int(usage.get("output_tokens")),
    }
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens")
    if isinstance(cached, int) and cached > 0:
        result["cache_read_input_tokens"] = cached
    return result


def _stop_reason(status: Any, saw_tool_use: bool) -> str:
    """Anthropic stop_reason from the Responses terminal status and tool use."""
    if status == "incomplete":
        return "max_tokens"
    if saw_tool_use:
        return "tool_use"
    return "end_turn"


def responses_to_anthropic_message(response: Json, model: str) -> Json:
    """Non-stream Responses object to the Anthropic Message shape.

    Reasoning items become thinking blocks, message items become text blocks,
    and function_call items become tool_use blocks that reuse the upstream
    call_id, so the id Claude Code sends back as tool_use_id round-trips to
    the function_call_output call_id on the next turn.
    """
    content: list[Json] = []
    saw_tool_use = False
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "reasoning":
            text = _summary_text(item)
            if text:
                content.append({"type": "thinking", "thinking": text})
        elif item_type == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        content.append({"type": "text", "text": text})
        elif item_type == "function_call":
            saw_tool_use = True
            arguments = item.get("arguments")
            if isinstance(arguments, str):
                try:
                    input_value = json.loads(arguments) if arguments else {}
                except (ValueError, TypeError):
                    input_value = {}
            else:
                input_value = arguments or {}
            content.append(
                {
                    "type": "tool_use",
                    "id": str(item.get("call_id") or item.get("id") or f"toolu_{uuid.uuid4().hex}"),
                    "name": str(item.get("name") or ""),
                    "input": input_value if isinstance(input_value, dict) else {},
                }
            )
    if not content:
        content.append({"type": "text", "text": ""})
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _stop_reason(response.get("status"), saw_tool_use),
        "stop_sequence": None,
        "usage": _anthropic_usage(response.get("usage")),
    }


# ---------------------------------------------------------------------------
# Stream translation: Responses SSE events → Anthropic Messages SSE
# ---------------------------------------------------------------------------


class ResponsesToAnthropicStream:
    """Consume Responses SSE event dicts; emit Anthropic Messages SSE frames.

    One instance per client stream. ``write`` is the caller's client-write
    callable (``(bytes) -> bool``, False when the client is gone) — either the
    stream engine's locked writer or a KeepaliveWriter. Frames carry both the
    ``event:`` line and the ``data:`` JSON, matching the wire format the real
    Anthropic API emits. ``consume`` returns False once the client write has
    failed, so callers stop feeding events.
    """

    def __init__(self, write: Any, model: str) -> None:
        self._write = write
        self._model = model
        self._message_id = f"msg_{uuid.uuid4().hex}"
        self._dead = False
        self._started = False
        self._finished = False
        self._errored = False
        self._next_index = 0
        # item id → open content-block index (insertion order = block order)
        self._open: dict[str, int] = {}
        self._tool_args_streamed: set[str] = set()
        self._saw_tool_use = False
        self._status = "in_progress"
        self._usage: Json = {"input_tokens": 0, "output_tokens": 0}

    @property
    def alive(self) -> bool:
        return not self._dead

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def errored(self) -> bool:
        return self._errored

    # -- frame plumbing ------------------------------------------------------

    def _frame(self, event_type: str, data: Json) -> bool:
        if self._dead:
            return False
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        ok = self._write(b"event: " + event_type.encode("utf-8") + b"\ndata: " + body + b"\n\n")
        if not ok:
            self._dead = True
        return ok

    def emit_error(self, message: str, *, code: str | None = None) -> bool:
        """Emit the terminal Anthropic error event; the stream ends after it."""
        if self._dead or self._errored or self._finished:
            return self.alive
        error: Json = {"type": code or "api_error", "message": message}
        ok = self._frame("error", {"type": "error", "error": error})
        self._errored = True
        return ok

    # -- state transitions ---------------------------------------------------

    def _ensure_started(self) -> bool:
        if self._started or self._dead:
            return self.alive
        self._started = True
        return self._frame(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self._message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self._model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )

    def _open_block(self, item_id: Any, block: Json) -> bool:
        key = str(item_id or "")
        if not key or key in self._open:
            return self.alive
        index = self._next_index
        self._next_index += 1
        self._open[key] = index
        return self._frame(
            "content_block_start",
            {"type": "content_block_start", "index": index, "content_block": block},
        )

    def _delta(self, item_id: Any, delta: Json) -> bool:
        key = str(item_id or "")
        if key not in self._open:
            # A text delta for a block whose added was never seen (verbatim
            # upstream quirk): open a text block lazily rather than dropping
            # the text. Other delta types are dropped instead — a thinking or
            # input_json delta inside a text block would corrupt the block
            # type the client is accumulating.
            if not key or delta.get("type") != "text_delta":
                return self.alive
            if not self._open_block(key, {"type": "text", "text": ""}):
                return False
        return self._frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": self._open[key], "delta": delta},
        )

    def _close_block(self, item_id: Any) -> bool:
        key = str(item_id or "")
        if key not in self._open:
            return self.alive
        index = self._open.pop(key)
        return self._frame(
            "content_block_stop", {"type": "content_block_stop", "index": index}
        )

    def finish(self) -> bool:
        """Close the message: open blocks, stop_reason, and message_stop.

        Public because the verbatim relay calls it directly when the upstream
        stream ended without a response.completed event.
        """
        self._ensure_started()
        if self._next_index == 0:
            # A stream that never opened a block still needs one: the
            # Anthropic message shape is invalid without a content block.
            self._open_block(f"msg_empty_{uuid.uuid4().hex}", {"type": "text", "text": ""})
        for key in list(self._open):
            self._close_block(key)
        ok = self._frame(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": _stop_reason(self._status, self._saw_tool_use), "stop_sequence": None},
                "usage": dict(self._usage),
            },
        )
        ok = ok and self._frame("message_stop", {"type": "message_stop"})
        # Only a fully written terminal sequence counts as finished: the
        # verbatim relay meters outcome="done" off this flag, so a client
        # that died mid-finish must stay unfinished and meter as gone.
        if ok:
            self._finished = True
        return ok

    # -- event consumption ---------------------------------------------------

    def consume(self, event: Json) -> bool:
        """Translate one Responses SSE event; False when the client is gone."""
        event_type = event.get("type")

        if event_type == "response.created":
            return self._ensure_started()

        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            item_id = event.get("item_id") or item.get("id")
            item_type = item.get("type")
            if not self._ensure_started():
                return False
            if item_type == "reasoning":
                return self._open_block(item_id, {"type": "thinking", "thinking": ""})
            if item_type == "message":
                return self._open_block(item_id, {"type": "text", "text": ""})
            if item_type == "function_call":
                self._saw_tool_use = True
                return self._open_block(
                    item_id,
                    {
                        "type": "tool_use",
                        "id": str(item.get("call_id") or item_id),
                        "name": str(item.get("name") or ""),
                        "input": {},
                    },
                )
            return self.alive

        if event_type == "response.reasoning_summary_text.delta":
            return self._delta(
                event.get("item_id"), {"type": "thinking_delta", "thinking": event.get("delta") or ""}
            )

        if event_type == "response.output_text.delta":
            return self._delta(
                event.get("item_id"), {"type": "text_delta", "text": event.get("delta") or ""}
            )

        if event_type == "response.function_call_arguments.delta":
            item_id = str(event.get("item_id") or "")
            if item_id:
                # Mark the arguments as streamed so the trailing output_item.done
                # (which repeats the full arguments) does not duplicate them.
                self._tool_args_streamed.add(item_id)
            return self._delta(
                event.get("item_id"),
                {"type": "input_json_delta", "partial_json": event.get("delta") or ""},
            )

        if event_type == "response.output_item.done":
            item = event.get("item") or {}
            item_id = event.get("item_id") or item.get("id")
            if item.get("type") == "function_call":
                arguments = item.get("arguments")
                if isinstance(arguments, str) and arguments and str(item_id) not in self._tool_args_streamed:
                    self._delta(item_id, {"type": "input_json_delta", "partial_json": arguments})
            return self._close_block(item_id)

        if event_type == "response.completed":
            response = event.get("response") or {}
            self._usage = _anthropic_usage(response.get("usage"))
            self._status = response.get("status") or "completed"
            for item in response.get("output") or []:
                if isinstance(item, dict) and item.get("type") == "function_call":
                    self._saw_tool_use = True
            return self.finish()

        if event_type in {"response.error", "error"}:
            error = event.get("error") or {}
            message = error.get("message") or "upstream stream error"
            code = error.get("code") or error.get("type")
            return self.emit_error(
                str(message), code=code if isinstance(code, str) and code else None
            )

        # in_progress / summary part events / unknown types: nothing to show.
        return self.alive


class AnthropicStreamEngine(_ZenStreamEngine):
    """_ZenStreamEngine whose client stream speaks Anthropic Messages SSE.

    Everything upstream of send_event — connect retries, per-family chunk
    parsing, accumulation, the empty-completion retry, metering — is inherited
    unchanged; only the client-facing events are translated. The base
    finalize() writes an OpenAI-style ``data: [DONE]`` sentinel that has no
    meaning in the Anthropic stream, so _write drops exactly that frame.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.translator = ResponsesToAnthropicStream(self._write, self.model)

    def _write(self, data: bytes) -> bool:
        if data == b"data: [DONE]\n\n":
            return self.client_alive
        return super()._write(data)

    def send_event(self, event: Json) -> None:
        if not self.client_alive:
            return
        if not self.translator.consume(event):
            self.client_alive = False
            trace(
                "client.disconnected",
                request_id=self.request_id,
                message="client closed connection during stream",
            )

    def send_error(self, message: str, *, code: str | None = None) -> None:
        if not self.client_alive:
            return
        if not self.translator.emit_error(message, code=code):
            self.client_alive = False


# ---------------------------------------------------------------------------
# /v1/messages dispatch
# ---------------------------------------------------------------------------


def handle_anthropic_messages_request(
    handler: Any,
    payload: Json,
    config: ProxyConfig,
    request_id: str,
) -> None:
    """Serve a /v1/messages request for any certified Go model family.

    Messages-family models relay byte-verbatim (the existing fast path);
    Chat- and Responses-family models translate through the Responses payload
    so Claude Code can drive every Go model.
    """
    started = time.time()
    model = payload.get("model") or DEFAULT_MODEL
    bare_id, family = go_request_identity(model)
    if family == ANTHROPIC_MESSAGES:
        handle_go_messages_request(handler, payload, config, request_id)
        return

    api_key = _resolve_go_key(config, request_id, model, started)

    responses_payload, stats = anthropic_to_responses_payload(payload)
    cleaned, dropped_ids = drop_dangling_function_calls(responses_payload.get("input"))
    if dropped_ids:
        responses_payload = dict(responses_payload)
        responses_payload["input"] = cleaned
        trace(
            "request.dangling_calls_dropped",
            request_id=request_id,
            provider=GO_PROVIDER,
            model=model,
            dropped=dropped_ids,
        )
    trace(
        "request.converted",
        request_id=request_id,
        surface="anthropic_messages",
        stats=stats,
        upstream_model=bare_id,
        stream=responses_payload.get("stream") is True,
    )
    extra_headers = opencode_session_headers(handler.headers, responses_payload)
    base_url, google_auth = go_family_target(family, config)

    if responses_payload.get("stream") is not True:
        result = call_family_responses(
            responses_payload,
            config,
            request_id,
            model=model,
            bare_id=bare_id,
            family=family,
            api_key=api_key,
            base_url=base_url,
            provider=GO_PROVIDER,
            extra_headers=extra_headers,
            function_tools_only=True,
            caption_images=True,
            google_auth=google_auth,
        )
        _send_json(handler, responses_to_anthropic_message(result, model))
        return

    url, body, headers = _build_family_request(
        responses_payload,
        family,
        bare_id,
        api_key,
        stream=True,
        session_model=model,
        base_url=base_url,
        extra_headers=extra_headers,
        function_tools_only=True,
        caption_images=True,
        config=config,
        request_id=request_id,
        google_auth=google_auth,
    )
    raw_payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=raw_payload, headers=headers, method="POST")
    trace(
        f"{GO_PROVIDER}.start",
        request_id=request_id,
        url=url,
        bytes=len(raw_payload),
        family=family,
        stream=True,
    )
    try:
        response, retries = _open_upstream_stream(req, config, request_id, default_max_retries())
    except _ConnectFailed as fail:
        attempts = fail.attempts or 0
        exc = fail.exc
        if isinstance(exc, urllib.error.HTTPError):
            status = _client_status(exc.code)
            _relay_upstream_error(handler, status, fail.body, exc.headers)
            record_usage_event(
                model=model,
                status=status,
                duration_ms=int((time.time() - started) * 1000),
                provider=GO_PROVIDER,
                retries=attempts or None,
            )
            return
        error = _network_error(exc, attempts)
        record_usage_event(
            model=model,
            status=int(error.status),
            duration_ms=int((time.time() - started) * 1000),
            provider=GO_PROVIDER,
            retries=attempts or None,
        )
        raise error from exc

    handler.send_response(HTTPStatus.OK)
    handler.send_header("content-type", "text/event-stream")
    handler.send_header("cache-control", "no-cache")
    handler.end_headers()

    if family == "openai_responses":
        # The Responses family relays verbatim upstream SSE; translate the
        # event dicts, with the same transparent empty-stream retry the
        # verbatim /v1/responses surface gets (attempts 1-2 buffered).
        _relay_responses_stream_translated(
            handler,
            response,
            retries,
            model=model,
            request_id=request_id,
            started=started,
            req=req,
            config=config,
        )
        return

    engine = AnthropicStreamEngine(
        handler,
        responses_payload,
        config,
        request_id,
        family=family,
        bare_id=bare_id,
        model=model,
        response_id=new_response_id(),
        started=started,
        raw_payload=raw_payload,
        provider=GO_PROVIDER,
    )
    engine._start_keepalive()
    try:
        engine.run(req, response, retries)
    finally:
        engine._stop_keepalive()


class _FrameBuffer:
    """Collect translated SSE frames in memory for one buffered attempt."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def __call__(self, data: bytes) -> bool:
        self.frames.append(data)
        return True


def _feed_translated_stream(
    response: Any,
    translator: ResponsesToAnthropicStream,
    ka: KeepaliveWriter,
    request_id: str,
    raw_sink: list[bytes] | None = None,
) -> str:
    """Feed one open Responses SSE body into the translator.

    Returns the terminal outcome (``done`` / ``gone`` / ``aborted``) with
    the same finish/error tail the live relay always had; metering and the
    client socket belong to the caller. When ``raw_sink`` is given, every
    raw upstream line is also appended to it for the empty-content check.
    """
    outcome = "aborted"
    try:
        with response as resp:
            for raw_line in resp:
                if not ka.alive or not translator.alive:
                    outcome = "gone"
                    break
                if raw_sink is not None:
                    raw_sink.append(raw_line)
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and not translator.consume(event):
                    outcome = "gone"
                    break
        if not translator.finished and not translator.errored and translator.alive and ka.alive:
            # Upstream ended without response.completed: still give the client
            # a terminal message_delta + message_stop.
            translator.finish()
        if translator.finished:
            outcome = "done"
        elif not translator.alive or not ka.alive:
            outcome = "gone"
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        http.client.HTTPException,
    ) as exc:
        outcome = "aborted"
        trace(
            "upstream.stream_aborted",
            request_id=request_id,
            status=getattr(exc, "code", None) or getattr(exc, "reason", str(exc)),
        )
        if translator.alive:
            translator.emit_error("upstream stream aborted")
    return outcome


def _relay_responses_stream_translated(
    handler: Any,
    response: Any,
    retries: int,
    *,
    model: str,
    request_id: str,
    started: float,
    req: urllib.request.Request,
    config: ProxyConfig,
) -> None:
    """Translate one Responses-family SSE stream to Anthropic events.

    The HTTP SSE head is already committed. Up to three consecutive attempts:
    attempts 1 and 2 translate into a frame buffer that is discarded when the
    attempt ends without client-visible output (empty terminal or a cut
    stream) and flushed byte-identical otherwise; attempt 3 translates live
    so the client always sees a terminal outcome. Reasoning-only attempts
    count as empty, matching the verbatim relay. The terminal outcome meters
    exactly like the verbatim relay (200 done, 0 client-gone, 502 aborted).
    """
    ka = KeepaliveWriter(handler)
    ka.start()

    def _meter(outcome: str, extra_retries: int) -> None:
        total = retries + extra_retries
        status = 200 if outcome == "done" else 0 if outcome == "gone" else 502
        record_usage_event(
            model=model,
            status=status,
            duration_ms=int((time.time() - started) * 1000),
            stream_aborted=outcome != "done",
            retries=total or None,
            provider=GO_PROVIDER,
        )

    def _done(outcome: str, **extra: Any) -> None:
        trace(
            f"{GO_PROVIDER}.done",
            request_id=request_id,
            family="openai_responses",
            stream=True,
            outcome=outcome,
            **extra,
        )

    waits = _empty_retry_waits()
    current = response
    extra = 0
    try:
        for attempt in (1, 2, 3):
            if attempt == 3:
                translator = ResponsesToAnthropicStream(ka.write, model)
                outcome = _feed_translated_stream(current, translator, ka, request_id)
                _meter(outcome, extra)
                _done(outcome, empty_retried=True)
                return
            buf = _FrameBuffer()
            raw_lines: list[bytes] = []
            translator = ResponsesToAnthropicStream(buf, model)
            outcome = _feed_translated_stream(current, translator, ka, request_id, raw_lines)
            if outcome == "gone":
                _meter("gone", extra)
                _done("gone")
                return
            if translator.errored:
                # An upstream terminal error is delivered, never retried.
                ka.writelines(buf.frames)
                _meter("aborted", extra)
                _done("aborted")
                return
            if outcome == "done" and _responses_buffer_has_content(raw_lines):
                if not ka.writelines(buf.frames):
                    outcome = "gone"
                _meter(outcome, extra)
                _done(outcome)
                return
            wait_ms = waits[min(attempt - 1, len(waits) - 1)]
            trace(
                f"{GO_PROVIDER}.empty_retry",
                request_id=request_id,
                family="openai_responses",
                stream=True,
                attempt=attempt + 1,
                wait_ms=wait_ms,
                outcome=outcome,
                lines=len(raw_lines),
            )
            time.sleep(wait_ms / 1000.0)
            if not ka.alive:
                _meter("gone", extra)
                _done("gone")
                return
            try:
                current, retries_n = _open_upstream_stream(
                    req, config, request_id, default_max_retries()
                )
            except _ConnectFailed as fail:
                exc = fail.exc
                trace(
                    f"{GO_PROVIDER}.empty_retry_failed",
                    request_id=request_id,
                    attempt=attempt + 1,
                    status=getattr(exc, "code", None) or getattr(exc, "reason", str(exc)),
                )
                # The retry never produced a stream, so hand the client the
                # buffered attempt: translator.finish() already ran inside
                # the feed (or runs now for a cut attempt), so the frames
                # are a well-formed Anthropic message either way.
                if not translator.finished and not translator.errored and translator.alive:
                    translator.finish()
                ka.writelines(buf.frames)
                _meter("aborted", extra + (fail.attempts or 0))
                _done("aborted")
                return
            extra += 1 + (retries_n or 0)
    finally:
        ka.stop()
