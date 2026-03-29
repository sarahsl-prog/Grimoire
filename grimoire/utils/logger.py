"""Structured logging configuration using loguru."""

import os
import sys
from pathlib import Path
from typing import Any, Optional

from loguru import logger

DEFAULT_LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level:<8} | "
    "{name}:{function}:{line} | "
    "{message}"
)


def _get_log_directory() -> Path:
    """Determine the appropriate log directory.

    Returns:
        Path to use for log files.
    """
    # Check for production environment
    production_logs = Path("/var/log/grimoire")

    # Use production path if writable or if running as root/system service
    try:
        if production_logs.parent.exists() and os.access(
            production_logs.parent, os.W_OK
        ):
            production_logs.mkdir(parents=True, exist_ok=True)
            return production_logs
    except (OSError, PermissionError):
        pass

    # Fall back to development path
    dev_logs = Path("./log")
    dev_logs.mkdir(parents=True, exist_ok=True)
    return dev_logs.absolute()


def setup_logger(
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    rotation: str = "1 week",
    retention: str = "1 month",
) -> None:
    """Configure loguru with structured logging and rotation.

    Args:
        level: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_dir: Directory for log files. Auto-detected if not specified.
        rotation: Log rotation interval (e.g., "1 week", "500 MB", "00:00").
        retention: Log retention period (e.g., "1 month", "10 days").

    Note:
        Never log API keys, tokens, passwords, or other secrets.
    """
    # Remove default handler
    logger.remove()

    # Determine log directory
    if log_dir is None:
        log_dir = _get_log_directory()

    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)

    # Console handler (always enabled)
    logger.add(
        sys.stdout,
        level=level,
        format=DEFAULT_LOG_FORMAT,
        colorize=sys.stdout.isatty(),
    )

    # File handler with rotation
    log_file = log_dir_path / "grimoire.log"

    logger.add(
        str(log_file),
        level=level,
        format=DEFAULT_LOG_FORMAT,
        rotation=rotation,
        retention=retention,
        compression="zip",
        enqueue=True,
    )

    logger.debug(f"Logger initialized: level={level}, log_dir={log_dir_path}")


def get_logger(name: str) -> Any:
    """Get a logger instance with the given name.

    Args:
        name: Logger name (typically __name__).

    Returns:
        Configured logger instance.
    """
    return logger.bind(name=name)


# Configure default logger on import
setup_logger()
