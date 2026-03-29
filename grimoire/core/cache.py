"""Abstract base class for cache implementations."""

from abc import ABC, abstractmethod
from typing import Any, Optional


class Cache(ABC):
    """Abstract base class for cache implementations.

    This interface supports Redis, DiskCache, and other cache backends.
    Provides key-value storage with optional TTL (time-to-live).

    Example:
        ```python
        class RedisCache(Cache):
            async def get(self, key: str) -> Optional[Any]:
                # Implementation
                pass
        ```

    Note:
        Cache implementations should handle serialization transparently.
        Values are typically JSON-serializable objects.
    """

    @abstractmethod
    async def get(self, key: str) -> Optional[Any]:
        """Get a value from the cache.

        Args:
            key: Cache key to look up.

        Returns:
            Cached value if found and not expired, None otherwise.

        Raises:
            RuntimeError: If cache retrieval fails.
        """
        raise NotImplementedError("Subclasses must implement get()")

    @abstractmethod
    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
    ) -> None:
        """Store a value in the cache.

        Args:
            key: Cache key.
            value: Value to cache (should be JSON-serializable).
            ttl: Time-to-live in seconds. None means no expiration.

        Raises:
            TypeError: If value cannot be serialized.
            RuntimeError: If cache storage fails.
        """
        raise NotImplementedError("Subclasses must implement set()")

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete a key from the cache.

        Args:
            key: Cache key to delete.

        Raises:
            RuntimeError: If cache deletion fails.

        Note:
            Deleting a non-existent key should not raise an error.
        """
        raise NotImplementedError("Subclasses must implement delete()")

    @abstractmethod
    async def clear(self) -> None:
        """Clear all cached values.

        Warning:
            This removes ALL cached data. Use with caution.

        Raises:
            RuntimeError: If cache clearing fails.
        """
        raise NotImplementedError("Subclasses must implement clear()")
