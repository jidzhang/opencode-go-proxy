"""Isolate all tests from the real proxy state dir (~/.codex/opencode-go-proxy/).

The HTTP handlers record usage-meter events on every turn. Without this
fixture, integration tests write fake events into the live state dir,
polluting the real meter. Autouse so no test can forget.

The fixture also seeds a minimal full-shape catalog in the isolated state dir
so routing and /v1/models tests never depend on the real machine's catalog
file (~/.codex/model-catalogs/opencode-go.json).
"""
import json
import os
import sys

import pytest


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path) -> None:
    old = os.environ.get("OPENCODE_GO_PROXY_STATE_DIR")
    state = tmp_path / "state"
    os.environ["OPENCODE_GO_PROXY_STATE_DIR"] = str(state)
    state.mkdir(exist_ok=True)
    (state / "opencode-go-catalog.json").write_text(
        json.dumps(
            {
                "fetched_at": "2026-08-10T00:00:00Z",
                "etag": 'W/"test-fixture"',
                "client_version": "0.147.0",
                "models": [
                    {
                        "slug": "deepseek-v4-flash",
                        "display_name": "DeepSeek V4 Flash",
                        "description": "Test fixture.",
                        "input_modalities": ["text", "image"],
                    },
                    {
                        "slug": "deepseek-v4-pro",
                        "display_name": "DeepSeek V4 Pro",
                        "description": "Test fixture.",
                        "input_modalities": ["text", "image"],
                    },
                ],
            }
        )
    )
    yield
    if old is None:
        os.environ.pop("OPENCODE_GO_PROXY_STATE_DIR", None)
    else:
        os.environ["OPENCODE_GO_PROXY_STATE_DIR"] = old


@pytest.fixture(autouse=True)
def isolated_estimate_state() -> None:
    """Zero-input estimation latches are process-global; clear them per test.

    A model that reported real tokens in one test would otherwise disable
    estimation for every later test using the same slug.
    """
    from opencode_go_proxy import meter

    meter.clear_estimate_latches()
    yield
    meter.clear_estimate_latches()


@pytest.fixture(autouse=True)
def isolated_caption_cache() -> None:
    """Caption cache and local-probe state are process-global; clear them.

    A shared caption hit would make integration tests pass without exercising
    the upstream, and a cached local-runtime probe would leak one test's
    runtime state into the next.
    """
    from opencode_go_proxy import vision

    vision.CAPTION_CACHE.clear()
    vision.clear_local_probe_cache()
    yield
    vision.CAPTION_CACHE.clear()
    vision.clear_local_probe_cache()


@pytest.fixture
def script_bin(tmp_path):
    """Write a fake executable for the ``OPENCODE_GO_PROXY_CODEX_BIN``-style env vars.

    The body is a plain python script (no shebang). On POSIX it is written
    with a python3 shebang and the exec bit; CreateProcess cannot exec
    shebang scripts (WinError 193), so on Windows the same body lands in
    ``<name>.py`` next to a ``<name>.bat`` cmd wrapper that forwards all
    arguments and stdin to the interpreter. Returns the path to set in the
    env var. Scripts live in a dedicated subdirectory so their names cannot
    collide with files or directories a test creates in tmp_path (e.g. a
    test using ``tmp_path / "codex"`` as a config directory).
    """
    def _make(name: str, body: str) -> str:
        bin_dir = tmp_path / "fake-bin"
        bin_dir.mkdir(exist_ok=True)
        py = bin_dir / f"{name}.py"
        py.write_text(body, encoding="utf-8", newline="\n")
        if sys.platform == "win32":
            bat = bin_dir / f"{name}.bat"
            bat.write_text(f'@echo off\r\n"{sys.executable}" "{py}" %*\r\n', encoding="ascii", newline="")
            return str(bat)
        script = bin_dir / name
        script.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8", newline="\n")
        script.chmod(0o755)
        return str(script)

    return _make


@pytest.fixture
def fake_codex_bin(script_bin, monkeypatch) -> str:
    """Point OPENCODE_GO_PROXY_CODEX_BIN at a binary that accepts multi_agent_v2."""
    path = script_bin("codex", "raise SystemExit(0)\n")
    monkeypatch.setenv("OPENCODE_GO_PROXY_CODEX_BIN", path)
    return path


@pytest.fixture
def assert_owner_only_mode():
    """Assert a file carries POSIX owner-only (0600) bits.

    Windows has no chmod-enforced file mode, so the check is a no-op there.
    """
    def _check(path) -> None:
        if sys.platform != "win32":
            mode = os.stat(path).st_mode & 0o777
            assert mode == 0o600, f"expected 0600, got {mode:#o}"

    return _check
