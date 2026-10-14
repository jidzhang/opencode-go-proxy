#!/usr/bin/env python3
"""Bisect a failing turn: probe arbitrary unit subsets through the proxy.

Units keep each function_call together with its output, so probes never
introduce orphan-call noise. A probe passes max_tokens=16 so a 200 returns
fast (validation still runs first, so 400s reproduce identically).

Usage:
  probe.py ROLLOUT SPEC [--model SLUG] [--port N] [--repeat N] [--timeout SECS]
SPEC is comma-separated unit ranges, e.g. "354-531,620-709" (unit indices
into the sanitized unit list; use --list-units to inspect).
--repeat N sends the same probe N times (flake detection).
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


def load_units(rollout: str, cap: int) -> list:
    """Group items into units; each call travels with its output."""
    from opencode_go_proxy.protocol import drop_dangling_function_calls
    items = load_items(rollout, cap)
    items, _ = drop_dangling_function_calls(items)
    by_call: dict = {}
    for i, p in enumerate(items):
        if isinstance(p, dict) and p.get("type") in ("function_call", "function_call_output"):
            by_call.setdefault(p.get("call_id"), []).append(i)
    units, paired = [], set()
    for i, p in enumerate(items):
        if i in paired:
            continue
        if isinstance(p, dict) and p.get("type") in ("function_call", "function_call_output"):
            idxs = by_call.get(p.get("call_id"), [i])
            units.append([items[j] for j in idxs])
            paired.update(idxs)
        else:
            units.append([p])
    return units


def parse_spec(spec: str) -> list:
    idx: list = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            idx.extend(range(int(lo), int(hi)))
        else:
            idx.append(int(part))
    return idx


def probe_once(units, model: str, port: int, timeout: float, max_tokens: int):
    body = json.dumps({
        "model": model,
        "input": [p for u in units for p in u],
        "stream": False, "max_output_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/responses", data=body,
        headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return ("PASS200", resp.status, len(body) // 1024)
    except urllib.error.HTTPError as exc:
        return ("FAIL400", exc.code,
                exc.read()[:300].decode("utf-8", errors="replace"), len(body) // 1024)
    except Exception as exc:  # noqa: BLE001 - timeouts etc. mean "no fast 400"
        return ("PASS_OTHER", f"{type(exc).__name__}", len(body) // 1024)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Probe input subsets through the proxy.")
    ap.add_argument("rollout")
    ap.add_argument("spec", help="e.g. 354-531,620-709")
    ap.add_argument("--ord-cap", type=int, default=10 ** 12)
    ap.add_argument("--model", default="opencode-go/muse-spark-1.3-contributor")
    ap.add_argument("--port", type=int, default=19999)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--max-tokens", type=int, default=16)
    ap.add_argument("--list-units", action="store_true")
    args = ap.parse_args(argv)

    units = load_units(args.rollout, args.ord_cap)
    print(f"total units: {len(units)}", flush=True)
    if args.list_units:
        for i, u in enumerate(units):
            kinds = [(p.get("type"), (p.get("id") or "")[:18]) for p in u]
            print(i, kinds)
        return 0
    idx = parse_spec(args.spec)
    chosen = [units[i] for i in idx]
    for n in range(args.repeat):
        print(f"probe {args.spec} try{n} ...", flush=True)
        print(probe_once(chosen, args.model, args.port, args.timeout, args.max_tokens),
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
