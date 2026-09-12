"""
Observability utilities for Grimoire.

This module provides utilities for logging, tracing, and monitoring
the Grimoire knowledge management system.
"""

import asyncio
import functools
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any

from loguru import logger


class PerformanceTimer:
    """Context manager for measuring execution time."""

    def __init__(self, operation: str, logger_instance: Any = None) -> None:
        """
        Initialize timer.

        Args:
            operation: Name of the operation being timed
            logger_instance: Logger to use for output (defaults to loguru logger)
        """
        self.operation = operation
        self.logger = logger_instance or logger
        self.start_time: float | None = None

    def __enter__(self) -> "PerformanceTimer":
        """Start timing."""
        self.start_time = time.perf_counter()
        self.logger.debug(f"Starting {self.operation}")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Stop timing and log result."""
        end_time = time.perf_counter()
        # start_time is None only if __exit__ runs without __enter__; report a
        # zero duration rather than raising out of a context manager's exit.
        duration = end_time - self.start_time if self.start_time is not None else 0.0

        if exc_type is None:
            self.logger.info(f"{self.operation} completed in {duration:.2f}s")
        else:
            self.logger.error(
                f"{self.operation} failed after {duration:.2f}s: {exc_val}"
            )

        # Returning None (rather than False) keeps exceptions propagating while
        # satisfying the typed __exit__ contract.


def trace_function(
    operation: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Decorator to trace function execution time and success/failure.

    Args:
        operation: Name of the operation (defaults to function name)

    Returns:
        Decorated function
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        # Resolved into a local, not back into `operation` via nonlocal: writing
        # to the closure would make a reused decorator object label every later
        # function with the first one's name.
        op_name = operation or f"{func.__module__}.{func.__name__}"

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with PerformanceTimer(op_name):
                return await func(*args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with PerformanceTimer(op_name):
                return func(*args, **kwargs)

        # Check if function is async
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


class MetricsCollector:
    """Simple metrics collector for counting operations."""

    def __init__(self) -> None:
        """Initialize metrics collector."""
        self.counters: dict[str, int] = {}
        self.timers: dict[str, list[float]] = {}

    def increment(self, metric: str, value: int = 1) -> None:
        """
        Increment a counter metric.

        Args:
            metric: Name of the metric
            value: Value to increment by
        """
        if metric not in self.counters:
            self.counters[metric] = 0
        self.counters[metric] += value

    def record_time(self, metric: str, duration: float) -> None:
        """
        Record a timing metric.

        Args:
            metric: Name of the metric
            duration: Duration in seconds
        """
        if metric not in self.timers:
            self.timers[metric] = []
        self.timers[metric].append(duration)

    def get_counter(self, metric: str) -> int:
        """
        Get counter value.

        Args:
            metric: Name of the metric

        Returns:
            Counter value
        """
        return self.counters.get(metric, 0)

    def get_average_time(self, metric: str) -> float:
        """
        Get average timing for a metric.

        Args:
            metric: Name of the metric

        Returns:
            Average time in seconds, or 0 if no recordings
        """
        if metric not in self.timers or not self.timers[metric]:
            return 0.0
        return sum(self.timers[metric]) / len(self.timers[metric])

    def reset(self) -> None:
        """Reset all metrics."""
        self.counters.clear()
        self.timers.clear()


# Global metrics collector
_global_metrics = MetricsCollector()


def increment_metric(metric: str, value: int = 1) -> None:
    """
    Increment a global metric counter.

    Args:
        metric: Name of the metric
        value: Value to increment by
    """
    _global_metrics.increment(metric, value)


def record_timing(metric: str, duration: float) -> None:
    """
    Record a timing metric globally.

    Args:
        metric: Name of the metric
        duration: Duration in seconds
    """
    _global_metrics.record_time(metric, duration)


def get_metric_count(metric: str) -> int:
    """
    Get global metric counter value.

    Args:
        metric: Name of the metric

    Returns:
        Counter value
    """
    return _global_metrics.get_counter(metric)


def get_average_timing(metric: str) -> float:
    """
    Get global average timing for a metric.

    Args:
        metric: Name of the metric

    Returns:
        Average time in seconds
    """
    return _global_metrics.get_average_time(metric)


class Tracer:
    """Simple tracer for function calls."""

    def __init__(self) -> None:
        """Initialize tracer."""
        self.trace_stack: list[dict[str, Any]] = []

    def trace_call(
        self,
        func_name: str,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> None:
        """
        Trace a function call.

        Args:
            func_name: Name of the function
            args: Positional arguments
            kwargs: Keyword arguments
        """
        kwargs = kwargs or {}
        self.trace_stack.append(
            {
                "function": func_name,
                "args": args,
                "kwargs": kwargs,
                "timestamp": time.time(),
            }
        )
        logger.debug(f"TRACE: {func_name}({args}, {kwargs})")

    def trace_return(self, func_name: str, result: Any = None) -> None:
        """
        Trace a function return.

        Args:
            func_name: Name of the function
            result: Function result
        """
        logger.debug(f"TRACE: {func_name} -> {result}")

    def get_trace(self) -> list[dict[str, Any]]:
        """
        Get trace history.

        Returns:
            List of trace entries
        """
        return self.trace_stack.copy()

    def clear_trace(self) -> None:
        """Clear trace history."""
        self.trace_stack.clear()


# Global tracer
_global_tracer = Tracer()


def trace_call(
    func_name: str,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
) -> None:
    """
    Trace a function call globally.

    Args:
        func_name: Name of the function
        args: Positional arguments
        kwargs: Keyword arguments
    """
    _global_tracer.trace_call(func_name, args, kwargs)


def trace_return(func_name: str, result: Any = None) -> None:
    """
    Trace a function return globally.

    Args:
        func_name: Name of the function
        result: Function result
    """
    _global_tracer.trace_return(func_name, result)


# Convenience functions
def log_performance(
    operation: str,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Decorator for logging performance of functions.

    Args:
        operation: Name of the operation

    Returns:
        Decorator function
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                result = await func(*args, **kwargs)
                duration = time.perf_counter() - start
                logger.info(f"{operation}: {duration:.2f}s")
                increment_metric(f"{operation}.success")
                record_timing(operation, duration)
                return result
            except Exception as e:
                duration = time.perf_counter() - start
                logger.error(f"{operation} failed after {duration:.2f}s: {e}")
                increment_metric(f"{operation}.error")
                raise

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
                duration = time.perf_counter() - start
                logger.info(f"{operation}: {duration:.2f}s")
                increment_metric(f"{operation}.success")
                record_timing(operation, duration)
                return result
            except Exception as e:
                duration = time.perf_counter() - start
                logger.error(f"{operation} failed after {duration:.2f}s: {e}")
                increment_metric(f"{operation}.error")
                raise

        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    return decorator
