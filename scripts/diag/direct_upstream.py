#!/usr/bin/env python3
"""POST a turn's history straight to the upstream, bypassing the proxy.

Isolates proxy-vs-gateway blame: identical bytes must yield identical
verdicts with and without the proxy in the loop. Auth key comes from
$OPENCODE_GO_API_KEY (same name the proxy uses); never logged.

Usage:
  direct_upstream.py ROLLOUT [ORD_CAP] [--repeat N] [--timeout SECS]
Sends the sanitized input (orphans dropped, like the proxy does) as a bare
Responses body to https://opencode.ai/zen/go/v1/responses.
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
    ap = argparse.ArgumentParser(description="POST history direct to upstream.")
    ap.add_argument("rollout")
    ap.add_argument("ord_cap", nargs="?", type=int, default=10 ** 12)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--model", default="muse-spark-1.3-contributor")
    args = ap.parse_args(argv)

    key = os.environ.get("OPENCODE_GO_API_KEY") or os.environ.get("OPENCODE_API_KEY")
    if not key:
        print("need $OPENCODE_GO_API_KEY in env", file=sys.stderr)
        return 2
    from opencode_go_proxy.protocol import drop_dangling_function_calls
    from opencode_go_proxy.opencode_session import opencode_session_headers

    items = load_items(args.rollout, args.ord_cap)
    items, _ = drop_dangling_function_calls(items)
    payload = {"model": args.model, "input": items, "stream": False, "max_output_tokens": 16}
    body = json.dumps(payload).encode()
    headers = {"content-type": "application/json", "authorization": "Bearer " + key,
               "user-agent": "opencode-go-proxy/diag", "accept": "application/json",
               **opencode_session_headers(None, {"input": items})}
    for n in range(args.repeat):
        req = urllib.request.Request("https://opencode.ai/zen/go/v1/responses",
                                     data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                print(f"try{n}: 200", flush=True)
        except urllib.error.HTTPError as exc:
            print(f"try{n}: {exc.code} {exc.read()[:150]}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"try{n}: {type(exc).__name__}: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
