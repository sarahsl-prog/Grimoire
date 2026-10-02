"""File-only logging for the terminal UI.

A full-screen UI owns the terminal, so any log line written to stderr paints
over it.  Two things would do that if left alone:

* loguru installs a stderr sink at import time.
* Libraries such as httpx log through the stdlib ``logging`` module, whose
  last-resort handler also writes to stderr.

This module removes the first, routes the second into loguru, and sends
everything to a rotating file.  It deliberately does not import
``grimoire.utils.logger``: that module calls ``setup_logger()`` at import,
which re-adds the stderr sink.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from types import FrameType

from loguru import logger

LOG_FILE_NAME = "grimoire-tui.log"

# ``extra[session_id]`` must be in the format: loguru's default format and the
# project's DEFAULT_LOG_FORMAT both omit ``extra``, so binding a session id
# alone would never show up in the file.
LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level:<8} | "
    "{extra[session_id]} | "
    "{name}:{function}:{line} | "
    "{message}"
)


class _InterceptHandler(logging.Handler):
    """Forward stdlib ``logging`` records to loguru.

    This is the recipe from loguru's documentation: it maps the level and
    walks past the logging module's own frames so the reported caller is the
    library code, not this handler.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame: FrameType | None = logging.currentframe()
        depth = 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def _candidate_directories() -> list[Path]:
    """Where to try writing, in order.

    ``./logs`` matches the CLI's default.  An installed tool can be launched
    from a read-only directory, so fall back to the per-user state directory
    rather than failing to start.
    """
    candidates = [Path("logs")]
    with contextlib.suppress(RuntimeError):  # no resolvable home directory
        candidates.append(Path.home() / ".local" / "state" / "grimoire" / "logs")
    return candidates


def configure_tui_logging(
    session_id: str,
    *,
    debug: bool = False,
    log_dir: Path | None = None,
) -> Path | None:
    """Send all logging to a file and silence the terminal.

    Safe to call more than once: it removes every existing sink first, so
    reconfiguring never duplicates lines.

    Args:
        session_id: Bound into every record via ``extra``.
        debug: Keep DEBUG records and httpx request logging.  Off by default.
        log_dir: Directory for ``grimoire-tui.log``.  When omitted, ``./logs``
            is tried first, then ``~/.local/state/grimoire/logs``.

    Returns:
        The absolute log file path, or None when no directory was writable.  In that
        case logging is simply off: a UI that cannot log is better than one
        that cannot start, and nothing is ever written to the terminal.
    """
    logger.remove()
    logger.configure(extra={"session_id": session_id})

    # Level 0 hands every stdlib record to loguru; the file sink's level does
    # the filtering, so there is one place that decides what is kept.
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    # httpx logs one INFO line per request, which is noise outside debugging.
    logging.getLogger("httpx").setLevel(logging.NOTSET if debug else logging.WARNING)

    level = "DEBUG" if debug else "INFO"
    directories = [log_dir] if log_dir is not None else _candidate_directories()
    for directory in directories:
        path = directory / LOG_FILE_NAME
        try:
            directory.mkdir(parents=True, exist_ok=True)
            logger.add(
                str(path),
                level=level,
                format=LOG_FORMAT,
                rotation="1 week",
                retention="1 month",
                compression="zip",
                enqueue=True,
            )
        except OSError:
            continue
        # Absolute, so the path shown to the user stays valid wherever the
        # process later changes directory.
        return path.resolve()
    return None
