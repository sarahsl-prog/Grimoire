"""Structured logging configuration using loguru."""

import sys
from pathlib import Path
from typing import Any

from loguru import logger

DEFAULT_LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level:<8} | "
    "{extra[session_id]} | "
    "{name}:{function}:{line} | "
    "{message}"
)

CLI_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level:<8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def _default_session_id(record: Any) -> None:
    """Ensure ``extra[session_id]`` exists so the log format never raises."""
    record["extra"].setdefault("session_id", "-")


def setup_logger(
    level: str = "INFO",
    log_dir: Path | None = None,
    rotation: str = "1 week",
    retention: str = "1 month",
    console_format: str = DEFAULT_LOG_FORMAT,
    use_json: bool = False,
) -> None:
    """Configure loguru with console + file sinks.

    Always writes to file. Console sink is also added.

    Args:
        level: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_dir: Directory for log files. Reads from settings if not given.
        rotation: Log rotation interval (e.g., "1 week", "500 MB", "00:00").
        retention: Log retention period (e.g., "1 month", "10 days").
        console_format: Format string for the console sink.
        use_json: Emit JSON to the file sink when True.
    """
    logger.remove()

    # DEFAULT_LOG_FORMAT references extra[session_id]; give every record a "-"
    # when no request/launch bound one.  A patcher (not configure(extra=...))
    # so a session id a front end already configured is never clobbered.
    logger.configure(patcher=_default_session_id)

    if log_dir is None:
        try:
            from grimoire.config import get_settings

            settings = get_settings()
            log_dir = Path(settings.logging.log_dir)
            rotation = settings.logging.rotation
            retention = settings.logging.retention
            use_json = settings.logging.use_json
        except Exception:
            log_dir = Path("./logs")

    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)

    # Console sink — always plain text
    logger.add(
        sys.stderr,
        level=level,
        format=console_format,
        colorize=sys.stderr.isatty(),
    )

    # File sink — JSON when requested, plain text otherwise
    file_format = "{message}" if use_json else DEFAULT_LOG_FORMAT
    file_serialize = use_json
    logger.add(
        str(log_dir_path / "grimoire.log"),
        level=level,
        format=file_format,
        rotation=rotation,
        retention=retention,
        compression="zip",
        enqueue=True,
        serialize=file_serialize,
    )

    logger.debug(f"Logger initialized: level={level}, log_dir={log_dir_path}, json={use_json}")


def get_logger(name: str) -> Any:
    """Get a named logger instance."""
    return logger.bind(name=name)


# Default setup on import — CLI and agents call setup_logger() again to
# override level and pick up the correct log_dir from settings.
setup_logger()
