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
"""Cache implementations for Grimoire.

This module provides:
- Cache ABC for cache interface
- RedisCache: Distributed cache using Redis
- DiskCache: Local file-based cache using diskcache library
- CacheFactory: Factory for creating cache instances
"""

import hashlib
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional, Union

from loguru import logger
"""Abstract base class for cache implementations."""

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
    """Redis-based cache implementation.

    Provides distributed caching with TTL support via Redis.
    Supports key namespacing for multi-tenant use.

    Args:
        host: Redis host.
        port: Redis port.
        db: Redis database number.
        namespace: Optional key prefix for namespacing.
        password: Optional Redis password.

    Example:
        ```python
        cache = RedisCache(host="localhost", port=6379, namespace="embeddings")
        await cache.set("query_123", embedding_vector, ttl=3600)
        result = await cache.get("query_123")
        ```
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
        namespace: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        """Initialize Redis cache.

        Args:
            host: Redis host.
            port: Redis port.
            db: Redis database number.
            namespace: Optional key prefix for namespacing.
            password: Optional Redis password.
        """
        self.host = host
        self.port = port
        self.db = db
        self.namespace = namespace
        self.password = password
        self._client: Optional[Any] = None

    def _get_key(self, key: str) -> str:
        """Get namespaced key.

        Args:
            key: Original cache key.

        Returns:
            Namespaced key if namespace is set, original key otherwise.
        """
        if self.namespace:
            return f"{self.namespace}:{key}"
        return key

    def _get_client(self) -> Any:
        """Lazy initialization of Redis client.

        Returns:
            Redis client instance.

        Raises:
            RuntimeError: If Redis is not available.
        """
        if self._client is None:
            try:
                import redis.asyncio as redis

                self._client = redis.Redis(
                    host=self.host,
                    port=self.port,
                    db=self.db,
                    password=self.password,
                    decode_responses=True,
                )
                logger.debug(
                    f"RedisCache initialized: {self.host}:{self.port}/{self.db}"
                )
            except Exception as e:
                logger.error(f"Failed to connect to Redis: {e}")
                raise RuntimeError(f"Redis connection failed: {e}") from e
        return self._client

    async def get(self, key: str) -> Optional[Any]:
        """Get a value from Redis cache.

        Args:
            key: Cache key to look up.

        Returns:
            Cached value if found and not expired, None otherwise.
        """
        try:
            client = self._get_client()
            namespaced_key = self._get_key(key)
            data = await client.get(namespaced_key)

            if data is None:
                return None

            return json.loads(data)
        except Exception as e:
            logger.warning(f"Redis get failed for key '{key}': {e}")
            return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
    ) -> None:
        """Store a value in Redis cache.

        Args:
            key: Cache key.
            value: Value to cache (JSON-serializable).
            ttl: Time-to-live in seconds. None means no expiration.
        """
        try:
            client = self._get_client()
            namespaced_key = self._get_key(key)
            serialized = json.dumps(value)

            if ttl is not None:
                await client.setex(namespaced_key, ttl, serialized)
            else:
                await client.set(namespaced_key, serialized)

            logger.debug(f"Redis cache set: {key} (ttl={ttl})")
        except Exception as e:
            logger.warning(f"Redis set failed for key '{key}': {e}")
            raise RuntimeError(f"Failed to cache value: {e}") from e

    async def delete(self, key: str) -> None:
        """Delete a key from Redis cache.

        Args:
            key: Cache key to delete.
        """
        try:
            client = self._get_client()
            namespaced_key = self._get_key(key)
            await client.delete(namespaced_key)
            logger.debug(f"Redis cache deleted: {key}")
        except Exception as e:
            logger.warning(f"Redis delete failed for key '{key}': {e}")

    async def clear(self) -> None:
        """Clear all cached values in the namespace.

        Warning:
            If namespace is set, only clears keys in that namespace.
            Otherwise, clears ALL keys in the database.
        """
        try:
            client = self._get_client()

            if self.namespace:
                # Delete only keys in namespace
                pattern = f"{self.namespace}:*"
                cursor = 0
                while True:
                    cursor, keys = await client.scan(cursor, match=pattern, count=100)
                    if keys:
                        await client.delete(*keys)
                    if cursor == 0:
                        break
            else:
                # Clear entire database
                await client.flushdb()

            logger.info(f"Redis cache cleared (namespace={self.namespace})")
        except Exception as e:
            logger.error(f"Redis clear failed: {e}")
            raise RuntimeError(f"Failed to clear cache: {e}") from e

    async def health_check(self) -> bool:
        """Check if Redis connection is healthy.

        Returns:
            True if connection is working, False otherwise.
        """
        try:
            client = self._get_client()
            await client.ping()
            return True
        except Exception:
            return False


class DiskCache(Cache):
    """Disk-based cache implementation using diskcache library.

    Provides local file-based caching suitable for single-node deployments.
    Uses SQLite for storage with automatic compression.

    Args:
        path: Directory path for cache files.
        size_limit: Maximum cache size in bytes (default: 1GB).

    Example:
        ```python
        cache = DiskCache(path="/tmp/grimoire_cache")
        await cache.set("embedding", vector_data, ttl=3600)
        result = await cache.get("embedding")
        ```
    """

    def __init__(
        self,
        path: Union[str, Path] = ".cache",
        size_limit: int = 1024 * 1024 * 1024,  # 1GB
    ) -> None:
        """Initialize disk cache.

        Args:
            path: Directory path for cache files.
            size_limit: Maximum cache size in bytes.
        """
        self.path = Path(path)
        self.size_limit = size_limit
        self._cache: Optional[Any] = None

    def _get_cache(self) -> Any:
        """Lazy initialization of diskcache.

        Returns:
            DiskCache instance.

        Raises:
            RuntimeError: If diskcache is not available.
        """
        if self._cache is None:
            try:
                import diskcache

                self.path.mkdir(parents=True, exist_ok=True)
                self._cache = diskcache.Cache(
                    str(self.path),
                    size_limit=self.size_limit,
                )
                logger.debug(f"DiskCache initialized: {self.path}")
            except Exception as e:
                logger.error(f"Failed to initialize DiskCache: {e}")
                raise RuntimeError(f"DiskCache initialization failed: {e}") from e
        return self._cache

    def _validate_key(self, key: str) -> str:
        """Validate and sanitize cache key.

        Diskcache has restrictions on key format. We hash keys that might
        contain problematic characters.

        Args:
            key: Cache key to validate.

        Returns:
            Validated/sanitized key.
        """
        # diskcache keys must be str and contain reasonable characters
        # Hash long or potentially problematic keys
        if len(key) > 200 or any(c in key for c in ["\n", "\r", "\x00"]):
            return hashlib.sha256(key.encode()).hexdigest()
        return key

    async def get(self, key: str) -> Optional[Any]:
        """Get a value from disk cache.

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
        """
        try:
            cache = self._get_cache()
            validated_key = self._validate_key(key)
            value = cache.get(validated_key)

            if value is not None and isinstance(value, str):
                # Try to deserialize JSON
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return value

            return value
        except Exception as e:
            logger.warning(f"DiskCache get failed for key '{key}': {e}")
            return None

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
        """Store a value in disk cache.

        Args:
            key: Cache key.
            value: Value to cache.
            ttl: Time-to-live in seconds. None means no expiration.
        """
        try:
            cache = self._get_cache()
            validated_key = self._validate_key(key)

            # Serialize to JSON for consistency with Redis
            try:
                serialized = json.dumps(value)
            except (TypeError, ValueError):
                # Fall back to storing as-is if not JSON-serializable
                serialized = value

            cache.set(validated_key, serialized, expire=ttl)
            logger.debug(f"DiskCache set: {key} (ttl={ttl})")
        except Exception as e:
            logger.warning(f"DiskCache set failed for key '{key}': {e}")
            raise RuntimeError(f"Failed to cache value: {e}") from e

    async def delete(self, key: str) -> None:
        """Delete a key from disk cache.

        Args:
            key: Cache key to delete.
        """
        try:
            cache = self._get_cache()
            validated_key = self._validate_key(key)
            cache.delete(validated_key)
            logger.debug(f"DiskCache deleted: {key}")
        except Exception as e:
            logger.warning(f"DiskCache delete failed for key '{key}': {e}")

    async def clear(self) -> None:
        """Clear all cached values.

        Warning:
            This removes ALL cached data from the disk cache.
        """
        try:
            cache = self._get_cache()
            cache.clear()
            logger.info(f"DiskCache cleared: {self.path}")
        except Exception as e:
            logger.error(f"DiskCache clear failed: {e}")
            raise RuntimeError(f"Failed to clear cache: {e}") from e

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics.

        Returns:
            Dictionary with cache statistics.
        """
        try:
            cache = self._get_cache()
            return {
                "size": len(cache),
                "volume": cache.volume(),
                "path": str(self.path),
            }
        except Exception as e:
            logger.warning(f"Failed to get cache stats: {e}")
            return {"error": str(e)}


class CacheFactory:
    """Factory for creating cache instances.

    Provides a unified way to create cache backends based on configuration.

    Example:
        ```python
        # Redis cache
        cache = CacheFactory.create(
            backend="redis",
            host="localhost",
            port=6379
        )

        # Disk cache
        cache = CacheFactory.create(
            backend="disk",
            path="/tmp/cache"
        )
        ```
    """

    @staticmethod
    def create(
        backend: str = "disk",
        **kwargs: Any,
    ) -> Cache:
        """Create a cache instance.

        Args:
            backend: Cache backend type ("redis" or "disk").
            **kwargs: Backend-specific configuration options.

        Returns:
            Configured Cache instance.

        Raises:
            ValueError: If backend type is not supported.
        """
        backend = backend.lower()

        if backend == "redis":
            return RedisCache(**kwargs)
        elif backend == "disk":
            return DiskCache(**kwargs)
        else:
            raise ValueError(
                f"Unknown cache backend: {backend}. Use 'redis' or 'disk'."
            )
