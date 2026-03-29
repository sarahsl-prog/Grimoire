"""Comprehensive tests for Cache implementations.

This module tests the RedisCache implementation including:
- Happy path: set/get/delete/clear operations
- TTL expiration
- Key namespacing
- Connection error handling
- JSON serialization/deserialization
- Edge cases and error handling
- Async behavior
- State management
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from grimoire.core.cache import Cache, CacheKeyPrefix, RedisCache

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_redis_client() -> Generator[MagicMock, None, None]:
    """Create a mock Redis client for testing.

    Yields:
        MagicMock: Configured mock Redis client.
    """
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=None)
    mock_client.set = AsyncMock(return_value=True)
    mock_client.delete = AsyncMock(return_value=1)
    mock_client.ping = AsyncMock(return_value=True)
    mock_client.keys = AsyncMock(return_value=[])
    mock_client.ttl = AsyncMock(return_value=-1)
    mock_client.exists = AsyncMock(return_value=0)
    mock_client.close = AsyncMock(return_value=None)
    yield mock_client


@pytest.fixture
def mock_connection_pool() -> Generator[MagicMock, None, None]:
    """Create a mock connection pool for testing.

    Yields:
        MagicMock: Configured mock connection pool.
    """
    with patch("grimoire.core.cache.redis.ConnectionPool") as mock_pool_class:
        mock_pool = MagicMock()
        mock_pool_class.return_value = mock_pool
        yield mock_pool


@pytest_asyncio.fixture
async def cache_instance(mock_redis_client: MagicMock) -> RedisCache:
    """Create a connected RedisCache instance with mocked client.

    Args:
        mock_redis_client: Mocked Redis client fixture.

    Returns:
        RedisCache: Configured cache instance ready for use.
    """
    cache = RedisCache(
        host="localhost",
        port=6379,
        db=2,
        namespace="test:cache:",
    )
    cache._client = mock_redis_client
    cache._pool = MagicMock()
    return cache


@pytest_asyncio.fixture
async def embedded_cache() -> RedisCache:
    """Create a standard test cache instance.

    Returns:
        RedisCache: Cache with default test settings.
    """
    return RedisCache(
        host="localhost",
        port=6379,
        db=2,
        namespace="test:",
    )


# =============================================================================
# Happy Path Tests
# =============================================================================


class TestCacheHappyPath:
    """Standard use cases - basic functionality works."""

    @pytest.mark.asyncio
    async def test_redis_cache_can_be_created(self) -> None:
        """RedisCache can be instantiated with valid config."""
        cache = RedisCache(
            host="localhost",
            port=6379,
            db=2,
        )
        assert cache is not None
        assert cache._host == "localhost"
        assert cache._port == 6379
        assert cache._db == 2
        assert cache._namespace == CacheKeyPrefix.DEFAULT.value

    @pytest.mark.asyncio
    async def test_set_and_get_string(self, cache_instance: RedisCache) -> None:
        """Can store and retrieve a string value."""
        cache = cache_instance
        await cache.set("test_key", "test_value")

        # Configure mock to return the value
        cache._client.get = AsyncMock(return_value='"test_value"')

        result = await cache.get("test_key")
        assert result == "test_value"

    @pytest.mark.asyncio
    async def test_set_and_get_dict(self, cache_instance: RedisCache) -> None:
        """Can store and retrieve a dictionary."""
        cache = cache_instance
        test_dict = {"key": "value", "number": 42, "nested": {"a": 1}}

        await cache.set("dict_key", test_dict)
        cache._client.get = AsyncMock(return_value=json.dumps(test_dict))

        result = await cache.get("dict_key")
        assert result == test_dict

    @pytest.mark.asyncio
    async def test_set_and_get_list(self, cache_instance: RedisCache) -> None:
        """Can store and retrieve a list."""
        cache = cache_instance
        test_list = [1, 2, 3, "four", {"five": 5}]

        await cache.set("list_key", test_list)
        cache._client.get = AsyncMock(return_value=json.dumps(test_list))

        result = await cache.get("list_key")
        assert result == test_list

    @pytest.mark.asyncio
    async def test_set_and_get_none(self, cache_instance: RedisCache) -> None:
        """Can store and retrieve None value."""
        cache = cache_instance

        await cache.set("none_key", None)
        cache._client.get = AsyncMock(return_value="null")

        result = await cache.get("none_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_missing_key(self, cache_instance: RedisCache) -> None:
        """Getting a missing key returns None."""
        cache = cache_instance
        cache._client.get = AsyncMock(return_value=None)

        result = await cache.get("nonexistent_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_existing_key(self, cache_instance: RedisCache) -> None:
        """Can delete an existing key."""
        cache = cache_instance

        await cache.set("delete_me", "value")
        cache._client.delete = AsyncMock(return_value=1)

        # Should not raise
        await cache.delete("delete_me")

    @pytest.mark.asyncio
    async def test_delete_nonexistent_key(self, cache_instance: RedisCache) -> None:
        """Deleting a non-existent key does not raise error."""
        cache = cache_instance
        cache._client.delete = AsyncMock(return_value=0)

        # Should not raise
        await cache.delete("nonexistent")

    @pytest.mark.asyncio
    async def test_clear_with_keys(self, cache_instance: RedisCache) -> None:
        """Clear removes all keys in namespace."""
        cache = cache_instance
        keys = [
            b"test:cache:key1",
            b"test:cache:key2",
            b"test:cache:key3",
        ]
        cache._client.keys = AsyncMock(return_value=keys)
        cache._client.delete = AsyncMock(return_value=len(keys))

        await cache.clear()
        cache._client.keys.assert_called_once_with("test:cache:*")

    @pytest.mark.asyncio
    async def test_clear_empty_namespace(self, cache_instance: RedisCache) -> None:
        """Clear with no keys does not error."""
        cache = cache_instance
        cache._client.keys = AsyncMock(return_value=[])

        # Should not raise
        await cache.clear()

    @pytest.mark.asyncio
    async def test_set_with_ttl(self, cache_instance: RedisCache) -> None:
        """Can set a value with TTL."""
        cache = cache_instance

        await cache.set("ttl_key", "value", ttl=3600)
        cache._client.set.assert_called_once()
        call_args = cache._client.set.call_args
        assert call_args.kwargs.get("ex") == 3600

    @pytest.mark.asyncio
    async def test_set_without_ttl(self, cache_instance: RedisCache) -> None:
        """Can set a value without TTL (no expiration)."""
        cache = cache_instance

        await cache.set("no_ttl_key", "value")
        cache._client.set.assert_called_once()
        call_args = cache._client.set.call_args
        assert call_args.kwargs.get("ex") is None


# =============================================================================
# Edge Cases & Boundary Conditions
# =============================================================================


class TestCacheEdgeCases:
    """Boundary conditions and unusual inputs."""

    @pytest.mark.asyncio
    async def test_empty_string_key(self, cache_instance: RedisCache) -> None:
        """Can use empty string as key (not recommended but allowed)."""
        cache = cache_instance

        await cache.set("", "empty_key_value")
        cache._client.get = AsyncMock(return_value='"empty_key_value"')

        result = await cache.get("")
        assert result == "empty_key_value"

    @pytest.mark.asyncio
    async def test_special_characters_in_key(self, cache_instance: RedisCache) -> None:
        """Can use special characters in key names."""
        cache = cache_instance
        special_keys = [
            "key:with:colons",
            "key-with-dashes",
            "key_with_underscores",
            "key.with.dots",
            "key/with/slashes",
        ]

        for key in special_keys:
            await cache.set(key, f"value_{key}")
            cache._client.get = AsyncMock(return_value=json.dumps(f"value_{key}"))
            result = await cache.get(key)
            assert result == f"value_{key}"

    @pytest.mark.asyncio
    async def test_very_long_key(self, cache_instance: RedisCache) -> None:
        """Can handle keys up to reasonable length."""
        cache = cache_instance
        long_key = "x" * 200  # Redis supports up to 512MB but we test reasonable limits

        await cache.set(long_key, "value")
        cache._client.get = AsyncMock(return_value='"value"')

        result = await cache.get(long_key)
        assert result == "value"

    @pytest.mark.asyncio
    async def test_unicode_key(self, cache_instance: RedisCache) -> None:
        """Can use Unicode characters in keys."""
        cache = cache_instance
        unicode_key = "测试键:café:emoji👋"

        await cache.set(unicode_key, "unicode_value")
        cache._client.get = AsyncMock(return_value='"unicode_value"')

        result = await cache.get(unicode_key)
        assert result == "unicode_value"

    @pytest.mark.asyncio
    async def test_unicode_value(self, cache_instance: RedisCache) -> None:
        """Can store Unicode string values."""
        cache = cache_instance
        unicode_value = {"text": "Hello 世界 🌍 café"}

        await cache.set("unicode", unicode_value)
        cache._client.get = AsyncMock(return_value=json.dumps(unicode_value))

        result = await cache.get("unicode")
        assert result == unicode_value

    @pytest.mark.asyncio
    async def test_large_value(self, cache_instance: RedisCache) -> None:
        """Can handle large values."""
        cache = cache_instance
        large_list = list(range(10000))

        await cache.set("large", large_list)
        cache._client.get = AsyncMock(return_value=json.dumps(large_list))

        result = await cache.get("large")
        assert result == large_list

    @pytest.mark.asyncio
    async def test_zero_ttl(self, cache_instance: RedisCache) -> None:
        """TTL of 0 removes key immediately (Redis behavior)."""
        cache = cache_instance

        await cache.set("zero_ttl", "value", ttl=0)
        # Redis deletes immediately when ttl=0
        cache._client.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_negative_ttl(self, cache_instance: RedisCache) -> None:
        """Negative TTL treats as immediate deletion (Redis behavior)."""
        cache = cache_instance

        await cache.set("negative_ttl", "value", ttl=-1)
        cache._client.set.assert_called_once()
        call_args = cache._client.set.call_args
        assert call_args.kwargs.get("ex") == -1

    @pytest.mark.asyncio
    async def test_nested_complex_structure(self, cache_instance: RedisCache) -> None:
        """Can handle deeply nested structures."""
        cache = cache_instance
        nested = {
            "level1": {
                "level2": {
                    "level3": {
                        "list": [1, 2, {"nested": "value"}],
                        "tuple": [1, 2, 3],  # Tuples become lists in JSON
                    }
                }
            },
            "arrays": [[1, 2], [3, 4], [5, 6]],
        }

        await cache.set("nested", nested)
        cache._client.get = AsyncMock(return_value=json.dumps(nested))

        result = await cache.get("nested")
        assert result == nested


# =============================================================================
# Key Namespacing Tests
# =============================================================================


class TestCacheNamespacing:
    """Key namespacing functionality."""

    @pytest.mark.asyncio
    async def test_default_namespace(self, mock_redis_client: MagicMock) -> None:
        """Default namespace is applied."""
        cache = RedisCache(namespace=CacheKeyPrefix.DEFAULT)
        cache._client = mock_redis_client

        await cache.set("mykey", "value")
        args = mock_redis_client.set.call_args[0]
        assert args[0] == f"{CacheKeyPrefix.DEFAULT.value}mykey"

    @pytest.mark.asyncio
    async def test_embedding_namespace(self, mock_redis_client: MagicMock) -> None:
        """Embedding namespace prefix is applied."""
        cache = RedisCache(namespace=CacheKeyPrefix.EMBEDDING)
        cache._client = mock_redis_client

        await cache.set("embedding_key", [0.1, 0.2, 0.3])
        args = mock_redis_client.set.call_args[0]
        assert args[0] == f"{CacheKeyPrefix.EMBEDDING.value}embedding_key"

    @pytest.mark.asyncio
    async def test_query_namespace(self, mock_redis_client: MagicMock) -> None:
        """Query namespace prefix is applied."""
        cache = RedisCache(namespace=CacheKeyPrefix.QUERY)
        cache._client = mock_redis_client

        await cache.set("query_hash", {"results": []})
        args = mock_redis_client.set.call_args[0]
        assert args[0] == f"{CacheKeyPrefix.QUERY.value}query_hash"

    @pytest.mark.asyncio
    async def test_generation_namespace(self, mock_redis_client: MagicMock) -> None:
        """Generation namespace prefix is applied."""
        cache = RedisCache(namespace=CacheKeyPrefix.GENERATION)
        cache._client = mock_redis_client

        await cache.set("content_id", "generated text")
        args = mock_redis_client.set.call_args[0]
        assert args[0] == f"{CacheKeyPrefix.GENERATION.value}content_id"

    @pytest.mark.asyncio
    async def test_custom_namespace_string(self, mock_redis_client: MagicMock) -> None:
        """Custom string namespace is applied."""
        cache = RedisCache(namespace="custom:prefix:")
        cache._client = mock_redis_client

        await cache.set("mykey", "value")
        args = mock_redis_client.set.call_args[0]
        assert args[0] == "custom:prefix:mykey"

    @pytest.mark.asyncio
    async def test_clear_respects_namespace(self, cache_instance: RedisCache) -> None:
        """Clear only affects keys in the namespace."""
        cache = cache_instance
        cache._client.keys = AsyncMock(return_value=[b"test:cache:key1"])

        await cache.clear()
        cache._client.keys.assert_called_once_with("test:cache:*")


# =============================================================================
# JSON Serialization Tests
# =============================================================================


class TestCacheSerialization:
    """JSON serialization/deserialization behavior."""

    @pytest.mark.asyncio
    async def test_datetime_serialization(self, cache_instance: RedisCache) -> None:
        """datetime objects cannot be directly serialized."""
        cache = cache_instance
        from datetime import datetime

        now = {"time": datetime.now()}
        with pytest.raises(TypeError):
            await cache.set("datetime", now)

    @pytest.mark.asyncio
    async def test_bytes_serialization(self, cache_instance: RedisCache) -> None:
        """bytes objects cannot be directly serialized."""
        cache = cache_instance
        data = {"binary": b"byte data"}

        with pytest.raises(TypeError):
            await cache.set("bytes", data)

    @pytest.mark.asyncio
    async def test_set_serialization(self, cache_instance: RedisCache) -> None:
        """set objects cannot be directly serialized."""
        cache = cache_instance
        data = {"myset": {1, 2, 3}}

        with pytest.raises(TypeError):
            await cache.set("set", data)

    @pytest.mark.asyncio
    async def test_bytes_deserialization(self, cache_instance: RedisCache) -> None:
        """bytes returned from Redis are decoded."""
        cache = cache_instance
        cache._client.get = AsyncMock(return_value=b'"bytes_value"')

        result = await cache.get("key")
        assert result == "bytes_value"

    @pytest.mark.asyncio
    async def test_invalid_json_deserialization(
        self, cache_instance: RedisCache
    ) -> None:
        """Invalid JSON raises RuntimeError (wrapped from ValueError)."""
        cache = cache_instance
        cache._client.get = AsyncMock(return_value="not valid json {]")

        with pytest.raises(RuntimeError, match="Cache retrieval failed"):
            await cache.get("key")


# =============================================================================
# Connection Management Tests
# =============================================================================


class TestCacheConnection:
    """Connection management and lifecycle."""

    @pytest.mark.asyncio
    async def test_is_connected_property(self, mock_redis_client: MagicMock) -> None:
        """is_connected returns correct state."""
        cache = RedisCache()
        assert not cache.is_connected

        cache._client = mock_redis_client
        assert cache.is_connected

    @pytest.mark.asyncio
    async def test_connect_idempotent(self) -> None:
        """Calling connect when already connected is safe."""
        import redis.asyncio as redis

        with patch.object(redis, "ConnectionPool") as mock_pool:
            with patch.object(redis, "Redis") as mock_redis_class:
                mock_client = MagicMock()
                mock_client.ping = AsyncMock(return_value=True)
                mock_redis_class.return_value = mock_client

                cache = RedisCache()
                await cache.connect()
                assert cache.is_connected

                # Second connect should be idempotent - should not try to connect again
                await cache.connect()
                assert cache.is_connected
                # ConnectionPool should only be called once
                assert mock_pool.call_count == 1

    @pytest.mark.asyncio
    async def test_disconnect_safe_when_not_connected(self) -> None:
        """Disconnect is safe when not connected."""
        cache = RedisCache()
        # Should not raise
        await cache.disconnect()

    @pytest.mark.asyncio
    async def test_async_context_manager(self) -> None:
        """Can use async context manager."""
        import redis.asyncio as redis

        with patch.object(redis, "ConnectionPool"):
            with patch.object(redis, "Redis") as mock_redis_class:
                mock_client = MagicMock()
                mock_client.ping = AsyncMock(return_value=True)
                mock_client.close = AsyncMock(return_value=None)
                mock_redis_class.return_value = mock_client

                async with RedisCache() as cache:
                    assert cache.is_connected

                # After exiting context, close was called
                mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_logs_info(self) -> None:
        """Connection logs appropriate messages."""
        import redis.asyncio as redis

        with patch.object(redis, "ConnectionPool"):
            with patch.object(redis, "Redis") as mock_redis_class:
                mock_client = MagicMock()
                mock_client.ping = AsyncMock(return_value=True)
                mock_redis_class.return_value = mock_client

                with patch("grimoire.core.cache.logger") as mock_logger:
                    cache = RedisCache()
                    await cache.connect()

                    mock_logger.info.assert_called()


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestCacheErrorHandling:
    """Error handling behavior."""

    @pytest.mark.asyncio
    async def test_get_connection_error(self, cache_instance: RedisCache) -> None:
        """RuntimeError raised on get failure."""
        cache = cache_instance
        cache._client.get = AsyncMock(side_effect=Exception("Connection lost"))

        with pytest.raises(RuntimeError, match="Cache retrieval failed"):
            await cache.get("key")

    @pytest.mark.asyncio
    async def test_set_connection_error(self, cache_instance: RedisCache) -> None:
        """RuntimeError raised on set failure."""
        cache = cache_instance
        cache._client.set = AsyncMock(side_effect=Exception("Connection lost"))

        with pytest.raises(RuntimeError, match="Cache storage failed"):
            await cache.set("key", "value")

    @pytest.mark.asyncio
    async def test_delete_connection_error(self, cache_instance: RedisCache) -> None:
        """RuntimeError raised on delete failure."""
        cache_instance._client.delete = AsyncMock(
            side_effect=Exception("Connection lost")
        )

        with pytest.raises(RuntimeError, match="Cache deletion failed"):
            await cache_instance.delete("key")

    @pytest.mark.asyncio
    async def test_clear_connection_error(self, cache_instance: RedisCache) -> None:
        """RuntimeError raised on clear failure."""
        cache_instance._client.keys = AsyncMock(
            side_effect=Exception("Connection lost")
        )

        with pytest.raises(RuntimeError, match="Cache clearing failed"):
            await cache_instance.clear()

    @pytest.mark.asyncio
    async def test_missing_redis_package_import_error(self) -> None:
        """ImportError raised if redis package not installed."""
        with patch.dict(sys.modules, {"redis": None, "redis.asyncio": None}):
            with patch(
                "builtins.__import__",
                side_effect=ImportError("No module named 'redis'"),
            ):
                with patch("grimoire.core.cache.logger"):
                    cache = RedisCache()
                    # The import is deferred to connect time
                    with pytest.raises(ImportError):
                        await cache.connect()


# =============================================================================
# Async Behavior Tests
# =============================================================================


class TestCacheAsyncBehavior:
    """Async-specific behavior tests."""

    @pytest.mark.asyncio
    async def test_auto_connect_on_get(self, cache_instance: RedisCache) -> None:
        """get() auto-connects if not connected."""
        import redis.asyncio as redis

        cache = cache_instance
        # Store original client
        original_client = cache._client
        cache._client = None  # Force disconnected state

        with patch.object(redis, "ConnectionPool"):
            with patch.object(redis, "Redis") as mock_redis_class:
                mock_client = MagicMock()
                mock_client.ping = AsyncMock(return_value=True)
                mock_client.get = AsyncMock(return_value='"value"')
                mock_redis_class.return_value = mock_client

                # After disconnecting, get() should auto-connect
                result = await cache.get("key")
                assert result == "value"
                cache._client = original_client  # Restore for cleanup

    @pytest.mark.asyncio
    async def test_auto_connect_on_set(self, cache_instance: RedisCache) -> None:
        """set() auto-connects if not connected."""
        import redis.asyncio as redis

        cache = cache_instance
        # Store original client
        original_client = cache._client
        cache._client = None  # Force disconnected state

        with patch.object(redis, "ConnectionPool"):
            with patch.object(redis, "Redis") as mock_redis_class:
                mock_client = MagicMock()
                mock_client.ping = AsyncMock(return_value=True)
                mock_client.set = AsyncMock(return_value=True)
                mock_redis_class.return_value = mock_client

                # After disconnecting, set() should auto-connect
                await cache.set("key", "value")
                cache._client = original_client  # Restore for cleanup

    @pytest.mark.asyncio
    async def test_concurrent_operations(self, cache_instance: RedisCache) -> None:
        """Multiple operations can run concurrently."""
        cache = cache_instance
        values = {}

        async def set_and_get(key: str, value: str) -> None:
            await cache.set(key, value)
            cache._client.get = AsyncMock(return_value=json.dumps(value))
            result = await cache.get(key)
            values[key] = result

        await asyncio.gather(
            set_and_get("key1", "value1"),
            set_and_get("key2", "value2"),
            set_and_get("key3", "value3"),
        )

        assert values == {"key1": "value1", "key2": "value2", "key3": "value3"}


# =============================================================================
# TTL and Expiration Tests
# =============================================================================


class TestCacheTTL:
    """TTL and expiration behavior."""

    @pytest.mark.asyncio
    async def test_ttl_positive(self, cache_instance: RedisCache) -> None:
        """Check remaining TTL."""
        cache = cache_instance
        cache._client.ttl = AsyncMock(return_value=3500)

        result = await cache.ttl("key")
        assert result == 3500

    @pytest.mark.asyncio
    async def test_ttl_no_expiration(self, cache_instance: RedisCache) -> None:
        """TTL of -1 means key has no expiration."""
        cache = cache_instance
        cache._client.ttl = AsyncMock(return_value=-1)

        result = await cache.ttl("key")
        assert result == -1

    @pytest.mark.asyncio
    async def test_ttl_key_not_exist(self, cache_instance: RedisCache) -> None:
        """TTL of -2 means key doesn't exist."""
        cache = cache_instance
        cache._client.ttl = AsyncMock(return_value=-2)

        result = await cache.ttl("nonexistent")
        assert result == -2

    @pytest.mark.asyncio
    async def test_exists_true(self, cache_instance: RedisCache) -> None:
        """exists returns True for existing key."""
        cache = cache_instance
        cache._client.exists = AsyncMock(return_value=1)

        result = await cache.exists("key")
        assert result is True

    @pytest.mark.asyncio
    async def test_exists_false(self, cache_instance: RedisCache) -> None:
        """exists returns False for missing key."""
        cache = cache_instance
        cache._client.exists = AsyncMock(return_value=0)

        result = await cache.exists("nonexistent")
        assert result is False


