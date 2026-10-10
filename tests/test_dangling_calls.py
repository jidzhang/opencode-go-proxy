"""Dangling function_call sanitizer: abandoned calls must not stick sessions."""

from __future__ import annotations

from opencode_go_proxy.protocol import drop_dangling_function_calls


def _call(cid, name="exec_command"):
    return {"type": "function_call", "id": f"fc_{cid}", "call_id": cid,
            "name": name, "arguments": "{}"}


def _out(cid):
    return {"type": "function_call_output", "id": f"fco_{cid}",
            "call_id": cid, "output": "ok"}


class TestDropDangling:
    def test_paired_calls_kept(self) -> None:
        items = [_call("a"), _out("a"), {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "hi"}]}]
        cleaned, dropped = drop_dangling_function_calls(items)
        assert cleaned is items and dropped == []

    def test_orphan_call_dropped(self) -> None:
        items = [_call("a"), _out("a"), _call("orphan")]
        cleaned, dropped = drop_dangling_function_calls(items)
        assert dropped == ["orphan"]
        assert [(i.get("type"), i.get("call_id")) for i in cleaned] == [
            ("function_call", "a"),
            ("function_call_output", "a"),
        ]

    def test_call_without_ids_kept(self) -> None:
        items = [{"type": "function_call", "name": "x", "arguments": "{}"}]
        cleaned, dropped = drop_dangling_function_calls(items)
        assert cleaned is items and dropped == []

    def test_orphan_output_kept(self) -> None:
        items = [_out("ghost")]
        cleaned, dropped = drop_dangling_function_calls(items)
        assert cleaned is items and dropped == []

    def test_non_list_passthrough(self) -> None:
        for bad in ("text", None, 42, {"type": "x"}):
            cleaned, dropped = drop_dangling_function_calls(bad)
            assert cleaned is bad and dropped == []

    def test_id_fallback_match(self) -> None:
        # call addressed by id only, output referencing it: kept
        items = [{"type": "function_call", "id": "only-id", "name": "x", "arguments": "{}"},
                 {"type": "function_call_output", "id": "o", "call_id": "only-id", "output": "ok"}]
        cleaned, dropped = drop_dangling_function_calls(items)
        assert cleaned is items and dropped == []
