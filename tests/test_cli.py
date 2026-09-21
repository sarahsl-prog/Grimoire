"""Tests for the Grimoire CLI commands.

Tests cover:
- Command registration and help text
- Ingest command (file and directory)
- Query commands (ask, search)
- Generate commands (summary, flashcards, cliff-notes, outline)
- Config commands (init, show)
- Status and cache commands
- Error handling
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner
from pytest_httpx import HTTPXMock

from grimoire.cli.main import cli
from grimoire.cli.status import _vector_store_summary
from grimoire.config.settings import VectorStoreType


@pytest.fixture
def runner() -> CliRunner:
    """Click CLI test runner."""
    return CliRunner()


def _mock_db_ctx() -> MagicMock:
    """Create a mock for get_db_context that returns an async context manager."""
    ctx = MagicMock()
    session = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# Shared patch targets - patch where the names are USED (the import target module)
_INGEST = "grimoire.cli.ingest"
_QUERY = "grimoire.cli.query"
_GEN = "grimoire.cli.generate"
_STATUS = "grimoire.cli.status"
_DOCS = "grimoire.cli.docs"


# =============================================================================
# Command Registration Tests
# =============================================================================


class TestCommandRegistration:
    """Verify all commands are registered and show help."""

    def test_main_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        for cmd in [
            "ingest",
            "ask",
            "search",
            "generate",
            "category",
            "watch",
            "status",
            "reindex",
            "config",
            "cache",
            "tag",
            "untag",
            "docs",
        ]:
            assert cmd in result.output

    def test_version(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "2.0.0" in result.output

    @pytest.mark.parametrize(
        "cmd,expected",
        [
            (["ingest", "--help"], "--recursive"),
            (["ask", "--help"], "QUESTION"),
            (["search", "--help"], "--format"),
            (["generate", "--help"], "summary"),
            (["category", "--help"], "add"),
            (["watch", "--help"], "start"),
            (["config", "--help"], "init"),
            (["cache", "--help"], "clear"),
            (["status", "--help"], "--detailed"),
            (["docs", "--help"], "list"),
        ],
    )
    def test_subcommand_help(
        self, runner: CliRunner, cmd: list[str], expected: str
    ) -> None:
        result = runner.invoke(cli, cmd)
        assert result.exit_code == 0
        assert expected in result.output


# =============================================================================
# Ingest Tests
# =============================================================================


class TestIngestCommand:
    """Test the ingest command."""

    @patch(f"{_INGEST}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_INGEST}.setup_db", new_callable=AsyncMock)
    @patch(f"{_INGEST}.build_ingestion_agent")
    @patch(f"{_INGEST}.get_db_context")
    def test_ingest_single_file(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        test_file = tmp_path / "doc.pdf"
        test_file.write_text("test")

        mock_result = MagicMock(
            status="completed", chunks_created=5, tags_applied=2, duration_ms=150
        )
        mock_agent = MagicMock()
        mock_agent.ingest_file = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["ingest", str(test_file)])
        assert result.exit_code == 0
        assert "5 chunks" in result.output

    @patch(f"{_INGEST}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_INGEST}.setup_db", new_callable=AsyncMock)
    @patch(f"{_INGEST}.build_ingestion_agent")
    @patch(f"{_INGEST}.get_db_context")
    def test_ingest_directory(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "a.txt").write_text("a")

        mock_result = MagicMock(
            succeeded=3,
            total=4,
            skipped=0,
            failed=1,
            duration_ms=500,
            results=[
                MagicMock(
                    status="failed", file_path="/x.pdf", error_message="parse error"
                )
            ],
        )
        mock_agent = MagicMock()
        mock_agent.ingest_directory = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["ingest", str(tmp_path)])
        assert result.exit_code == 0
        assert "3/4 succeeded" in result.output
        assert "parse error" in result.output

    @patch(f"{_INGEST}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_INGEST}.setup_db", new_callable=AsyncMock)
    @patch(f"{_INGEST}.build_ingestion_agent")
    @patch(f"{_INGEST}.get_db_context")
    def test_ingest_skipped_duplicate(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        test_file = tmp_path / "dup.pdf"
        test_file.write_text("test")

        mock_agent = MagicMock()
        mock_agent.ingest_file = AsyncMock(return_value=MagicMock(status="skipped"))
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["ingest", str(test_file)])
        assert result.exit_code == 0
        assert "Skipped" in result.output


# =============================================================================
# Query Tests
# =============================================================================


class TestAskCommand:
    """Test the ask command."""

    @patch(f"{_QUERY}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.setup_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.build_query_agent")
    @patch(f"{_QUERY}.get_db_context")
    def test_ask_with_answer(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_cite = MagicMock(
            document_title="Paper A", document_id="abc12345", relevance_score=0.92
        )
        mock_result = MagicMock(
            answer="The key finding is X.", citations=[mock_cite], cached=False
        )

        mock_agent = MagicMock()
        mock_agent.query = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["ask", "What is X?"])
        assert result.exit_code == 0
        assert "The key finding is X." in result.output
        assert "Paper A" in result.output

    @patch(f"{_QUERY}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.setup_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.build_query_agent")
    @patch(f"{_QUERY}.get_db_context")
    def test_ask_no_results(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock(answer="", citations=[])
        mock_agent = MagicMock()
        mock_agent.query = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["ask", "Unknown topic?"])
        assert result.exit_code == 0
        assert "No relevant information" in result.output

    @patch(f"{_QUERY}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.setup_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.build_query_agent")
    @patch(f"{_QUERY}.get_db_context")
    def test_ask_with_tag_filter(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock(answer="Answer.", citations=[], cached=False)
        mock_agent = MagicMock()
        mock_agent.query = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(
            cli, ["ask", "test?", "--tag", "research", "--tag", "ai"]
        )
        assert result.exit_code == 0
        call_kwargs = mock_agent.query.call_args[1]
        assert call_kwargs["filter_dict"] == {"tags": ["research", "ai"]}

    @patch(f"{_QUERY}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.setup_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.build_query_agent")
    @patch(f"{_QUERY}.get_db_context")
    def test_ask_no_cache_flag(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock(answer="Answer.", citations=[], cached=False)
        mock_agent = MagicMock()
        mock_agent.query = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["ask", "test?", "--no-cache"])
        assert result.exit_code == 0
        call_kwargs = mock_agent.query.call_args[1]
        assert call_kwargs["use_cache"] is False


class TestAskSecurityFilters:
    """Phase 9 — security filter flags on ``grimoire ask``."""

    @patch(f"{_QUERY}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.setup_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.build_query_agent")
    @patch(f"{_QUERY}.get_db_context")
    def test_severity_and_tactic_flags_compose(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock(answer="A.", citations=[], cached=False)
        mock_agent = MagicMock()
        mock_agent.query = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(
            cli,
            ["ask", "powershell", "--severity", "high", "--tactic", "execution"],
        )
        assert result.exit_code == 0, result.output
        call_kwargs = mock_agent.query.call_args[1]
        assert call_kwargs["filter_dict"]["severity"] == "high"
        assert call_kwargs["filter_dict"]["mitre_tactic"] == "execution"

    @patch(f"{_QUERY}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.setup_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.build_query_agent")
    @patch(f"{_QUERY}.get_db_context")
    def test_all_security_flags_propagate(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock(answer="A.", citations=[], cached=False)
        mock_agent = MagicMock()
        mock_agent.query = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(
            cli,
            [
                "ask",
                "q",
                "--severity",
                "critical",
                "--tactic",
                "lateral-movement",
                "--technique",
                "T1021",
                "--source-type",
                "sigma_rule",
                "--cve-id",
                "CVE-2024-1234",
                "--content-date-after",
                "2024-01-01",
                "--platform",
                "windows",
                "--platform",
                "linux",
            ],
        )
        assert result.exit_code == 0, result.output
        filt = mock_agent.query.call_args[1]["filter_dict"]
        assert filt["severity"] == "critical"
        assert filt["mitre_tactic"] == "lateral-movement"
        assert filt["mitre_technique_id"] == "T1021"
        assert filt["source_type"] == "sigma_rule"
        assert filt["cve_id"] == "CVE-2024-1234"
        assert filt["content_date_after"] == "2024-01-01"
        assert filt["platforms"] == ["windows", "linux"]

    @patch(f"{_QUERY}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.setup_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.build_query_agent")
    @patch(f"{_QUERY}.get_db_context")
    def test_no_security_flags_keeps_filter_dict_none(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock(answer="A.", citations=[], cached=False)
        mock_agent = MagicMock()
        mock_agent.query = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["ask", "plain question"])
        assert result.exit_code == 0, result.output
        assert mock_agent.query.call_args[1]["filter_dict"] is None


class TestIngestSourceTypeOverride:
    """Phase 9 — ``grimoire ingest --source-type`` override."""

    @patch(f"{_INGEST}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_INGEST}.setup_db", new_callable=AsyncMock)
    @patch(f"{_INGEST}.build_ingestion_agent")
    @patch(f"{_INGEST}.get_db_context")
    def test_source_type_passed_to_ingest_file(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
        tmp_path,
    ) -> None:
        test_file = tmp_path / "rule.yml"
        test_file.write_text("title: x\n")
        mock_result = MagicMock(
            status="completed", chunks_created=1, tags_applied=0, duration_ms=1
        )
        mock_agent = MagicMock()
        mock_agent.ingest_file = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(
            cli,
            ["ingest", str(test_file), "--source-type", "sigma_rule"],
        )
        assert result.exit_code == 0, result.output
        assert mock_agent.ingest_file.call_args[1]["source_type"] == "sigma_rule"

    @patch(f"{_INGEST}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_INGEST}.setup_db", new_callable=AsyncMock)
    @patch(f"{_INGEST}.build_ingestion_agent")
    @patch(f"{_INGEST}.get_db_context")
    def test_source_type_passed_to_ingest_directory(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
        tmp_path,
    ) -> None:
        mock_result = MagicMock(
            succeeded=0, total=0, skipped=0, failed=0, duration_ms=1, results=[]
        )
        mock_agent = MagicMock()
        mock_agent.ingest_directory = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(
            cli,
            ["ingest", str(tmp_path), "--source-type", "nvd_cve"],
        )
        assert result.exit_code == 0, result.output
        assert mock_agent.ingest_directory.call_args[1]["source_type"] == "nvd_cve"

    def test_invalid_source_type_rejected_by_click(
        self, runner: CliRunner, tmp_path
    ) -> None:
        """Click's Choice validator must reject unknown source_type values."""
        f = tmp_path / "a.txt"
        f.write_text("x")
        result = runner.invoke(
            cli, ["ingest", str(f), "--source-type", "totally_invalid"]
        )
        assert result.exit_code != 0
        assert (
            "Invalid value for '--source-type'" in result.output
            or "Invalid value" in result.output
        )


