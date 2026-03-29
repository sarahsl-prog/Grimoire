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
