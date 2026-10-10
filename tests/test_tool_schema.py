"""Merged tool schemas must satisfy the chat gateway's strict validation.

The gateway rejects a whole turn when any function schema lacks a top-level
``"type": "object"`` (observed: automation_update's top-level oneOf). The
normalizer must guarantee the marker while preserving sibling keywords.
"""

from __future__ import annotations

import json

from opencode_go_proxy.codex_tools import _normalize_tool


class TestNormalizeToolSchema:
    def test_oneof_schema_gains_object_marker(self) -> None:
        params = {"oneOf": [{"type": "object", "properties": {"a": {}}}]}
        tool = _normalize_tool("codex_app__automation_update", "desc", params)
        out = tool["function"]["parameters"]
        assert out["type"] == "object"
        assert out["oneOf"] == params["oneOf"]

    def test_object_schema_untouched(self) -> None:
        params = {"type": "object", "properties": {"x": {"type": "string"}}}
        tool = _normalize_tool("ns__t", "d", params)
        assert tool["function"]["parameters"] is params

    def test_explicit_non_object_type_preserved(self) -> None:
        params = {"type": "array", "items": {"type": "string"}}
        tool = _normalize_tool("ns__t", "d", params)
        assert tool["function"]["parameters"]["type"] == "array"

    def test_explicit_null_type_does_not_overwrite_marker(self) -> None:
        params = {"type": None, "properties": {"a": {"type": "string"}}}
        original = dict(params)
        tool = _normalize_tool("ns__t", "d", params)
        out = tool["function"]["parameters"]
        assert out["type"] == "object"
        assert out["properties"] == {"a": {"type": "string"}}
        assert params == original  # caller dict never mutated

    def test_non_dict_parameters_default(self) -> None:
        for bad in (None, "x", 42, [1]):
            tool = _normalize_tool("ns__t", "d", bad)
            assert tool["function"]["parameters"] == {"type": "object", "properties": {}}

    def test_checked_in_snapshot_satisfies_gateway(self) -> None:
        with open("contrib/codex-app-tools.json", encoding="utf-8") as handle:
            data = json.load(handle)
        tools = data if isinstance(data, list) else data.get("tools", [])
        assert tools, "snapshot must not be empty"
        for tool in tools:
            fn = tool.get("function", tool)
            params = fn.get("parameters")
            assert isinstance(params, dict), fn.get("name")
            assert params.get("type") == "object", fn.get("name")
