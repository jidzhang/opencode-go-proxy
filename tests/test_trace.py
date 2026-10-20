"""Level semantics of the structured trace stream.

The writer thread is disabled for these tests (``_ensure_writer`` is mocked
out) so queue contents are deterministic: what :func:`trace` enqueued is
exactly what the writer would have emitted.
"""

from __future__ import annotations

import os
import queue
from unittest import mock

import pytest

from opencode_go_proxy import trace as trace_mod


def _drain(q) -> list:
    """Pull everything currently queued; safe on an already-empty queue."""
    records = []
    while True:
        try:
            records.append(q.get_nowait())
        except queue.Empty:
            return records


@pytest.fixture
def quiet_queue():
    """Hand each test a private queue the trace writer cannot touch.

    ``_ensure_writer`` is mocked so these tests never START the writer. When
    an earlier test (the in-process HTTP suites) already started it, that
    thread stays blocked in ``get()`` on the ORIGINAL queue object, so it can
    never steal records from the swapped-in private queue: what trace()
    enqueued here is exactly what the test drains. Patching restores the
    real queue afterwards and the writer resumes on it untouched.
    """
    with mock.patch.object(trace_mod, "_ensure_writer"), mock.patch.object(
        trace_mod, "_queue", queue.Queue()
    ):
        yield trace_mod._queue


def _events(q) -> list[tuple[str, str]]:
    return [(record["event"], record["level"]) for record in _drain(q)]


def test_default_level_info_hides_debug(quiet_queue) -> None:
    with mock.patch.dict(os.environ, {trace_mod.TRACE_ENV: "1"}, clear=False):
        os.environ.pop(trace_mod.LEVEL_ENV, None)
        trace_mod.trace("request.received", level="debug", request_id="r1")
        trace_mod.trace("server.start", level="info")
        trace_mod.trace("upstream.error", level="warn")
        trace_mod.trace("request.crashed", level="error")
    assert _events(quiet_queue) == [("server.start", "info"), ("upstream.error", "warn"), ("request.crashed", "error")]


def test_unleveled_trace_ranks_as_info(quiet_queue) -> None:
    with mock.patch.dict(os.environ, {trace_mod.TRACE_ENV: "1"}, clear=False):
        os.environ.pop(trace_mod.LEVEL_ENV, None)
        trace_mod.trace("some.legacy_event")
    assert _events(quiet_queue) == [("some.legacy_event", "info")]


def test_level_env_filters_warn_and_above(quiet_queue) -> None:
    with mock.patch.dict(os.environ, {trace_mod.TRACE_ENV: "1", trace_mod.LEVEL_ENV: "warn"}, clear=False):
        trace_mod.trace("server.start", level="info")
        trace_mod.trace("upstream.retry", level="warn")
        trace_mod.trace("request.crashed", level="error")
    assert _events(quiet_queue) == [("upstream.retry", "warn"), ("request.crashed", "error")]


def test_level_env_debug_shows_everything(quiet_queue) -> None:
    with mock.patch.dict(os.environ, {trace_mod.TRACE_ENV: "1", trace_mod.LEVEL_ENV: "debug"}, clear=False):
        trace_mod.trace("request.received", level="debug")
    assert _events(quiet_queue) == [("request.received", "debug")]


def test_warning_alias_accepted(quiet_queue) -> None:
    with mock.patch.dict(os.environ, {trace_mod.TRACE_ENV: "1", trace_mod.LEVEL_ENV: "warning"}, clear=False):
        trace_mod.trace("server.start", level="info")
        trace_mod.trace("upstream.error", level="warn")
    assert _events(quiet_queue) == [("upstream.error", "warn")]


def test_invalid_level_env_falls_back_to_default(quiet_queue) -> None:
    with mock.patch.dict(os.environ, {trace_mod.TRACE_ENV: "1", trace_mod.LEVEL_ENV: "verbose"}, clear=False):
        trace_mod.trace("request.received", level="debug")
        trace_mod.trace("server.start", level="info")
    assert _events(quiet_queue) == [("server.start", "info")]


def test_trace_off_still_kills_all_levels(quiet_queue) -> None:
    with mock.patch.dict(os.environ, {trace_mod.TRACE_ENV: "0", trace_mod.LEVEL_ENV: "debug"}, clear=False):
        trace_mod.trace("request.received", level="debug")
        trace_mod.trace("request.crashed", level="error")
    assert _events(quiet_queue) == []
