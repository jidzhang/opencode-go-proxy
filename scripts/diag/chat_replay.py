#!/usr/bin/env python3
"""Translate a turn to chat-completions shape and replay it verbatim.

Runs the proxy's own responses_payload_to_chat_payload over rollout items,
then POSTs the messages to the proxy's /v1/chat/completions (verbatim relay).
Distinguishes message-shape failures from tools/instructions failures.

Usage:
  chat_replay.py ROLLOUT [ORD_CAP] [--model SLUG] [--port N] [--timeout SECS]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from replay_turn import load_items  # noqa: E402


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Replay translated chat messages.")
    ap.add_argument("rollout")
    ap.add_argument("ord_cap", nargs="?", type=int, default=10 ** 12)
    ap.add_argument("--model", default="opencode-go/deepseek-v4.1-flash")
    ap.add_argument("--port", type=int, default=19999)
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    from opencode_go_proxy.protocol import (
        drop_dangling_function_calls,
        responses_payload_to_chat_payload,
    )
    items = load_items(args.rollout, args.ord_cap)
    items, _ = drop_dangling_function_calls(items)
    chat, _, stats = responses_payload_to_chat_payload(
        {"model": args.model, "input": items})
    print("messages:", len(chat["messages"]), "tools:", len(chat.get("tools") or []),
          "stats:", json.dumps(stats)[:200], flush=True)
    body = json.dumps({"model": args.model, "messages": chat["messages"],
                       "stream": False, "max_tokens": 16}).encode()
    print("chat body KB:", len(body) // 1024, flush=True)
    req = urllib.request.Request(
        f"http://127.0.0.1:{args.port}/v1/chat/completions", data=body,
        headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            print("STATUS:", resp.status)
            print(resp.read()[:300].decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        print("STATUS:", exc.code)
        print(exc.read()[:500].decode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
