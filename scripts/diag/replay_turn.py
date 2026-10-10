#!/usr/bin/env python3
"""Replay one Codex turn's history through the proxy, byte-faithful.

Reconstructs a /v1/responses request from a rollout jsonl file (all
response_item rows up to an ordinal cap) and POSTs it to the local proxy.
Used to reproduce gateway 400s offline and to verify proxy-side sanitizers.

Usage:
  replay_turn.py ROLLOUT [ORD_CAP] [--model SLUG] [--port N] [--stream]
                 [--strip-reasoning] [--drop-orphans] [--max-tokens N]
                 [--timeout SECS] [--show-body N]
Defaults: model=opencode-go/muse-spark-1.3-contributor, port=19999,
  non-streaming. Exit 0 on any HTTP status (prints STATUS).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))


def load_items(rollout: str, cap: int) -> list:
    items = []
    with open(rollout, errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") == "response_item" and (row.get("ordinal") or 0) <= cap:
                items.append(row["payload"])
    return items


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Replay a Codex turn through the proxy.")
    ap.add_argument("rollout")
    ap.add_argument("ord_cap", nargs="?", type=int, default=10 ** 12)
    ap.add_argument("--model", default="opencode-go/muse-spark-1.3-contributor")
    ap.add_argument("--port", type=int, default=19999)
    ap.add_argument("--stream", action="store_true")
    ap.add_argument("--strip-reasoning", action="store_true",
                    help="drop type==reasoning items before sending")
    ap.add_argument("--drop-orphans", action="store_true",
                    help="drop function_calls with no later output before sending")
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--show-body", type=int, default=500)
    args = ap.parse_args(argv)

    items = load_items(args.rollout, args.ord_cap)
    if args.drop_orphans:
        from opencode_go_proxy.protocol import drop_dangling_function_calls
        cleaned, dropped = drop_dangling_function_calls({"input": items})
        items = cleaned["input"]
        print(f"dropped {len(dropped)} dangling call(s): {dropped}", flush=True)
    if args.strip_reasoning:
        from opencode_go_proxy.protocol import drop_reasoning_items
        cleaned, dropped = drop_reasoning_items({"input": items})
        items = cleaned["input"]
        print(f"stripped {dropped} reasoning item(s)", flush=True)
    print(f"input items: {len(items)}, KB: {sum(len(json.dumps(p)) for p in items) // 1024}",
          flush=True)

    body_dict: dict = {"model": args.model, "input": items, "stream": args.stream}
    if args.max_tokens is not None:
        body_dict["max_output_tokens"] = args.max_tokens
    body = json.dumps(body_dict).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{args.port}/v1/responses", data=body,
        headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            print("STATUS:", resp.status)
            print(resp.read()[:args.show_body].decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        print("STATUS:", exc.code)
        print(exc.read()[:args.show_body].decode("utf-8", errors="replace"))
    except (TimeoutError, urllib.error.URLError) as exc:
        print(f"NO-RESPONSE ({type(exc).__name__}): validation passed or upstream slow")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
