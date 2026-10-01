"""Tests for GUI configuration parsing.

These tests import no Qt: GuiConfig is deliberately Qt-free so it can be
tested without a display server.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from grimoire.gui.config import DEFAULT_BASE_URL, SUPPORTED_EXTENSIONS, GuiConfig


class TestGuiConfigFromEnv:
    def test_defaults_when_env_empty(self) -> None:
        cfg = GuiConfig.from_env({})
        assert cfg.base_url == DEFAULT_BASE_URL
        assert cfg.base_url == "http://localhost:8001"
        assert cfg.api_key is None
        assert cfg.is_configured is False

    def test_reads_both_variables(self) -> None:
        cfg = GuiConfig.from_env(
            {"GRIMOIRE_API_URL": "http://box:9000", "GRIMOIRE_API_KEY": "grim_agt_x"}
        )
        assert cfg.base_url == "http://box:9000"
        assert cfg.api_key == "grim_agt_x"
        assert cfg.is_configured is True

    def test_strips_trailing_slash_from_base_url(self) -> None:
        cfg = GuiConfig.from_env({"GRIMOIRE_API_URL": "http://box:9000/"})
        assert cfg.base_url == "http://box:9000"

    def test_blank_key_is_treated_as_absent(self) -> None:
        cfg = GuiConfig.from_env({"GRIMOIRE_API_KEY": "   "})
        assert cfg.api_key is None
        assert cfg.is_configured is False


class TestGuiConfigWithApiKey:
    def test_returns_new_instance_with_key(self) -> None:
        cfg = GuiConfig.from_env({})
        updated = cfg.with_api_key("grim_dvl_y")
        assert updated.api_key == "grim_dvl_y"
        assert updated.base_url == cfg.base_url
        assert cfg.api_key is None, "original config must be unchanged"


class TestSupportedExtensions:
    def test_matches_the_parser(self) -> None:
        # The GUI duplicates this set to avoid importing Docling at startup.
        # If the parser gains a format, this test is what catches the drift.
        from grimoire.core.parser import DocumentParser

        assert frozenset(DocumentParser.SUPPORTED_EXTENSIONS) == SUPPORTED_EXTENSIONS


class TestSessionId:
    def test_defaults_to_none(self) -> None:
        assert GuiConfig().session_id is None
        assert GuiConfig.from_env({}).session_id is None

    def test_from_env_never_reads_a_session_id(self) -> None:
        """Callers mint one per launch; it is not user configuration."""
        cfg = GuiConfig.from_env({"GRIMOIRE_SESSION_ID": "abc"})
        assert cfg.session_id is None

    @pytest.mark.parametrize("good", ["a1b2c3d4e5f6", "a-b_C9", "x", "a" * 64])
    def test_accepts_safe_ids(self, good: str) -> None:
        assert GuiConfig(session_id=good).session_id == good

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "a" * 65,
            "a b",
            "x\r\nInjected: 1",
            "abc\n",  # `$` would let a trailing newline through
            "caf\u00e9",
            "a/b",
        ],
    )
    def test_rejects_unsafe_ids(self, bad: str) -> None:
        with pytest.raises(ValueError, match="session_id"):
            GuiConfig(session_id=bad)

    def test_replace_is_validated_too(self) -> None:
        with pytest.raises(ValueError, match="session_id"):
            replace(GuiConfig(), session_id="bad id")

    def test_error_message_does_not_echo_the_value(self) -> None:
        """The rejected value may be attacker-controlled; keep it out of logs."""
        with pytest.raises(ValueError) as excinfo:
            GuiConfig(session_id="x\r\nInjected: 1")
        assert "Injected" not in str(excinfo.value)