class TestSearchCommand:
    """Test the search command."""

    @patch(f"{_QUERY}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.setup_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.build_query_agent")
    @patch(f"{_QUERY}.get_db_context")
    def test_search_text_output(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock(
            total_results=2,
            duration_ms=42,
            results=[
                {
                    "document_title": "Doc A",
                    "score": 0.95,
                    "content": "Some content here",
                },
                {"document_title": "Doc B", "score": 0.85, "content": "Other content"},
            ],
        )
        mock_agent = MagicMock()
        mock_agent.search = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["search", "machine learning"])
        assert result.exit_code == 0
        assert "2 results" in result.output
        assert "Doc A" in result.output

    @patch(f"{_QUERY}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.setup_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.build_query_agent")
    @patch(f"{_QUERY}.get_db_context")
    def test_search_text_output_falls_back_on_null_title(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        """A present-but-None title must fall back to the short document id.

        `document_title` is always in the dict, so a `.get(key, default)`
        default never fires and the table would print the literal "None".
        """
        mock_result = MagicMock(
            total_results=1,
            duration_ms=42,
            results=[
                {
                    "document_title": None,
                    "document_id": "abcdef1234567890",
                    "score": 0.95,
                    "content": "Untitled content",
                },
            ],
        )
        mock_agent = MagicMock()
        mock_agent.search = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["search", "neural networks"])
        assert result.exit_code == 0
        assert "abcdef12" in result.output
        assert "None" not in result.output

    @patch(f"{_QUERY}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.setup_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.build_query_agent")
    @patch(f"{_QUERY}.get_db_context")
    def test_search_markdown_output_falls_back_on_null_title(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        """Same None-title fallback in the markdown table renderer."""
        mock_result = MagicMock(
            total_results=1,
            duration_ms=42,
            results=[
                {
                    "document_title": None,
                    "document_id": "abcdef1234567890",
                    "score": 0.95,
                    "content": "Untitled content",
                },
            ],
        )
        mock_agent = MagicMock()
        mock_agent.search = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(
            cli, ["search", "neural networks", "--format", "markdown"]
        )
        assert result.exit_code == 0
        assert "abcdef12" in result.output
        assert "None" not in result.output

    @patch(f"{_QUERY}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.setup_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.build_query_agent")
    @patch(f"{_QUERY}.get_db_context")
    def test_search_json_output(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "query": "test",
            "results": [],
            "total_results": 0,
        }
        mock_agent = MagicMock()
        mock_agent.search = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["search", "test", "--format", "json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "results" in parsed

    @patch(f"{_QUERY}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.setup_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.build_query_agent")
    @patch(f"{_QUERY}.get_db_context")
    def test_search_markdown_output(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock(
            total_results=2,
            duration_ms=42,
            results=[
                {
                    "document_title": "Doc A",
                    "score": 0.95,
                    "content": "Some content here",
                },
                {"document_title": "Doc B", "score": 0.85, "content": "Other content"},
            ],
        )
        mock_agent = MagicMock()
        mock_agent.search = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(
            cli, ["search", "machine learning", "--format", "markdown"]
        )
        assert result.exit_code == 0
        assert "| #" in result.output
        assert "|---" in result.output
        assert "Doc A" in result.output
        assert "Doc B" in result.output

    @patch(f"{_QUERY}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.setup_db", new_callable=AsyncMock)
    @patch(f"{_QUERY}.build_query_agent")
    @patch(f"{_QUERY}.get_db_context")
    def test_search_no_results(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock(total_results=0, duration_ms=5, results=[])
        mock_agent = MagicMock()
        mock_agent.search = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["search", "nonexistent topic"])
        assert result.exit_code == 0
        assert "No results" in result.output


# =============================================================================
# Generate Tests
# =============================================================================


class TestGenerateCommand:
    """Test content generation commands."""

    @patch(f"{_GEN}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.setup_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.build_content_gen_agent")
    @patch(f"{_GEN}.get_db_context")
    def test_generate_summary(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock(
            content="This is a summary.",
            cached=False,
            duration_ms=200,
            model_used="llama3",
        )
        mock_agent = MagicMock()
        mock_agent.generate_summary = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["generate", "summary", "-d", "abc123"])
        assert result.exit_code == 0
        assert "This is a summary." in result.output

    @patch(f"{_GEN}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.setup_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.build_content_gen_agent")
    @patch(f"{_GEN}.get_db_context")
    def test_generate_flashcards(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock(
            content="Q: What?\nA: That.", cached=False, duration_ms=100, model_used=""
        )
        mock_agent = MagicMock()
        mock_agent.generate_flash_cards = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(
            cli, ["generate", "flashcards", "-d", "abc123", "-n", "5"]
        )
        assert result.exit_code == 0
        assert "What?" in result.output

    @patch(f"{_GEN}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.setup_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.build_content_gen_agent")
    @patch(f"{_GEN}.get_db_context")
    def test_generate_cliff_notes(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock(
            content="Cliff notes content.", cached=True, duration_ms=0, model_used=""
        )
        mock_agent = MagicMock()
        mock_agent.generate_cliff_notes = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["generate", "cliff-notes", "-d", "id1"])
        assert result.exit_code == 0
        assert "Cliff notes" in result.output
        assert "cached" in result.output

    @patch(f"{_GEN}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.setup_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.build_content_gen_agent")
    @patch(f"{_GEN}.get_db_context")
    def test_generate_outline(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock(
            content="1. Intro\n2. Methods", cached=False, duration_ms=50, model_used=""
        )
        mock_agent = MagicMock()
        mock_agent.generate_outline = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["generate", "outline", "-d", "id1"])
        assert result.exit_code == 0
        assert "Intro" in result.output

    @patch(f"{_GEN}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.setup_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.build_content_gen_agent")
    @patch(f"{_GEN}.get_db_context")
    def test_generate_json_output(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "content": "test output",
            "cached": False,
        }
        mock_agent = MagicMock()
        mock_agent.generate_summary = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(
            cli, ["generate", "summary", "-d", "id1", "--format", "json"]
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["content"] == "test output"


# =============================================================================
# Config Tests
# =============================================================================


class TestConfigCommand:
    """Test configuration commands."""

    def test_config_init_creates_file(self, runner: CliRunner, tmp_path: Path) -> None:
        outfile = tmp_path / "grimoire.yaml"
        result = runner.invoke(cli, ["config", "init", "-o", str(outfile)])
        assert result.exit_code == 0
        assert outfile.exists()
        import yaml

        data = yaml.safe_load(outfile.read_text())
        assert "llm" in data
        assert "database" in data

    def test_config_init_overwrite_decline(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        outfile = tmp_path / "grimoire.yaml"
        outfile.write_text("existing")
        result = runner.invoke(cli, ["config", "init", "-o", str(outfile)], input="n\n")
        assert result.exit_code == 0
        assert outfile.read_text() == "existing"

    @patch("grimoire.cli.config.get_settings")
    def test_config_show(self, mock_settings: MagicMock, runner: CliRunner) -> None:
        mock_settings.return_value.model_dump.return_value = {
            "llm": {"model": "llama3.2"},
            "database": {"url": "sqlite:///test.db"},
        }
        result = runner.invoke(cli, ["config", "show"])
        assert result.exit_code == 0
        assert "llama3.2" in result.output

    @patch("grimoire.cli.config.get_settings")
    def test_config_show_section(
        self, mock_settings: MagicMock, runner: CliRunner
    ) -> None:
        mock_settings.return_value.model_dump.return_value = {
            "llm": {"model": "llama3.2"},
            "database": {"url": "sqlite:///test.db"},
        }
        result = runner.invoke(cli, ["config", "show", "-s", "llm"])
        assert result.exit_code == 0
        assert "llama3.2" in result.output

    @patch("grimoire.cli.config.get_settings")
    def test_config_show_invalid_section(
        self, mock_settings: MagicMock, runner: CliRunner
    ) -> None:
        mock_settings.return_value.model_dump.return_value = {"llm": {}}
        result = runner.invoke(cli, ["config", "show", "-s", "nonexistent"])
        assert result.exit_code == 0
        assert "Unknown section" in result.output


# =============================================================================
# Status Tests
# =============================================================================


class TestStatusCommand:
    """Test status and cache commands."""

    @patch(f"{_STATUS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.get_db_context")
    def test_status_basic(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        # execute() is async, returns an object whose .scalar() is sync
        mock_exec_result = MagicMock()
        mock_exec_result.scalar.return_value = 42
        mock_session.execute = AsyncMock(return_value=mock_exec_result)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "Documents" in result.output
        assert "Vector store" in result.output

    @patch("grimoire.vectorstore.chromadb.ChromaDBStore")
    @patch(f"{_STATUS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.get_db_context")
    def test_status_detailed_vector_store_in_sync(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        mock_store_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec_result = MagicMock()
        mock_exec_result.scalar.return_value = 10
        mock_session.execute = AsyncMock(return_value=mock_exec_result)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        mock_store = AsyncMock()
        mock_store.count = AsyncMock(return_value=10)
        mock_store_cls.return_value = mock_store

        result = runner.invoke(cli, ["status", "--detailed"])
        assert result.exit_code == 0
        assert "10 embeddings" in result.output
        assert "WARNING" not in result.output

    @patch("grimoire.vectorstore.chromadb.ChromaDBStore")
    @patch(f"{_STATUS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.get_db_context")
    def test_status_detailed_vector_store_drift_warns(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        mock_store_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec_result = MagicMock()
        mock_exec_result.scalar.return_value = 10
        mock_session.execute = AsyncMock(return_value=mock_exec_result)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        mock_store = AsyncMock()
        mock_store.count = AsyncMock(return_value=999)
        mock_store_cls.return_value = mock_store

        result = runner.invoke(cli, ["status", "--detailed"])
        assert result.exit_code == 0
        assert "999 embeddings" in result.output
        assert "WARNING" in result.output
        assert "out of sync" in result.output

    @patch("grimoire.vectorstore.chromadb.ChromaDBStore")
    @patch(f"{_STATUS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.get_db_context")
    def test_status_detailed_vector_store_unreachable(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        mock_store_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec_result = MagicMock()
        mock_exec_result.scalar.return_value = 10
        mock_session.execute = AsyncMock(return_value=mock_exec_result)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock(
            side_effect=ConnectionError("refused")
        )
        mock_store_cls.return_value = mock_store

        result = runner.invoke(cli, ["status", "--detailed"])
        assert result.exit_code == 0
        assert "Vector store: unreachable" in result.output

    @patch("grimoire.vectorstore.chromadb.ChromaDBStore")
    @patch(f"{_STATUS}.CacheFactory")
    @patch(f"{_STATUS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.get_db_context")
    def test_status_detailed_redis_cache(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        mock_factory: MagicMock,
        mock_store_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec_result = MagicMock()
        mock_exec_result.scalar.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_exec_result)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        mock_store = AsyncMock()
        mock_store.count = AsyncMock(return_value=0)
        mock_store_cls.return_value = mock_store

        mock_cache = AsyncMock()
        mock_cache.client.info = AsyncMock(
            return_value={"redis_version": "7.2.0", "connected_clients": 3}
        )
        mock_factory.create.return_value = mock_cache

        result = runner.invoke(cli, ["status", "--detailed"])
        assert result.exit_code == 0
        assert "Cache (redis)" in result.output
        assert "7.2.0" in result.output
        assert "3" in result.output

    @patch("grimoire.vectorstore.chromadb.ChromaDBStore")
    @patch(f"{_STATUS}.CacheFactory")
    @patch(f"{_STATUS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.get_db_context")
    def test_status_detailed_redis_cache_error_is_swallowed(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        mock_factory: MagicMock,
        mock_store_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec_result = MagicMock()
        mock_exec_result.scalar.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_exec_result)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        mock_store = AsyncMock()
        mock_store.count = AsyncMock(return_value=0)
        mock_store_cls.return_value = mock_store

        mock_cache = AsyncMock()
        mock_cache.client.info = AsyncMock(side_effect=ConnectionError("refused"))
        mock_factory.create.return_value = mock_cache

        result = runner.invoke(cli, ["status", "--detailed"])
        assert result.exit_code == 0

    @patch("grimoire.vectorstore.chromadb.ChromaDBStore")
    @patch(f"{_STATUS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.get_db_context")
    def test_status_detailed_ollama_reachable(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        mock_store_cls: MagicMock,
        runner: CliRunner,
        httpx_mock: HTTPXMock,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec_result = MagicMock()
        mock_exec_result.scalar.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_exec_result)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        mock_store = AsyncMock()
        mock_store.count = AsyncMock(return_value=0)
        mock_store_cls.return_value = mock_store

        from grimoire.config.settings import get_settings

        llm_url = get_settings().llm.url
        httpx_mock.add_response(
            url=f"{llm_url}/api/tags",
            json={"models": []},
        )

        result = runner.invoke(cli, ["status", "--detailed"])
        assert result.exit_code == 0
        assert "Ollama" in result.output
        assert f"reachable at {llm_url}" in result.output

    @patch("grimoire.vectorstore.chromadb.ChromaDBStore")
    @patch(f"{_STATUS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.get_db_context")
    def test_status_detailed_ollama_unreachable(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        mock_store_cls: MagicMock,
        runner: CliRunner,
        httpx_mock: HTTPXMock,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec_result = MagicMock()
        mock_exec_result.scalar.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_exec_result)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        mock_store = AsyncMock()
        mock_store.count = AsyncMock(return_value=0)
        mock_store_cls.return_value = mock_store

        from grimoire.config.settings import get_settings

        llm_url = get_settings().llm.url
        httpx_mock.add_exception(httpx.ConnectError("Connection refused"))

        result = runner.invoke(cli, ["status", "--detailed"])
        assert result.exit_code == 0
        assert "Ollama" in result.output
        assert f"unreachable at {llm_url}" in result.output


def _reindex_settings(vs_type: VectorStoreType = VectorStoreType.CHROMADB) -> Any:
    return SimpleNamespace(
        vector_store=SimpleNamespace(
            type=vs_type,
            host=None,
            port=None,
            chromadb=SimpleNamespace(path="./chroma_db", collection_name="documents"),
            qdrant=SimpleNamespace(url="http://localhost:6333"),
        ),
        embeddings=SimpleNamespace(
            model="sentence-transformers/all-mpnet-base-v2",
            fallback_model="sentence-transformers/all-MiniLM-L6-v2",
            device="cpu",
            batch_size=32,
        ),
        cache=SimpleNamespace(storage="disk", path="/tmp/grimoire-test-cache"),
    )


def _make_chunk(
    chunk_id: str, document_id: str = "doc-1", chunk_index: int = 0
) -> MagicMock:
    chunk = MagicMock()
    chunk.id = chunk_id
    chunk.document_id = document_id
    chunk.chunk_index = chunk_index
    chunk.content = f"content for {chunk_id}"
    chunk.token_count = 5
    return chunk


class TestReindexCommand:
    """Test the reindex command that repairs chunk/embedding drift."""

    @patch(f"{_STATUS}.get_settings")
    def test_reindex_rejects_qdrant(
        self, mock_settings: MagicMock, runner: CliRunner
    ) -> None:
        mock_settings.return_value = _reindex_settings(VectorStoreType.QDRANT)

        result = runner.invoke(cli, ["reindex"])
        assert result.exit_code != 0
        assert "only supports the chromadb backend" in result.output

    @patch("grimoire.vectorstore.chromadb.ChromaDBStore")
    @patch(f"{_STATUS}.get_settings")
    @patch(f"{_STATUS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.get_db_context")
    def test_reindex_no_chunks(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        mock_settings: MagicMock,
        mock_store_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_settings.return_value = _reindex_settings()

        mock_session = AsyncMock()
        mock_exec = MagicMock()
        mock_exec.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_exec)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        mock_store_cls.return_value = AsyncMock()

        result = runner.invoke(cli, ["reindex"])
        assert result.exit_code == 0
        assert "nothing to reindex" in result.output

    @patch("grimoire.vectorstore.chromadb.ChromaDBStore")
    @patch(f"{_STATUS}.get_settings")
    @patch(f"{_STATUS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.get_db_context")
    def test_reindex_all_present(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        mock_settings: MagicMock,
        mock_store_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_settings.return_value = _reindex_settings()

        chunks = [_make_chunk("c1"), _make_chunk("c2")]
        mock_session = AsyncMock()
        mock_exec = MagicMock()
        mock_exec.scalars.return_value.all.return_value = chunks
        mock_session.execute = AsyncMock(return_value=mock_exec)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        mock_store = AsyncMock()
        mock_store.get = AsyncMock(
            return_value=[{"id": "c1"}, {"id": "c2"}]
        )
        mock_store_cls.return_value = mock_store

        result = runner.invoke(cli, ["reindex"])
        assert result.exit_code == 0
        assert "All 2 chunks are present" in result.output

    @patch("grimoire.vectorstore.chromadb.ChromaDBStore")
    @patch(f"{_STATUS}.get_settings")
    @patch(f"{_STATUS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.get_db_context")
    def test_reindex_dry_run_lists_missing_without_writing(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        mock_settings: MagicMock,
        mock_store_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_settings.return_value = _reindex_settings()

        chunks = [_make_chunk("c1"), _make_chunk("c2")]
        mock_session = AsyncMock()
        mock_exec = MagicMock()
        mock_exec.scalars.return_value.all.return_value = chunks
        mock_session.execute = AsyncMock(return_value=mock_exec)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        mock_store = AsyncMock()
        mock_store.get = AsyncMock(return_value=[])  # nothing found -> both missing
        mock_store_cls.return_value = mock_store

        result = runner.invoke(cli, ["reindex", "--dry-run"])
        assert result.exit_code == 0
        assert "2 of 2 chunks are missing" in result.output
        assert "c1" in result.output
        assert "c2" in result.output
        mock_store.add_documents.assert_not_called()

    @patch("grimoire.vectorstore.chromadb.ChromaDBStore")
    @patch(f"{_STATUS}.get_settings")
    @patch(f"{_STATUS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.get_db_context")
    def test_reindex_declined_confirmation_does_not_write(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        mock_settings: MagicMock,
        mock_store_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_settings.return_value = _reindex_settings()

        chunks = [_make_chunk("c1")]
        mock_session = AsyncMock()
        mock_exec = MagicMock()
        mock_exec.scalars.return_value.all.return_value = chunks
        mock_session.execute = AsyncMock(return_value=mock_exec)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        mock_store = AsyncMock()
        mock_store.get = AsyncMock(return_value=[])
        mock_store_cls.return_value = mock_store

        result = runner.invoke(cli, ["reindex"], input="n\n")
        assert result.exit_code == 0
        mock_store.add_documents.assert_not_called()

    @patch("grimoire.core.embedder.Embedder")
    @patch("grimoire.core.cache.CacheFactory")
    @patch("grimoire.vectorstore.chromadb.ChromaDBStore")
    @patch(f"{_STATUS}.get_settings")
    @patch(f"{_STATUS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_STATUS}.get_db_context")
    def test_reindex_repairs_missing_chunks(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        mock_settings: MagicMock,
        mock_store_cls: MagicMock,
        mock_cache_factory: MagicMock,
        mock_embedder_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_settings.return_value = _reindex_settings()

        chunks = [_make_chunk("c1"), _make_chunk("c2")]
        mock_session = AsyncMock()
        mock_exec = MagicMock()
        mock_exec.scalars.return_value.all.return_value = chunks
        mock_session.execute = AsyncMock(return_value=mock_exec)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        mock_store = AsyncMock()
        mock_store.get = AsyncMock(return_value=[])  # both missing
        mock_store_cls.return_value = mock_store

        mock_embedder = AsyncMock()
        mock_embedder.embed = AsyncMock(return_value=[[0.1, 0.2], [0.3, 0.4]])
        mock_embedder_cls.return_value = mock_embedder

        result = runner.invoke(cli, ["reindex", "--no-confirm"])
        assert result.exit_code == 0
        assert "Re-embedded 2 chunk(s)" in result.output

        mock_store.add_documents.assert_called_once()
        call_kwargs = mock_store.add_documents.call_args.kwargs
        assert set(call_kwargs["ids"]) == {"c1", "c2"}
        assert call_kwargs["embeddings"] == [[0.1, 0.2], [0.3, 0.4]]


class TestVectorStoreSummary:
    """Test the _vector_store_summary status helper."""

    def _settings(
        self,
        vs_type: VectorStoreType,
        host: str | None,
        port: int | None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            vector_store=SimpleNamespace(
                type=vs_type,
                host=host,
                port=port,
                chromadb=SimpleNamespace(path="./chroma_db"),
                qdrant=SimpleNamespace(url="http://localhost:6333"),
            )
        )

    def test_embedded_chromadb(self) -> None:
        settings = self._settings(VectorStoreType.CHROMADB, host=None, port=None)
        assert (
            _vector_store_summary(settings)
            == "chromadb (embedded, path=./chroma_db)"
        )

    def test_remote_chromadb(self) -> None:
        settings = self._settings(
            VectorStoreType.CHROMADB, host="chromadb", port=8000
        )
        assert _vector_store_summary(settings) == "chromadb (remote chromadb:8000)"

    def test_remote_chromadb_default_port(self) -> None:
        settings = self._settings(VectorStoreType.CHROMADB, host="chromadb", port=None)
        assert _vector_store_summary(settings) == "chromadb (remote chromadb:8000)"

    def test_qdrant(self) -> None:
        settings = self._settings(VectorStoreType.QDRANT, host=None, port=None)
        assert _vector_store_summary(settings) == "qdrant (http://localhost:6333)"

    @patch("grimoire.cli.status.CacheFactory")
    @patch("grimoire.cli.status.get_settings")
    def test_cache_stats(
        self,
        mock_settings: MagicMock,
        mock_factory: MagicMock,
        runner: CliRunner,
    ) -> None:
        from grimoire.core.cache import DiskCache

        mock_cache = MagicMock(spec=DiskCache)
        mock_cache.get_stats.return_value = {"size": 100, "volume": 1024}
        mock_factory.create.return_value = mock_cache
        mock_settings.return_value.cache.storage = "disk"
        mock_settings.return_value.cache.path = "/tmp/cache"

        result = runner.invoke(cli, ["cache", "stats"])
        assert result.exit_code == 0
        assert "100" in result.output

    @patch("grimoire.cli.status.CacheFactory")
    @patch("grimoire.cli.status.get_settings")
    def test_cache_clear(
        self,
        mock_settings: MagicMock,
        mock_factory: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_cache = AsyncMock()
        mock_factory.create.return_value = mock_cache
        mock_settings.return_value.cache.storage = "disk"
        mock_settings.return_value.cache.path = "/tmp/cache"

        result = runner.invoke(cli, ["cache", "clear", "--no-confirm"])
        assert result.exit_code == 0
        assert "cleared" in result.output.lower()


# =============================================================================
# Docs List Tests
# =============================================================================


def _make_mock_doc(
    doc_id: str = "abcd1234-5678-9abc-def0-123456789abc",
    title: str = "Test Document",
    file_type: str = "pdf",
    status: str = "completed",
    created_at: datetime | None = None,
) -> MagicMock:
    """Create a mock Document object."""
    doc = MagicMock()
    doc.id = doc_id
    doc.title = title
    doc.file_type = MagicMock(value=file_type)
    doc.processing_status = MagicMock(value=status)
    doc.created_at = created_at or datetime(2026, 4, 3, 12, 0, 0)
    return doc


class TestDocsListCommand:
    """Test the docs list command."""

    @patch(f"{_DOCS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.get_db_context")
    def test_docs_list_all(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec = MagicMock()
        mock_exec.scalars.return_value.all.return_value = [
            _make_mock_doc(title="Doc A"),
            _make_mock_doc(
                doc_id="bbbb2222-0000-0000-0000-000000000000", title="Doc B"
            ),
        ]
        mock_session.execute = AsyncMock(return_value=mock_exec)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        result = runner.invoke(cli, ["docs", "list"])
        assert result.exit_code == 0
        assert "Doc A" in result.output
        assert "Doc B" in result.output
        assert "2 document(s) found" in result.output

    @patch(f"{_DOCS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.get_db_context")
    def test_docs_list_by_category(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec = MagicMock()
        mock_exec.scalars.return_value.all.return_value = [
            _make_mock_doc(title="ML Paper"),
        ]
        mock_session.execute = AsyncMock(return_value=mock_exec)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        result = runner.invoke(cli, ["docs", "list", "--category", "Research"])
        assert result.exit_code == 0
        assert "ML Paper" in result.output

    @patch(f"{_DOCS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.get_db_context")
    def test_docs_list_by_search(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec = MagicMock()
        mock_exec.scalars.return_value.all.return_value = [
            _make_mock_doc(title="LLM Quantization Guide"),
        ]
        mock_session.execute = AsyncMock(return_value=mock_exec)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        result = runner.invoke(cli, ["docs", "list", "--search", "quantization"])
        assert result.exit_code == 0
        assert "Quantization" in result.output

    @patch(f"{_DOCS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.get_db_context")
    def test_docs_list_by_since_relative(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec = MagicMock()
        mock_exec.scalars.return_value.all.return_value = [
            _make_mock_doc(title="Recent Doc"),
        ]
        mock_session.execute = AsyncMock(return_value=mock_exec)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        result = runner.invoke(cli, ["docs", "list", "--since", "7d"])
        assert result.exit_code == 0
        assert "Recent Doc" in result.output

    @patch(f"{_DOCS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.get_db_context")
    def test_docs_list_by_since_absolute(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec = MagicMock()
        mock_exec.scalars.return_value.all.return_value = [
            _make_mock_doc(title="March Doc"),
        ]
        mock_session.execute = AsyncMock(return_value=mock_exec)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        result = runner.invoke(cli, ["docs", "list", "--since", "2026-03-01"])
        assert result.exit_code == 0
        assert "March Doc" in result.output

    @patch(f"{_DOCS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.get_db_context")
    def test_docs_list_combined_filters(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec = MagicMock()
        mock_exec.scalars.return_value.all.return_value = [
            _make_mock_doc(title="Filtered Doc"),
        ]
        mock_session.execute = AsyncMock(return_value=mock_exec)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        result = runner.invoke(
            cli,
            [
                "docs",
                "list",
                "--category",
                "AI",
                "--search",
                "deep",
                "--since",
                "7d",
            ],
        )
        assert result.exit_code == 0
        assert "Filtered Doc" in result.output

    @patch(f"{_DOCS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.get_db_context")
    def test_docs_list_json_output(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        # First call returns documents, second call returns categories per doc
        doc = _make_mock_doc(title="JSON Doc")
        mock_exec_docs = MagicMock()
        mock_exec_docs.scalars.return_value.all.return_value = [doc]

        mock_exec_cats = MagicMock()
        mock_exec_cats.scalars.return_value.all.return_value = ["Research", "AI"]

        mock_session.execute = AsyncMock(side_effect=[mock_exec_docs, mock_exec_cats])

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        result = runner.invoke(cli, ["docs", "list", "--format", "json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert len(parsed) == 1
        assert parsed[0]["title"] == "JSON Doc"
        assert "Research" in parsed[0]["categories"]

    @patch(f"{_DOCS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.get_db_context")
    def test_docs_list_markdown_output(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec = MagicMock()
        mock_exec.scalars.return_value.all.return_value = [
            _make_mock_doc(title="MD Doc"),
        ]
        mock_session.execute = AsyncMock(return_value=mock_exec)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        result = runner.invoke(cli, ["docs", "list", "--format", "markdown"])
        assert result.exit_code == 0
        assert "| ID" in result.output
        assert "|---" in result.output
        assert "MD Doc" in result.output
        assert "1 document(s) found" in result.output

    @patch(f"{_DOCS}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.setup_db", new_callable=AsyncMock)
    @patch(f"{_DOCS}.get_db_context")
    def test_docs_list_empty(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec = MagicMock()
        mock_exec.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_exec)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        result = runner.invoke(cli, ["docs", "list"])
        assert result.exit_code == 0
        assert "No documents found" in result.output

    def test_docs_list_bad_since(self, runner: CliRunner) -> None:
        """Invalid --since value should produce a clean error."""
        # _parse_since runs before DB setup, so no mocking needed for the bad-param path
        # However, setup_db is called first, so we need to mock it
        with (
            patch(f"{_DOCS}.setup_db", new_callable=AsyncMock),
            patch(f"{_DOCS}.teardown_db", new_callable=AsyncMock),
            patch(f"{_DOCS}.get_db_context") as mock_ctx,
        ):
            mock_session = AsyncMock()
            mock_exec = MagicMock()
            mock_exec.scalars.return_value.all.return_value = []
            mock_session.execute = AsyncMock(return_value=mock_exec)

            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=mock_session)
            ctx.__aexit__ = AsyncMock(return_value=False)
            mock_ctx.return_value = ctx

            result = runner.invoke(cli, ["docs", "list", "--since", "not-a-date"])
            assert result.exit_code != 0
            assert "Invalid date" in result.output


# =============================================================================
# Generate with Category Tests
# =============================================================================


class TestGenerateWithCategory:
    """Test generate commands with --category flag."""

    @patch(f"{_GEN}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.setup_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.build_content_gen_agent")
    @patch(f"{_GEN}.get_db_context")
    def test_generate_summary_with_category(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        # _resolve_doc_ids query returns document IDs
        mock_exec_ids = MagicMock()
        mock_exec_ids.scalars.return_value.all.return_value = ["id1", "id2"]
        mock_session.execute = AsyncMock(return_value=mock_exec_ids)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        mock_result = MagicMock(
            content="Category summary.",
            cached=False,
            duration_ms=100,
            model_used="llama3",
        )
        mock_agent = MagicMock()
        mock_agent.generate_summary = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent

        result = runner.invoke(
            cli, ["generate", "summary", "--category", "machine-learning"]
        )
        assert result.exit_code == 0
        assert "Category summary." in result.output

    @patch(f"{_GEN}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.setup_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.build_content_gen_agent")
    @patch(f"{_GEN}.get_db_context")
    def test_generate_flashcards_with_category(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec_ids = MagicMock()
        mock_exec_ids.scalars.return_value.all.return_value = ["id1"]
        mock_session.execute = AsyncMock(return_value=mock_exec_ids)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        mock_result = MagicMock(
            content="Q: What?\nA: That.", cached=False, duration_ms=50, model_used=""
        )
        mock_agent = MagicMock()
        mock_agent.generate_flash_cards = AsyncMock(return_value=mock_result)
        mock_build.return_value = mock_agent

        result = runner.invoke(cli, ["generate", "flashcards", "--category", "AI"])
        assert result.exit_code == 0
        assert "What?" in result.output

    @patch(f"{_GEN}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.setup_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.build_content_gen_agent")
    @patch(f"{_GEN}.get_db_context")
    def test_generate_both_flags_error(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(
            cli,
            [
                "generate",
                "summary",
                "-d",
                "abc123",
                "--category",
                "AI",
            ],
        )
        assert result.exit_code != 0
        assert "not both" in result.output

    @patch(f"{_GEN}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.setup_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.build_content_gen_agent")
    @patch(f"{_GEN}.get_db_context")
    def test_generate_neither_flag_error(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_ctx.return_value = _mock_db_ctx()

        result = runner.invoke(cli, ["generate", "summary"])
        assert result.exit_code != 0
        assert "Provide --doc-id or --category" in result.output

    @patch(f"{_GEN}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.setup_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.build_content_gen_agent")
    @patch(f"{_GEN}.get_db_context")
    def test_generate_category_no_docs(
        self,
        mock_ctx: MagicMock,
        mock_build: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_session = AsyncMock()
        mock_exec_ids = MagicMock()
        mock_exec_ids.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_exec_ids)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.return_value = ctx

        result = runner.invoke(
            cli, ["generate", "summary", "--category", "empty-category"]
        )
        assert result.exit_code == 0
        assert "No documents found in category" in result.output


# =============================================================================
# Edge Cases
# =============================================================================


class TestCLIEdgeCases:
    """Edge cases and error handling."""

    def test_ingest_nonexistent_path(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["ingest", "/nonexistent/path"])
        assert result.exit_code != 0

    def test_ask_missing_argument(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["ask"])
        assert result.exit_code != 0

    @patch(f"{_GEN}.teardown_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.setup_db", new_callable=AsyncMock)
    @patch(f"{_GEN}.get_db_context")
    def test_generate_no_doc_id(
        self,
        mock_ctx: MagicMock,
        mock_setup: AsyncMock,
        mock_teardown: AsyncMock,
        runner: CliRunner,
    ) -> None:
        mock_ctx.return_value = _mock_db_ctx()
        result = runner.invoke(cli, ["generate", "summary"])
        assert result.exit_code != 0
        assert "Provide --doc-id or --category" in result.output

    def test_search_missing_argument(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["search"])
        assert result.exit_code != 0


# =============================================================================
# Startup Configuration Validation
# =============================================================================


@pytest.fixture
def clean_settings_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the cached settings singleton for the duration of a test.

    ``monkeypatch.setattr`` restores the previous instance afterwards, so a
    deliberately broken config cannot leak into other tests.
    """
    import importlib

    # The re-exported `settings` proxy shadows the submodule attribute on the
    # package, so the module must be resolved via import_module.
    settings_module = importlib.import_module("grimoire.config.settings")
    monkeypatch.setattr(settings_module, "_settings", None)


class TestCLIConfigValidation:
    """The CLI must fail gracefully on a broken grimoire.yaml."""

    @staticmethod
    def _write_broken_config(directory: Path) -> Path:
        config_file = directory / "grimoire.yaml"
        config_file.write_text('grimoire:\n  llm:\n    model: "unterminated\n')
        return config_file

    def test_broken_config_exits_with_code_1(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        clean_settings_cache: None,
        tmp_path: Path,
    ) -> None:
        """A malformed config aborts before the subcommand runs, no traceback."""
        config_file = self._write_broken_config(tmp_path)
        monkeypatch.setenv("GRIMOIRE_CONFIG", str(config_file))

        result = runner.invoke(cli, ["status", "--help"])

        assert result.exit_code == 1
        assert "not valid YAML" in result.output
        assert str(config_file) in result.output
        # Never surface a raw traceback to the user.
        assert "Traceback" not in result.output

    def test_invalid_field_value_exits_with_code_1(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        clean_settings_cache: None,
        tmp_path: Path,
    ) -> None:
        """A field that fails validation produces a readable per-field report."""
        monkeypatch.setenv("GRIMOIRE_CONFIG", str(tmp_path / "absent.yaml"))
        monkeypatch.setenv("GRIMOIRE_REDIS__PORT", "99999")

        result = runner.invoke(cli, ["status", "--help"])

        assert result.exit_code == 1
        assert "redis.port" in result.output
        assert "Traceback" not in result.output

    def test_missing_config_file_is_not_an_error(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        clean_settings_cache: None,
        tmp_path: Path,
    ) -> None:
        """No grimoire.yaml at all simply means "use defaults"."""
        monkeypatch.setenv("GRIMOIRE_CONFIG", str(tmp_path / "absent.yaml"))

        result = runner.invoke(cli, ["status", "--help"])

        assert result.exit_code == 0

    def test_group_help_works_with_broken_config(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        clean_settings_cache: None,
        tmp_path: Path,
    ) -> None:
        """`grimoire --help` must stay usable when the config is broken."""
        monkeypatch.setenv("GRIMOIRE_CONFIG", str(self._write_broken_config(tmp_path)))

        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "Usage:" in result.output

    def test_config_group_is_exempt_from_eager_validation(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        clean_settings_cache: None,
        tmp_path: Path,
    ) -> None:
        """`config init` must still work — it is how a broken file gets fixed."""
        monkeypatch.setenv("GRIMOIRE_CONFIG", str(self._write_broken_config(tmp_path)))
        output = tmp_path / "fresh.yaml"

        result = runner.invoke(cli, ["config", "init", "-o", str(output)])

        assert result.exit_code == 0
        assert output.exists()
