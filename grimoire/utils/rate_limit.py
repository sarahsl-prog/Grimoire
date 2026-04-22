"""
Rate limiting utilities for Grimoire.

This module provides rate limiting functionality to prevent overwhelming
external APIs and services.
"""

import asyncio
import functools
import time
from collections import defaultdict
from typing import Dict, Optional, Callable, Any
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """Token bucket implementation for rate limiting."""

    capacity: int
    refill_rate: float  # tokens per second
    tokens: float = field(default=0)
    last_refill: float = field(default_factory=time.time)

    def __post_init__(self):
        """Initialize with full capacity of tokens."""
        self.tokens = float(self.capacity)

    def consume(self, tokens: int = 1) -> bool:
        """
        Attempt to consume tokens from the bucket.

        Args:
            tokens: Number of tokens to consume

        Returns:
            True if tokens were consumed, False if insufficient tokens
        """
        now = time.time()

        # Refill tokens based on elapsed time
        elapsed = now - self.last_refill
        refill_amount = elapsed * self.refill_rate
        self.tokens = min(self.capacity, self.tokens + refill_amount)
        self.last_refill = now

        # Try to consume tokens
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        else:
            return False

    def wait_time(self, tokens: int = 1) -> float:
        """
        Calculate time to wait until enough tokens are available.

        Args:
            tokens: Number of tokens needed

        Returns:
            Time in seconds to wait, or 0 if tokens are available
        """
        now = time.time()

        # Refill tokens based on elapsed time (same as consume)
        elapsed = now - self.last_refill
        refill_amount = elapsed * self.refill_rate
        available = min(self.capacity, self.tokens + refill_amount)

        if available >= tokens:
            return 0.0

        # Calculate how long to wait for enough tokens
        needed = tokens - available
        return needed / self.refill_rate


class RateLimiter:
    """Rate limiter that manages multiple token buckets."""

    def __init__(self):
        """Initialize rate limiter."""
        self.buckets: Dict[str, TokenBucket] = {}

    def register_bucket(self, key: str, capacity: int, refill_rate: float) -> None:
        """
        Register a new token bucket.

        Args:
            key: Unique identifier for the bucket
            capacity: Maximum tokens in bucket
            refill_rate: Tokens per second to refill
        """
        self.buckets[key] = TokenBucket(capacity, refill_rate)

    def consume(self, key: str, tokens: int = 1) -> bool:
        """
        Attempt to consume tokens from a bucket.

        Args:
            key: Bucket identifier
            tokens: Number of tokens to consume

        Returns:
            True if tokens were consumed, False if insufficient tokens
        """
        if key not in self.buckets:
            # No rate limit for this key
            return True

        return self.buckets[key].consume(tokens)

    async def wait_if_needed(self, key: str, tokens: int = 1) -> bool:
        """
        Wait if necessary for tokens to become available.

        Args:
            key: Bucket identifier
            tokens: Number of tokens needed

        Returns:
            True if waited, False if no wait needed
        """
        if key not in self.buckets:
            # No rate limit for this key
            return False

        bucket = self.buckets[key]
        wait_time = bucket.wait_time(tokens)

        if wait_time > 0:
            await asyncio.sleep(wait_time)
            return True
        else:
            return False


# Global rate limiter instance
_global_rate_limiter = RateLimiter()


def register_rate_limit(bucket_key: str, capacity: int, refill_rate: float) -> None:
    """
    Register a rate limit bucket globally.

    Args:
        bucket_key: Unique identifier for the bucket
        capacity: Maximum tokens in bucket
        refill_rate: Tokens per second to refill
    """
    _global_rate_limiter.register_bucket(bucket_key, capacity, refill_rate)


def rate_limit(bucket_key: str, requests: int = 1):
    """
    Decorator to rate limit function calls.

    Args:
        bucket_key: Rate limit bucket identifier
        requests: Number of requests/token to consume per call

    Returns:
        Decorated function
    """

    def decorator(func: Callable[..., Any]):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            await _global_rate_limiter.wait_if_needed(bucket_key, requests)

            if not _global_rate_limiter.consume(bucket_key, requests):
                raise RuntimeError(f"Rate limit exceeded for {bucket_key}")

            return await func(*args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            bucket = _global_rate_limiter.buckets.get(bucket_key)
            if bucket:
                wait = bucket.wait_time(requests)
                if wait > 0:
                    time.sleep(wait)

            if not _global_rate_limiter.consume(bucket_key, requests):
                raise RuntimeError(f"Rate limit exceeded for {bucket_key}")

            return func(*args, **kwargs)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


# Predefined rate limits for common services
def setup_common_rate_limits():
    """Setup rate limits for common services."""
    # Google Drive API limits (example values)
    register_rate_limit("gdrive_read", 1000, 1000 / 60)  # 1000 requests per minute
    register_rate_limit("gdrive_write", 100, 100 / 60)  # 100 writes per minute

    # OneDrive API limits (example values)
    register_rate_limit("onedrive_read", 1000, 1000 / 60)
    register_rate_limit("onedrive_write", 100, 100 / 60)

    # Ollama API limits
    register_rate_limit("ollama_generate", 10, 10)  # 10 concurrent generations
