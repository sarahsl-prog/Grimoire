"""Tests for wiki CLI commands."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from grimoire.cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestWikiCLI:
    """Test wiki command group."""

    def test_wiki_help(self, runner: CliRunner) -> None:
        """Wiki group shows help text."""
        result = runner.invoke(cli, ["wiki", "--help"])
        assert result.exit_code == 0
        assert "compile" in result.output
        assert "list" in result.output
        assert "show" in result.output
        assert "export" in result.output
        assert "status" in result.output

    def test_wiki_compile_help(self, runner: CliRunner) -> None:
        """Compile subcommand shows help."""
        result = runner.invoke(cli, ["wiki", "compile", "--help"])
        assert result.exit_code == 0
        assert "--doc-id" in result.output
        assert "--category" in result.output

    def test_wiki_list_help(self, runner: CliRunner) -> None:
        """List subcommand shows help."""
        result = runner.invoke(cli, ["wiki", "list", "--help"])
        assert result.exit_code == 0

    def test_wiki_show_help(self, runner: CliRunner) -> None:
        """Show subcommand shows help."""
        result = runner.invoke(cli, ["wiki", "show", "--help"])
        assert result.exit_code == 0
        assert "SLUG" in result.output.upper()

    def test_wiki_export_help(self, runner: CliRunner) -> None:
        """Export subcommand shows help."""
        result = runner.invoke(cli, ["wiki", "export", "--help"])
        assert result.exit_code == 0

    def test_wiki_status_help(self, runner: CliRunner) -> None:
        """Status subcommand shows help."""
        result = runner.invoke(cli, ["wiki", "status", "--help"])
        assert result.exit_code == 0