# =============================================================================
# State Management Tests
# =============================================================================


class TestCacheStateManagement:
    """State management tests."""

    @pytest.mark.asyncio
    async def test_overwrite_existing_key(self, cache_instance: RedisCache) -> None:
        """Setting same key overwrites previous value."""
        cache = cache_instance

        await cache.set("overwrite_key", "first")
        cache._client.get = AsyncMock(return_value='"second"')

        await cache.set("overwrite_key", "second")
        result = await cache.get("overwrite_key")
        assert result == "second"

    @pytest.mark.asyncio
    async def test_integer_values_preserved(self, cache_instance: RedisCache) -> None:
        """Integer values are preserved through serialization."""
        cache = cache_instance

        await cache.set("int", 42)
        cache._client.get = AsyncMock(return_value="42")

        # Note: JSON always decodes numbers
        result = await cache.get("int")
        assert result == 42
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_float_values_preserved(self, cache_instance: RedisCache) -> None:
        """Float values are preserved through serialization."""
        cache = cache_instance

        await cache.set("float", 3.14159)
        cache._client.get = AsyncMock(return_value="3.14159")

        result = await cache.get("float")
        assert result == 3.14159


# =============================================================================
# ABC Compliance Tests
# =============================================================================


class TestCacheABCCompliance:
    """Verification that RedisCache properly implements Cache ABC."""

    def test_is_abc_subclass(self) -> None:
        """RedisCache is a subclass of Cache ABC."""
        assert issubclass(RedisCache, Cache)

    def test_implements_required_methods(self) -> None:
        """RedisCache implements all abstract methods."""
        abstract_methods = getattr(Cache, "__abstractmethods__", set())
        for method in abstract_methods:
            assert hasattr(RedisCache, method)
            assert callable(getattr(RedisCache, method))

    def test_can_be_instantiated(self) -> None:
        """RedisCache can be instantiated (unlike abstract Cache)."""
        cache = RedisCache()
        assert isinstance(cache, Cache)

    def test_abstract_cache_cannot_be_instantiated(self) -> None:
        """Abstract Cache cannot be instantiated directly."""
        with pytest.raises(TypeError, match="abstract"):
            Cache()


