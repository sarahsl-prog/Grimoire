"""
Shared Agent Utilities for Grimoire.

This module contains base classes and utilities that are shared across all Grimoire agents.
"""

import asyncio
import functools
import logging
from typing import Any, Callable, Optional, TypeVar
from pathlib import Path

from loguru import logger

T = TypeVar("T")


class BaseAgent:
    """Base class for all Grimoire agents providing shared functionality."""

    def __init__(self, name: str):
        self.name = name
        self.logger = logger.bind(agent=self.name)

    async def execute_with_retry(
        self,
        func: Callable[..., T],
        *args,
        max_retries: int = 3,
        delay: float = 1.0,
        **kwargs,
    ) -> T:
        """
        Execute a function with retry logic.

        Args:
            func: The function to execute
            *args: Positional arguments for the function
            max_retries: Maximum number of retry attempts
            delay: Delay between retries in seconds
            **kwargs: Keyword arguments for the function

        Returns:
            The result of the function call

        Raises:
            Exception: If all retry attempts fail
        """
        last_exception = None

        for attempt in range(max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                if attempt < max_retries:
                    self.logger.warning(
                        f"Attempt {attempt + 1} failed: {e}. Retrying in {delay}s..."
                    )
                    await asyncio.sleep(delay)
                else:
                    self.logger.error(
                        f"All {max_retries + 1} attempts failed. Last error: {e}"
                    )

        raise last_exception

    def log_execution(func: Callable[..., T]) -> Callable[..., T]:
        """
        Decorator to log function execution.

        Args:
            func: The function to decorate

        Returns:
            Wrapped function with logging
        """

        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            # Get the class name if this is a method
            class_name = ""
            if hasattr(self, "__class__"):
                class_name = f"{self.__class__.__name__}."

            func_name = f"{class_name}{func.__name__}"

            self.logger.info(f"Executing {func_name}")
            try:
                result = await func(self, *args, **kwargs)
                self.logger.info(f"Successfully executed {func_name}")
                return result
            except Exception as e:
                self.logger.error(f"Error executing {func_name}: {e}")
                raise

        return wrapper


# Common error handling utilities
class AgentError(Exception):
    """Base exception for agent-related errors."""

    pass


class ConfigurationError(AgentError):
    """Raised when agent configuration is invalid."""

    pass


class ExecutionError(AgentError):
    """Raised when agent execution fails."""

    pass


# Logging setup utilities
def setup_agent_logging(
    log_level: str = "INFO",
    log_file: Optional[Path] = None,
    rotation: str = "10 MB",
    retention: str = "1 week",
) -> None:
    """
    Setup logging for agents.

    Args:
        log_level: The logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional path to log file
        rotation: Log rotation setting
        retention: Log retention setting
    """
    # Remove default handler
    logger.remove()

    # Add file logger if specified
    if log_file:
        logger.add(
            log_file,
            level=log_level,
            rotation=rotation,
            retention=retention,
            enqueue=True,  # Thread safe
        )

    # Add console logger
    logger.add(lambda msg: print(msg, end=""), level=log_level, colorize=True)


# Common utility functions
def validate_path(path: str) -> Path:
    """
    Validate and resolve a file path.

    Args:
        path: Path string to validate

    Returns:
        Resolved Path object

    Raises:
        ValueError: If path is invalid
    """
    if not path:
        raise ValueError("Path cannot be empty")

    try:
        resolved_path = Path(path).resolve()
        return resolved_path
    except Exception as e:
        raise ValueError(f"Invalid path '{path}': {e}")


async def run_concurrent_tasks(tasks: list, limit: int = 10) -> list:
    """
    Run coroutines concurrently with a limit on concurrent executions.

    Args:
        tasks: List of coroutine objects to run
        limit: Maximum number of concurrent tasks

    Returns:
        List of results from the tasks
    """
    semaphore = asyncio.Semaphore(limit)

    async def run_task(task):
        async with semaphore:
            return await task

    return await asyncio.gather(*[run_task(task) for task in tasks])
