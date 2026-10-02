"""Tests for the TUI's file-only logging.

The whole point of this module is that nothing may reach the terminal while a
full-screen UI owns it, so most tests assert on what did *not* get printed.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from grimoire.tui.logsetup import LOG_FILE_NAME, configure_tui_logging


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Put the process-wide logging state back after each test."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_httpx = logging.getLogger("httpx").level
    yield
    logger.remove()
    logger.add(sys.stderr)
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    logging.getLogger("httpx").setLevel(saved_httpx)


def _read(path: Path) -> str:
    logger.complete()  # enqueue=True writes from a background thread
    return path.read_text()


class TestFileSink:
    def test_returns_the_log_file_path(self, tmp_path: Path) -> None:
        path = configure_tui_logging("sess123", log_dir=tmp_path)

        assert path == tmp_path / LOG_FILE_NAME

    def test_creates_missing_directories(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b"

        path = configure_tui_logging("sess123", log_dir=target)

        assert path is not None and target.is_dir()

    def test_loguru_records_land_in_the_file_with_the_session_id(
        self, tmp_path: Path
    ) -> None:
        path = configure_tui_logging("sess123", log_dir=tmp_path)
        assert path is not None

        logger.info("hello from the tui")

        text = _read(path)
        assert "hello from the tui" in text
        assert "sess123" in text

    def test_stdlib_logging_is_routed_into_the_file(self, tmp_path: Path) -> None:
        """httpx and anyio log through stdlib; their warnings must not hit stderr."""
        path = configure_tui_logging("sess123", log_dir=tmp_path)
        assert path is not None

        logging.getLogger("httpx").warning("stdlib warning text")

        assert "stdlib warning text" in _read(path)

    def test_nothing_reaches_the_terminal(self, tmp_path: Path, capfd) -> None:
        configure_tui_logging("sess123", log_dir=tmp_path)

        logger.info("loguru line")
        logger.error("loguru error")
        logging.getLogger("httpx").warning("stdlib line")
        logging.getLogger("some.lib").error("stdlib error")
        logger.complete()

        captured = capfd.readouterr()
        assert captured.err == ""
        assert captured.out == ""


class TestLevels:
    def test_debug_is_dropped_by_default(self, tmp_path: Path) -> None:
        path = configure_tui_logging("sess123", log_dir=tmp_path)
        assert path is not None

        logger.debug("noisy detail")
        logger.info("kept")

        text = _read(path)
        assert "noisy detail" not in text
        assert "kept" in text

    def test_debug_flag_keeps_debug_records(self, tmp_path: Path) -> None:
        path = configure_tui_logging("sess123", debug=True, log_dir=tmp_path)
        assert path is not None

        logger.debug("noisy detail")

        assert "noisy detail" in _read(path)

    def test_httpx_request_chatter_is_quiet_unless_debugging(
        self, tmp_path: Path
    ) -> None:
        path = configure_tui_logging("sess123", log_dir=tmp_path)
        assert path is not None

        logging.getLogger("httpx").info("HTTP Request: POST /query/ask")

        assert "HTTP Request" not in _read(path)


class TestUnwritableDirectories:
    def test_falls_back_when_the_default_directory_is_unusable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An installed tool can be launched from anywhere, including read-only
        directories.  A file named ``logs`` makes ``mkdir`` fail even as root."""
        (tmp_path / "logs").write_text("not a directory")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        path = configure_tui_logging("sess123")

        assert path is not None
        assert (
            path.parent == tmp_path / "home" / ".local" / "state" / "grimoire" / "logs"
        )
        logger.info("fallback works")
        assert "fallback works" in _read(path)

    def test_returns_none_and_stays_silent_when_nothing_is_writable(
        self, tmp_path: Path, capfd
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x")

        path = configure_tui_logging("sess123", log_dir=blocker / "logs")
        logger.error("nowhere to go")
        logger.complete()

        assert path is None
        assert capfd.readouterr().err == ""


class TestIdempotence:
    def test_reconfiguring_does_not_duplicate_lines(self, tmp_path: Path) -> None:
        configure_tui_logging("first", log_dir=tmp_path)
        path = configure_tui_logging("second", log_dir=tmp_path)
        assert path is not None

        logger.info("only once")

        text = _read(path)
        assert text.count("only once") == 1
        assert "second" in text
