#!/usr/bin/env python3
"""Smoke-test a chat-family model with function tools attached.

Catches gateway tool-schema rejections (it names the offending function).
Relies on the proxy merging its codex_app snapshot, like real traffic.

Usage:
  tools_smoke.py [--model SLUG] [--port N] [--timeout SECS]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

TOOLS = [
    {"type": "function", "name": "exec_command", "description": "Run a shell command",
     "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"]}},
    {"type": "function", "name": "create_thread", "namespace": "codex_app",
     "description": "Create thread",
     "parameters": {"type": "object", "properties": {"target": {"type": "object"}}}},
]


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Tools-bearing smoke test.")
    ap.add_argument("--model", default="opencode-go/deepseek-v4.1-flash")
    ap.add_argument("--port", type=int, default=19999)
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)
    body = json.dumps({
        "model": args.model,
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "hi, reply with ok"}]}],
        "tools": TOOLS, "stream": False, "max_output_tokens": 16,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{args.port}/v1/responses", data=body,
        headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            print("STATUS:", resp.status)
            print(resp.read()[:200].decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        print("STATUS:", exc.code)
        print(exc.read()[:500].decode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
