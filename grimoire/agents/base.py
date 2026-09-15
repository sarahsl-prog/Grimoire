"""
Shared Agent Utilities for Grimoire.

This module contains base classes and utilities that are shared across all Grimoire agents.
"""

import asyncio
import functools
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any, Concatenate

from loguru import logger


class BaseAgent:
    """Base class for all Grimoire agents providing shared functionality."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.logger = logger.bind(agent=self.name)

    async def execute_with_retry[T](
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        max_retries: int = 3,
        delay: float = 1.0,
        **kwargs: Any,
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
            ValueError: If max_retries is negative, which would leave no attempt
                to report a failure from.
        """
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")

        last_exception: Exception | None = None

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

        # The loop above always runs at least once (max_retries >= 0 is enforced)
        # and either returns or records an exception, so this is defensive only.
        if last_exception is None:  # pragma: no cover
            raise ExecutionError("Retry loop exited without a result or an error")
        raise last_exception


def log_execution[**P, T](
    func: Callable[Concatenate[BaseAgent, P], Awaitable[T]],
) -> Callable[Concatenate[BaseAgent, P], Coroutine[Any, Any, T]]:
    """
    Decorator to log execution of an async BaseAgent method.

    Lives at module scope rather than in the class body so it can be applied as
    a plain ``@log_execution`` inside agent subclasses; as a class attribute it
    would have received the agent instance in place of the decorated function.

    Args:
        func: The async agent method to decorate

    Returns:
        Wrapped method with logging
    """

    async def wrapper(self: BaseAgent, /, *args: P.args, **kwargs: P.kwargs) -> T:
        func_name = f"{self.__class__.__name__}.{func.__name__}"

        self.logger.info(f"Executing {func_name}")
        try:
            result = await func(self, *args, **kwargs)
            self.logger.info(f"Successfully executed {func_name}")
            return result
        except Exception as e:
            self.logger.error(f"Error executing {func_name}: {e}")
            raise

    # update_wrapper is called for its in-place side effect and `wrapper` is
    # returned directly: the _Wrapped type that functools.wraps/update_wrapper
    # returns does not unify with a Concatenate-based Callable under mypy
    # --strict, though the runtime metadata copy is identical.
    functools.update_wrapper(wrapper, func)
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
    log_file: Path | None = None,
    rotation: str = "10 MB",
    retention: str = "1 week",
) -> None:
    """Setup logging for agents — delegates to setup_logger() for consistent file+console output."""
    from grimoire.utils.logger import setup_logger

    setup_logger(level=log_level)


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
        raise ValueError(f"Invalid path '{path}': {e}") from e


async def run_concurrent_tasks[T](
    tasks: list[Coroutine[Any, Any, T]], limit: int = 10
) -> list[T]:
    """
    Run coroutines concurrently with a limit on concurrent executions.

    Args:
        tasks: List of coroutine objects to run
        limit: Maximum number of concurrent tasks

    Returns:
        List of results from the tasks
    """
    semaphore = asyncio.Semaphore(limit)

    async def run_task(task: Coroutine[Any, Any, T]) -> T:
        async with semaphore:
            return await task

    return list(await asyncio.gather(*[run_task(task) for task in tasks]))
