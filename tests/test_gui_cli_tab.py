"""Tests for the CLI runner tab."""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from grimoire.gui.widgets.cli_tab import ALLOWED_COMMANDS, CliTab  # noqa: E402


class TestAllowlist:
    def test_holds_only_read_only_commands(self) -> None:
        forbidden = {"keys", "migrate", "reindex", "untag", "watch", "ingest", "tag"}
        for argv in ALLOWED_COMMANDS.values():
            assert argv, "every entry needs at least a subcommand"
            assert (
                argv[0] not in forbidden
            ), f"{argv[0]} must not be runnable from the GUI"

    def test_includes_the_expected_commands(self) -> None:
        subcommands = {argv[0] for argv in ALLOWED_COMMANDS.values()}
        assert {"status", "search", "ask", "docs", "config", "cache"} <= subcommands


class TestArgvConstruction:
    def test_subcommand_comes_from_the_dropdown(self, qtbot) -> None:
        tab = CliTab()
        qtbot.addWidget(tab)
        tab.command_combo.setCurrentText("status")
        tab.args_field.setText("--detailed")

        argv = tab.build_argv()

        assert argv[-2:] == ["status", "--detailed"]

    def test_shell_metacharacters_stay_literal(self, qtbot) -> None:
        tab = CliTab()
        qtbot.addWidget(tab)
        tab.command_combo.setCurrentText("status")
        tab.args_field.setText("; rm -rf / && echo pwned")

        argv = tab.build_argv()

        assert "; rm -rf / && echo pwned" not in " ".join(argv[:1])
        assert ";" in argv, "the semicolon is an argument, not a separator"
        assert argv.count("status") == 1

    def test_typed_subcommand_cannot_replace_the_dropdown(self, qtbot) -> None:
        tab = CliTab()
        qtbot.addWidget(tab)
        tab.command_combo.setCurrentText("status")
        tab.args_field.setText("keys create --tier agent")

        argv = tab.build_argv()

        assert argv.index("status") < argv.index("keys")

    def test_unbalanced_quotes_are_reported_not_raised(self, qtbot) -> None:
        tab = CliTab()
        qtbot.addWidget(tab)
        tab.command_combo.setCurrentText("search")
        tab.args_field.setText("'unclosed")

        tab.run()

        assert "quote" in tab.output_view.toPlainText().lower()


class TestProcessLifecycle:
    def test_streams_output_and_reports_exit_code(self, qtbot) -> None:
        tab = CliTab()
        qtbot.addWidget(tab)
        # Run a known-good process instead of grimoire: this test is about
        # QProcess plumbing, not about the CLI.
        tab.start_process(sys.executable, ["-c", "print('hello from child')"])

        qtbot.waitUntil(
            lambda: "hello from child" in tab.output_view.toPlainText(), timeout=10000
        )
        qtbot.waitUntil(
            lambda: "exit code 0" in tab.output_view.toPlainText().lower(),
            timeout=10000,
        )
        assert tab.run_button.isEnabled()

    def test_nonzero_exit_is_surfaced(self, qtbot) -> None:
        tab = CliTab()
        qtbot.addWidget(tab)
        tab.start_process(sys.executable, ["-c", "raise SystemExit(3)"])

        qtbot.waitUntil(
            lambda: "exit code 3" in tab.output_view.toPlainText().lower(),
            timeout=10000,
        )

    def test_stop_terminates_a_running_process(self, qtbot) -> None:
        tab = CliTab()
        qtbot.addWidget(tab)
        tab.start_process(sys.executable, ["-c", "import time; time.sleep(30)"])
        qtbot.waitUntil(lambda: not tab.run_button.isEnabled(), timeout=5000)

        tab.stop()

        qtbot.waitUntil(lambda: tab.run_button.isEnabled(), timeout=10000)
