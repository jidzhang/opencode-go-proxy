"""Env-matrix coverage for every proxy config knob, present and absent."""
import os
from unittest import mock

import pytest

from opencode_go_proxy import meter, secrets, upstream, upstream_headers
from opencode_go_proxy.app import build_parser
from opencode_go_proxy.config import resolve_chat_base_url


class TestEnvMatrix:
    @pytest.mark.parametrize("value,expected", [("7", 7), ("0", 0), ("", 2)])
    def test_max_retries(self, value, expected) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_MAX_RETRIES": value}, clear=True):
            assert upstream.default_max_retries() == expected

    def test_state_dir(self, tmp_path) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_STATE_DIR": str(tmp_path)}, clear=True):
            assert meter.state_dir() == str(tmp_path)
        with mock.patch.dict(os.environ, {}, clear=True):
            assert meter.state_dir() == meter.DEFAULT_STATE_DIR

    def test_key_env(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_API_KEY_ENV": "CUSTOM_KEY_VAR"}, clear=True):
            assert secrets.configured_key_env() == "CUSTOM_KEY_VAR"
        with mock.patch.dict(os.environ, {}, clear=True):
            assert secrets.configured_key_env() == "OPENCODE_GO_API_KEY"

    def test_estimate_zero_input_kill_switch(self) -> None:
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_ESTIMATE_ZERO_INPUT": "0"}, clear=True):
            assert meter.estimate_input_tokens("m", 1000, usage) is None
        with mock.patch.dict(os.environ, {}, clear=True):
            assert meter.estimate_input_tokens("m", 1000, usage) >= 1000

    def test_user_agent_header(self) -> None:
        # The adapter identity is still applied by the shared header builder.
        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_PROXY_USER_AGENT": "custom-ua/1"}, clear=True
        ):
            assert upstream_headers.base_upstream_headers()["user-agent"] == "custom-ua/1"
        with mock.patch.dict(os.environ, {}, clear=True):
            assert upstream_headers.base_upstream_headers()["user-agent"].startswith(
                "opencode-go-proxy/"
            )

    @pytest.mark.parametrize("name", ["OPENCODE_GO_BASE_URL", "OPENCODE_ZEN_BASE_URL"])
    def test_base_url_override(self, name) -> None:
        with mock.patch.dict(os.environ, {name: "https://example.test/v1"}, clear=True):
            assert resolve_chat_base_url() == "https://example.test/v1"

    def test_base_url_precedence(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_GO_BASE_URL": "https://a.test/v1", "OPENCODE_ZEN_BASE_URL": "https://b.test/v1"},
            clear=True,
        ):
            assert resolve_chat_base_url() == "https://a.test/v1"

    def test_parser_env_defaults(self) -> None:
        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_PROXY_PORT": "8801", "OPENCODE_GO_PROXY_BIND": "127.0.0.1"}, clear=True
        ):
            args = build_parser().parse_args([])
            assert args.port == 8801
            assert args.bind == "127.0.0.1"