# =============================================================================
# Configuration Tests
# =============================================================================


class TestCacheConfiguration:
    """Cache configuration options."""

    def test_default_configuration(self) -> None:
        """Default configuration values are reasonable."""
        cache = RedisCache()
        assert cache._host == "localhost"
        assert cache._port == 6379
        assert cache._db == 0
        assert cache._password is None
        assert cache._namespace == CacheKeyPrefix.DEFAULT.value

    def test_custom_configuration(self) -> None:
        """Can configure all options."""
        cache = RedisCache(
            host="redis.example.com",
            port=6380,
            db=5,
            password="secret123",  # noqa: S106 - Test password, not production
            namespace="custom:",
            socket_connect_timeout=10.0,
            socket_timeout=15.0,
            max_connections=50,
        )
        assert cache._host == "redis.example.com"
        assert cache._port == 6380
        assert cache._db == 5
        assert cache._password == "secret123"
        assert cache._namespace == "custom:"
        assert cache._socket_connect_timeout == 10.0
        assert cache._socket_timeout == 15.0
        assert cache._max_connections == 50


# =============================================================================
# CacheKeyPrefix Enum Tests
# =============================================================================


class TestCacheKeyPrefix:
    """CacheKeyPrefix enum tests."""

    def test_enum_values(self) -> None:
        """Enum values are as expected."""
        assert CacheKeyPrefix.EMBEDDING.value == "grimoire:embedding:"
        assert CacheKeyPrefix.QUERY.value == "grimoire:query:"
        assert CacheKeyPrefix.GENERATION.value == "grimoire:generation:"
        assert CacheKeyPrefix.DEFAULT.value == "grimoire:cache:"

    def test_enum_accessible_in_cache(self) -> None:
        """Enum can be used when creating cache."""
        cache = RedisCache(namespace=CacheKeyPrefix.EMBEDDING)
        assert cache._namespace == "grimoire:embedding:"
