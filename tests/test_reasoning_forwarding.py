"""Regression: reasoning effort and client attribution must survive proxying."""

from opencode_go_proxy import zen_upstream
from opencode_go_proxy.anthropic_client import (
    _anthropic_thinking_to_effort,
    anthropic_to_responses_payload,
)
from opencode_go_proxy.config import ProxyConfig
from opencode_go_proxy.opencode_session import opencode_session_headers
from opencode_go_proxy.protocol import (
    extract_reasoning_effort,
    responses_payload_to_chat_payload,
)


def config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=8787,
        chat_base_url="https://opencode.ai/inference/go/openai/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=10,
        max_body_bytes=1024 * 1024,
    )


def test_session_headers_forward_client_attribution() -> None:
    headers = opencode_session_headers(
        {"x-opencode-session": "s1", "x-opencode-client": "codex"},
        {"input": []},
    )
    assert headers == {"x-opencode-session": "s1", "x-opencode-client": "codex"}
    # No client header downstream -> default to cli for upstream parity.
    assert opencode_session_headers({"x-opencode-session": "s1"}, {"input": []}) == {
        "x-opencode-session": "s1",
        "x-opencode-client": "cli",
    }


def test_responses_reasoning_reaches_chat_upstream() -> None:
    chat, _, _ = responses_payload_to_chat_payload(
        {
            "model": "m",
            "input": [{"role": "user", "content": "hi"}],
            "reasoning": {"effort": "xhigh", "summary": "auto"},
        }
    )
    assert chat["reasoning_effort"] == "xhigh"


def test_claude_thinking_reaches_responses_upstream() -> None:
    out, _ = anthropic_to_responses_payload(
        {
            "model": "m",
            "messages": [],
            "thinking": {"type": "enabled", "budget_tokens": 25000},
        }
    )
    assert out["reasoning"] == {"effort": "xhigh"}


def test_verbatim_responses_keeps_reasoning_and_client() -> None:
    _, body, headers = zen_upstream._build_family_request(
        {
            "model": "opencode-go/muse-spark-1.3-contributor",
            "input": "hi",
            "stream": True,
            "reasoning": {"effort": "xhigh", "summary": "auto"},
        },
        "openai_responses",
        "muse-spark-1.3-contributor",
        "k",
        stream=True,
        session_model="opencode-go/muse-spark-1.3-contributor",
        base_url=config().chat_base_url,
        extra_headers=opencode_session_headers(
            {"x-opencode-session": "s1", "x-opencode-client": "cli"},
            {"input": "hi"},
        ),
        function_tools_only=True,
    )
    assert body["reasoning"] == {"effort": "xhigh", "summary": "auto"}
    assert headers["x-opencode-client"] == "cli"


def test_anthropic_thinking_budget_fits_max_tokens() -> None:
    # Anthropic requires budget_tokens < max_tokens; the default 8192 cap must
    # be raised instead of shipping an invalid budget.
    body = zen_upstream.responses_payload_to_anthropic_payload(
        {"model": "m", "input": [{"role": "user", "content": "hi"}], "reasoning": {"effort": "medium"}}
    )
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 10000}
    assert body["max_tokens"] > 10000
    # A caller max_output_tokens already above the budget stays untouched.
    body = zen_upstream.responses_payload_to_anthropic_payload(
        {
            "model": "m",
            "input": [{"role": "user", "content": "hi"}],
            "reasoning": {"effort": "xhigh"},
            "max_output_tokens": 64000,
        }
    )
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 32000}
    assert body["max_tokens"] == 64000


def test_anthropic_thinking_replaces_sampling_controls() -> None:
    # temperature is rejected alongside extended thinking; it only rides
    # along when thinking stays off.
    body = zen_upstream.responses_payload_to_anthropic_payload(
        {
            "model": "m",
            "input": [{"role": "user", "content": "hi"}],
            "reasoning": {"effort": "low"},
            "temperature": 0.7,
        }
    )
    assert "thinking" in body
    assert "temperature" not in body
    body = zen_upstream.responses_payload_to_anthropic_payload(
        {
            "model": "m",
            "input": [{"role": "user", "content": "hi"}],
            "reasoning": {"effort": "none"},
            "temperature": 0.7,
        }
    )
    assert "thinking" not in body
    assert body["temperature"] == 0.7


def test_gemini_thinking_budget_respects_flash_cap() -> None:
    body = zen_upstream.responses_payload_to_gemini_payload(
        {"model": "m", "input": [{"role": "user", "content": "hi"}], "reasoning": {"effort": "xhigh"}}
    )
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 24576}
    # "none" (and any unknown level) leaves the thinking config off instead of
    # silently enabling an 8000-token budget.
    body = zen_upstream.responses_payload_to_gemini_payload(
        {"model": "m", "input": [{"role": "user", "content": "hi"}], "reasoning": {"effort": "none"}}
    )
    assert "thinkingConfig" not in body.get("generationConfig", {})


def test_effort_extraction_agrees_across_families() -> None:
    # The whitespace-effort + flat-fallback shape must resolve identically on
    # the chat path and the anthropic path (one shared extractor).
    payload = {
        "model": "m",
        "input": [{"role": "user", "content": "hi"}],
        "reasoning": {"effort": "  "},
        "reasoning_effort": "xhigh",
    }
    assert extract_reasoning_effort(payload) == "xhigh"
    chat, _, _ = responses_payload_to_chat_payload(payload)
    body = zen_upstream.responses_payload_to_anthropic_payload(payload)
    assert chat["reasoning_effort"] == "xhigh"
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 32000}


def test_malformed_thinking_budget_takes_medium_fallback() -> None:
    # bool is an int subclass: it must not compare as 1 and map to "low".
    assert _anthropic_thinking_to_effort({"thinking": {"type": "enabled", "budget_tokens": True}}) == "medium"
    assert _anthropic_thinking_to_effort({"thinking": {"type": "enabled", "budget_tokens": 25000}}) == "xhigh"


def test_anthropic_thinking_skipped_mid_tool_loop() -> None:
    # The final assistant turn would need a thinking block before tool results;
    # Responses-shaped history has none, so the continuation runs without
    # thinking instead of being rejected outright.
    body = zen_upstream.responses_payload_to_anthropic_payload(
        {
            "model": "m",
            "input": [
                {"role": "user", "content": "hi"},
                {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            ],
            "reasoning": {"effort": "high"},
        }
    )
    assert "thinking" not in body


def test_anthropic_forced_tool_choice_dropped_with_thinking() -> None:
    # Extended thinking locks tool_choice to auto/none upstream.
    payload = {
        "model": "m",
        "input": [{"role": "user", "content": "hi"}],
        "reasoning": {"effort": "low"},
        "tools": [
            {
                "type": "function",
                "name": "f",
                "description": "",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        "tool_choice": "required",
    }
    body = zen_upstream.responses_payload_to_anthropic_payload(payload)
    assert "thinking" in body
    assert "tool_choice" not in body
    # Without thinking the forced choice still translates.
    payload["reasoning"] = {"effort": "none"}
    body = zen_upstream.responses_payload_to_anthropic_payload(payload)
    assert "thinking" not in body
    assert body["tool_choice"] == {"type": "any"}
    # tool_choice none now maps to the anthropic none shape, not auto.
    payload["tool_choice"] = "none"
    body = zen_upstream.responses_payload_to_anthropic_payload(payload)
    assert body["tool_choice"] == {"type": "none"}
