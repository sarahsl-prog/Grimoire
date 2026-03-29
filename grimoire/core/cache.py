"""Abstract base class for cache implementations and Redis implementation.

This module provides:
    - Cache: Abstract base class for cache implementations
    - RedisCache: Concrete implementation using Redis backend with TTL support
    - CacheKeyPrefix: Enum for key namespacing

Example:
    ```python
    from grimoire.core.cache import RedisCache

    cache = RedisCache(
        host="localhost",
        port=6379,
        db=2,
        namespace="grimoire"
    )
    await cache.set("key", {"data": "value"}, ttl=3600)
    value = await cache.get("key")
    ```
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Optional, Union
import json

from loguru import logger


class CacheKeyPrefix(Enum):
    """Namespace prefixes for cache keys.

    These prefixes help organize cache data by purpose and prevent
    key collisions between different components.

    Attributes:
        EMBEDDING: Cache entries for text embedding vectors
        QUERY: Cache entries for query results
        GENERATION: Cache entries for generated content
        DEFAULT: Default namespace for general cache entries
    """

    EMBEDDING = "grimoire:embedding:"
    QUERY = "grimoire:query:"
    GENERATION = "grimoire:generation:"
    DEFAULT = "grimoire:cache:"


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


class RedisCache(Cache):
    """Redis-backed cache implementation with TTL support.

    This class implements the Cache ABC using Redis as the backend.
    It provides key namespacing, JSON serialization, connection pooling,
    and proper error handling.

    Features:
        - Async operations via redis.asyncio
        - Connection pooling for performance
        - Automatic JSON serialization/deserialization
        - Configurable key namespacing
        - TTL support with per-key expiration
        - Graceful error handling with loguru logging

    Args:
        host: Redis server hostname (default: "localhost")
        port: Redis server port (default: 6379)
        db: Redis database number (default: 0)
        password: Redis password, if required (default: None)
        namespace: Key prefix for namespacing (default: CacheKeyPrefix.DEFAULT)
        socket_connect_timeout: Connection timeout in seconds (default: 5)
        socket_timeout: Socket timeout in seconds (default: 5)
        max_connections: Max connections in pool (default: 10)

    Example:
        ```python
        cache = RedisCache(
            host="localhost",
            port=6379,
            db=2,
            namespace=CacheKeyPrefix.EMBEDDING
        )
        await cache.connect()

        # Store with TTL
        await cache.set("query:123", {"result": [...]}, ttl=3600)

        # Retrieve
        value = await cache.get("query:123")

        await cache.disconnect()
        ```

    Note:
        Always call `connect()` before using the cache and `disconnect()`
        when done. Use as an async context manager for automatic cleanup.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
        namespace: Union[str, CacheKeyPrefix] = CacheKeyPrefix.DEFAULT,
        socket_connect_timeout: float = 5.0,
        socket_timeout: float = 5.0,
        max_connections: int = 10,
    ) -> None:
        """Initialize Redis cache configuration.

        Args:
            host: Redis server hostname.
            port: Redis server port.
            db: Redis database number.
            password: Redis password, if required.
            namespace: Key prefix for namespacing.
            socket_connect_timeout: Connection timeout in seconds.
            socket_timeout: Socket timeout in seconds.
            max_connections: Maximum connections in the pool.
        """
        self._host = host
        self._port = port
        self._db = db
        self._password = password
        self._namespace = (
            namespace.value if isinstance(namespace, CacheKeyPrefix) else namespace
        )
        self._socket_connect_timeout = socket_connect_timeout
        self._socket_timeout = socket_timeout
        self._max_connections = max_connections
        self._client: Optional[Any] = None
        self._pool: Optional[Any] = None

        logger.debug(
            f"RedisCache initialized with host={host}, port={port}, db={db}, "
            f"namespace={self._namespace}"
        )

    @property
    def is_connected(self) -> bool:
        """Check if Redis connection is established.

        Returns:
            True if connected, False otherwise.
        """
        return self._client is not None

    def _full_key(self, key: str) -> str:
        """Generate full key with namespace prefix.

        Args:
            key: Raw cache key.

        Returns:
            Full key with namespace prefix.
        """
        return f"{self._namespace}{key}"

    def _serialize(self, value: Any) -> str:
        """Serialize value to JSON string.

        Args:
            value: Value to serialize.

        Returns:
            JSON string representation.

        Raises:
            TypeError: If value cannot be serialized to JSON.
        """
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            logger.error(f"Failed to serialize value: {e}")
            raise TypeError(f"Value not JSON-serializable: {e}") from e

    def _deserialize(self, data: Union[str, bytes, None]) -> Optional[Any]:
        """Deserialize JSON string to Python object.

        Args:
            data: Raw data from Redis (can be str, bytes, or None).

        Returns:
            Deserialized Python object, or None if data is None.

        Raises:
            ValueError: If data cannot be deserialized.
        """
        if data is None:
            return None

        if isinstance(data, bytes):
            data = data.decode("utf-8")

        try:
            return json.loads(data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"Failed to deserialize value: {e}")
            raise ValueError(f"Invalid JSON data: {e}") from e

    async def connect(self) -> None:
        """Establish connection to Redis.

        Creates a connection pool and Redis client instance.
        Idempotent - can be called multiple times safely.

        Raises:
            ConnectionError: If connection to Redis fails.
        """
        if self.is_connected:
            logger.debug("RedisCache already connected")
            return

        try:
            import redis.asyncio as redis

            self._pool = redis.ConnectionPool(
                host=self._host,
                port=self._port,
                db=self._db,
                password=self._password,
                socket_connect_timeout=self._socket_connect_timeout,
                socket_timeout=self._socket_timeout,
                max_connections=self._max_connections,
            )
            self._client = redis.Redis(connection_pool=self._pool)

            # Test connection
            await self._client.ping()
            logger.info(f"RedisCache connected to {self._host}:{self._port}/{self._db}")
        except ImportError:
            logger.error("redis package not installed")
            raise ImportError(
                "redis package required. Install with: uv add redis"
            ) from None
        except Exception as e:
            self._client = None
            self._pool = None
            logger.error(f"Failed to connect to Redis: {e}")
            raise ConnectionError(f"Failed to connect to Redis: {e}") from e

    async def disconnect(self) -> None:
        """Close connection to Redis.

        Closes the connection pool and releases resources.
        Safe to call even if not connected.
        """
        if self._client is not None:
            try:
                await self._client.close()
                logger.info("RedisCache disconnected")
            except Exception as e:
                logger.warning(f"Error during Redis disconnect: {e}")
            finally:
                self._client = None
                self._pool = None

    async def __aenter__(self) -> "RedisCache":
        """Async context manager entry.

        Returns:
            Self for use in async with statement.
        """
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit.

        Ensures disconnect is called even if an exception occurs.
        """
        await self.disconnect()

    async def get(self, key: str) -> Optional[Any]:
        """Get a value from the cache.

        Args:
            key: Cache key to look up.

        Returns:
            Cached value if found and not expired, None otherwise.

        Raises:
            RuntimeError: If cache retrieval fails (not connection errors).
        """
        if not self.is_connected:
            logger.warning("RedisCache not connected, attempting connection")
            await self.connect()

        full_key = self._full_key(key)
        logger.debug(f"Cache GET: {full_key}")

        try:
            data = await self._client.get(full_key)  # type: ignore[union-attr]
            if data is None:
                logger.debug(f"Cache MISS: {full_key}")
                return None

            value = self._deserialize(data)
            logger.debug(f"Cache HIT: {full_key}")
            return value

        except Exception as e:
            logger.error(f"Redis GET failed for key '{full_key}': {e}")
            raise RuntimeError(f"Cache retrieval failed: {e}") from e

    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
    ) -> None:
        """Store a value in the cache.

        Args:
            key: Cache key.
            value: Value to cache (must be JSON-serializable).
            ttl: Time-to-live in seconds. None means no expiration.

        Raises:
            TypeError: If value cannot be serialized.
            RuntimeError: If cache storage fails.
        """
        if not self.is_connected:
            logger.warning("RedisCache not connected, attempting connection")
            await self.connect()

        full_key = self._full_key(key)
        serialized = self._serialize(value)

        logger.debug(f"Cache SET: {full_key} (ttl={ttl})")

        try:
            await self._client.set(  # type: ignore[union-attr]
                full_key, serialized, ex=ttl
            )
            logger.debug(f"Cache SET successful: {full_key}")
        except Exception as e:
            logger.error(f"Redis SET failed for key '{full_key}': {e}")
            raise RuntimeError(f"Cache storage failed: {e}") from e

    async def delete(self, key: str) -> None:
        """Delete a key from the cache.

        Args:
            key: Cache key to delete.

        Raises:
            RuntimeError: If cache deletion fails.

        Note:
            Deleting a non-existent key does not raise an error.
        """
        if not self.is_connected:
            logger.warning("RedisCache not connected, attempting connection")
            await self.connect()

        full_key = self._full_key(key)
        logger.debug(f"Cache DELETE: {full_key}")

        try:
            result = await self._client.delete(full_key)  # type: ignore[union-attr]
            logger.debug(f"Cache DELETE: {full_key} (deleted={result > 0})")
        except Exception as e:
            logger.error(f"Redis DELETE failed for key '{full_key}': {e}")
            raise RuntimeError(f"Cache deletion failed: {e}") from e

    async def clear(self) -> None:
        """Clear all cached values in the namespace.

        Warning:
            This removes ALL cached data with the current namespace prefix.
            Use with caution.

        Raises:
            RuntimeError: If cache clearing fails.
        """
        if not self.is_connected:
            logger.warning("RedisCache not connected, attempting connection")
            await self.connect()

        logger.warning(f"Cache CLEAR: namespace={self._namespace}")

        try:
            pattern = f"{self._namespace}*"
            keys = await self._client.keys(pattern)  # type: ignore[union-attr]
            if keys:
                await self._client.delete(*keys)  # type: ignore[union-attr]
                logger.info(f"Cache CLEAR: deleted {len(keys)} keys")
            else:
                logger.info("Cache CLEAR: no keys found in namespace")
        except Exception as e:
            logger.error(f"Redis CLEAR failed: {e}")
            raise RuntimeError(f"Cache clearing failed: {e}") from e

    async def ttl(self, key: str) -> int:
        """Get remaining TTL for a key.

        Args:
            key: Cache key to check.

        Returns:
            Remaining TTL in seconds.
            -1 if key exists but has no expiration.
            -2 if key does not exist.

        Raises:
            RuntimeError: If TTL check fails.
        """
        if not self.is_connected:
            logger.warning("RedisCache not connected, attempting connection")
            await self.connect()

        full_key = self._full_key(key)

        try:
            result = await self._client.ttl(full_key)  # type: ignore[union-attr]
            logger.debug(f"Cache TTL: {full_key} = {result}s")
            return int(result)
        except Exception as e:
            logger.error(f"Redis TTL failed for key '{full_key}': {e}")
            raise RuntimeError(f"TTL check failed: {e}") from e

    async def exists(self, key: str) -> bool:
        """Check if a key exists in the cache.

        Args:
            key: Cache key to check.

        Returns:
            True if key exists, False otherwise.

        Raises:
            RuntimeError: If existence check fails.
        """
        if not self.is_connected:
            logger.warning("RedisCache not connected, attempting connection")
            await self.connect()

        full_key = self._full_key(key)

        try:
            result = await self._client.exists(full_key)  # type: ignore[union-attr]
            exists = bool(int(result) > 0)
            logger.debug(f"Cache EXISTS: {full_key} = {exists}")
            return exists
        except Exception as e:
            logger.error(f"Redis EXISTS failed for key '{full_key}': {e}")
            raise RuntimeError(f"Existence check failed: {e}") from e


__all__ = ["Cache", "RedisCache", "CacheKeyPrefix"]
