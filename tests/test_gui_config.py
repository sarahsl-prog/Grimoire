"""Tests for GUI configuration parsing.

These tests import no Qt: GuiConfig is deliberately Qt-free so it can be
tested without a display server.
"""

from __future__ import annotations

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
