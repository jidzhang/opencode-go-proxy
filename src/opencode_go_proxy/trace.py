"""Structured stderr tracing shared by the proxy modules.

Every module reports one JSON object per line to stderr; the support bundle
collects these as the operational log. Tracing never prints secrets: error
bodies are masked before they leave this module.

The hot path never touches stderr: :func:`trace` only builds a dict and
enqueues it (non-blocking, bounded), while a single daemon writer thread
batches lines to stderr. A full queue drops the record and counts it instead
of ever blocking a request. Set ``OPENCODE_GO_PROXY_TRACE=0`` to disable
tracing entirely.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import re
import sys
import threading
import time
from typing import Any

from .errors import env_flag

# Secret-looking content masked out of upstream error bodies before they are
# traced to stderr (and later shipped in a support bundle). An upstream that
# echoes the request Authorization header back in its error body would
# otherwise leak the API key into logs verbatim.
_MASK_RE = re.compile(
    r"(?i)(authorization[\s\"']*[:=][\s\"']*)[^\"'\r\n,;]+|(\bsk-[A-Za-z0-9_\-]{8,}\b)"
)

TRACE_ENV = "OPENCODE_GO_PROXY_TRACE"
_QUEUE_SIZE = 4096
_BATCH_SIZE = 200


def _mask_trace_body(body: str, limit: int = 2000) -> str:
    text = body[:limit]

    def _repl(m: re.Match) -> str:
        if m.group(1):
            return f"{m.group(1)}<redacted>"
        return "<redacted>"

    return _MASK_RE.sub(_repl, text)


def trace_enabled() -> bool:
    """Whether trace events are emitted (default on; ``OPENCODE_GO_PROXY_TRACE=0`` off)."""
    return env_flag(TRACE_ENV, default_on=True)


_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_SIZE)
_writer_started = False
_writer_lock = threading.Lock()
_dropped = 0
_dropped_lock = threading.Lock()


def trace_dropped_count() -> int:
    """Records dropped because the trace queue was full (never blocks)."""
    with _dropped_lock:
        return _dropped


def _writer_loop() -> None:
    while True:
        try:
            first = _queue.get()
        except Exception:  # noqa: BLE001 - keep the writer alive
            continue
        batch = [first]
        try:
            while len(batch) < _BATCH_SIZE:
                batch.append(_queue.get_nowait())
        except queue.Empty:
            pass
        with _dropped_lock:
            global _dropped
            dropped, _dropped = _dropped, 0
        try:
            lines = []
            if dropped:
                lines.append(json.dumps({"ts": time.time(), "event": "trace.dropped", "count": dropped}, sort_keys=True))
            for record in batch:
                try:
                    lines.append(json.dumps(record, sort_keys=True))
                except (TypeError, ValueError):
                    lines.append(json.dumps({"ts": time.time(), "event": "trace.encode_error"}, sort_keys=True))
            sys.stderr.write("\n".join(lines) + "\n")
            sys.stderr.flush()
        except OSError:
            # stderr gone (shutdown / closed pipe): drop the batch, stay alive.
            # (No task_done bookkeeping: nothing ever calls queue.join().)
            pass


def _ensure_writer() -> None:
    global _writer_started
    if _writer_started:
        return
    with _writer_lock:
        if _writer_started:
            return
        thread = threading.Thread(target=_writer_loop, daemon=True, name="trace-writer")
        thread.start()
        _writer_started = True
        atexit.register(_drain_at_exit)


def _drain_at_exit() -> None:
    """Best-effort synchronous flush of pending lines at interpreter exit."""
    try:
        lines = []
        while True:
            try:
                record = _queue.get_nowait()
            except queue.Empty:
                break
            try:
                lines.append(json.dumps(record, sort_keys=True))
            except (TypeError, ValueError):
                continue
        if lines:
            sys.stderr.write("\n".join(lines) + "\n")
            sys.stderr.flush()
    except Exception:  # noqa: BLE001 - exit path must never raise
        pass


def trace(event: str, **fields: Any) -> None:
    """Enqueue one trace record; never blocks the caller.

    When tracing is disabled this is a single env lookup. Otherwise it builds
    the record dict and offers it to the bounded queue: a full queue drops
    the record (counted via :func:`trace_dropped_count`) rather than slowing
    a request. Serialization and stderr I/O happen on the writer thread.
    """
    if not trace_enabled():
        return
    _ensure_writer()
    record = {"ts": time.time(), "event": event, **fields}
    try:
        _queue.put_nowait(record)
    except queue.Full:
        global _dropped
        with _dropped_lock:
            _dropped += 1
