"""Tests for the embedding service."""

import hashlib
import tempfile
from pathlib import Path
from typing import Any, Generator, List, Optional
from unittest.mock import Mock, patch

import numpy as np
import pytest

from grimoire.core.cache import Cache, DiskCache
from grimoire.core.embedder import (
    Embedder,
    EmbedderFactory,
    EmbeddingConfig,
)


class MockCache(Cache):
    """Mock cache for testing without dependencies."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self.get_calls: int = 0
        self.set_calls: int = 0

    async def get(self, key: str) -> Optional[Any]:
        self.get_calls += 1
        return self._data.get(key)

    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
    ) -> None:
        self.set_calls += 1
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def clear(self) -> None:
        self._data.clear()


@pytest.fixture
def temp_cache_dir() -> Generator[Path, None, None]:
    """Provide a temporary directory for cache testing."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield Path(tmp_dir)


@pytest.fixture
def disk_cache(temp_cache_dir: Path) -> DiskCache:
    """Provide a DiskCache instance for testing."""
    return DiskCache(path=temp_cache_dir)


@pytest.fixture
def mock_cache() -> MockCache:
    """Provide a mock cache for testing."""
    return MockCache()


@pytest.fixture
def mock_sentence_transformer() -> Generator[Mock, None, None]:
    """Provide a mock SentenceTransformer."""
    with patch("sentence_transformers.SentenceTransformer") as mock:
        model = Mock()
        model.get_sentence_embedding_dimension.return_value = 768

        def mock_encode(texts: Any, **kwargs: Any) -> np.ndarray:
            if isinstance(texts, str):
                return np.random.rand(768).astype(np.float32)
            else:
                return np.random.rand(len(texts), 768).astype(np.float32)

        model.encode = mock_encode
        mock.return_value = model
        yield model


class TestEmbedderHappyPath:
    """Standard use cases."""

    @pytest.mark.asyncio
    async def test_embed_single(
        self, mock_sentence_transformer: Mock, mock_cache: MockCache
    ) -> None:
        """Embed a single text."""
        config = EmbeddingConfig(model="test-model", device="cpu")
        embedder = Embedder(config=config, cache=mock_cache)
        result = await embedder.embed_single("Hello world")
        assert isinstance(result, list)
        assert len(result) == 768
        assert all(isinstance(x, float) for x in result)

    @pytest.mark.asyncio
    async def test_embed_multiple(
        self, mock_sentence_transformer: Mock, mock_cache: MockCache
    ) -> None:
        """Embed multiple texts."""
        config = EmbeddingConfig(model="test-model", device="cpu", batch_size=4)
        embedder = Embedder(config=config, cache=mock_cache)
        texts = ["First", "Second", "Third"]
        results = await embedder.embed(texts)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_embedding_dim(self, mock_sentence_transformer: Mock) -> None:
        """Get embedding dimension."""
        embedder = Embedder()
        assert embedder.embedding_dim == 768

    @pytest.mark.asyncio
    async def test_similarity(self, mock_sentence_transformer: Mock) -> None:
        """Compute similarity."""
        embedder = Embedder()
        emb1 = await embedder.embed_single("ML")
        emb2 = await embedder.embed_single("AI")
        similarity = embedder.get_similarity(emb1, emb2)
        assert -1.0 <= similarity <= 1.0


class TestEmbedderEdgeCases:
    """Edge cases."""

    @pytest.mark.asyncio
    async def test_empty_text_raises_error(
        self, mock_sentence_transformer: Mock
    ) -> None:
        """Empty text raises error."""
        embedder = Embedder()
        with pytest.raises(ValueError, match="non-empty string"):
            await embedder.embed_single("")

    @pytest.mark.asyncio
    async def test_whitespace_text_raises_error(
        self, mock_sentence_transformer: Mock
    ) -> None:
        """Whitespace text raises error."""
        embedder = Embedder()
        with pytest.raises(ValueError, match="non-empty string"):
            await embedder.embed_single("   ")

    @pytest.mark.asyncio
    async def test_empty_list_raises_error(
        self, mock_sentence_transformer: Mock
    ) -> None:
        """Empty list raises error."""
        embedder = Embedder()
        with pytest.raises(ValueError, match="non-empty list"):
            await embedder.embed([])

    @pytest.mark.asyncio
    async def test_list_with_empty_raises_error(
        self, mock_sentence_transformer: Mock
    ) -> None:
        """List with empty strings raises error."""
        embedder = Embedder()
        with pytest.raises(ValueError, match="Invalid texts"):
            await embedder.embed(["Valid", ""])

    @pytest.mark.asyncio
    async def test_unicode_chars(self, mock_sentence_transformer: Mock, mock_cache: MockCache) -> None:
        """Handle Unicode."""
        embedder = Embedder(cache=mock_cache)
        texts = ["日本語", "🎉", "∫f(x)dx"]
        results = await embedder.embed(texts)
        assert len(results) == 3


class TestEmbedderCaching:
    """Caching behavior."""

    @pytest.mark.asyncio
    async def test_cache_hit(self, mock_sentence_transformer: Mock) -> None:
        """Cache hit returns cached value."""
        cache = MockCache()
        embedder = Embedder(cache=cache)
        cached = [0.1] * 768
        text = "Test"
        key = embedder._compute_cache_key(text)
        await cache.set(key, cached)
        result = await embedder.embed_single(text)
        assert result == cached

    @pytest.mark.asyncio
    async def test_cache_miss_computes(self, mock_sentence_transformer: Mock, mock_cache: MockCache) -> None:
        """Cache miss computes embedding."""
        embedder = Embedder(cache=mock_cache)
        result = await embedder.embed_single("New text")
        assert result is not None
        assert mock_cache.set_calls >= 1


class TestEmbedderDeviceDetection:
    """Device detection."""

    def test_auto_device(self) -> None:
        """Auto device detection."""
        embedder = Embedder()
        device = embedder._get_device()
        assert device in ["cuda", "mps", "cpu"]

    def test_explicit_cpu(self) -> None:
        """Explicit CPU."""
        config = EmbeddingConfig(device="cpu")
        embedder = Embedder(config=config)
        assert embedder._get_device() == "cpu"

    @pytest.mark.skipif(not __import__("torch").cuda.is_available(), reason="CUDA not available")
    def test_explicit_cuda(self) -> None:
        """Explicit CUDA."""
        config = EmbeddingConfig(device="cuda")
        embedder = Embedder(config=config)
        assert embedder._get_device() == "cuda"


class TestEmbedderFactory:
    """Factory methods."""

    def test_create_default(self) -> None:
        """Default embedder."""
        with patch("sentence_transformers.SentenceTransformer"):
            embedder = EmbedderFactory.create()
            assert embedder.config.model == EmbedderFactory.MODELS["general"]

    def test_create_preset(self) -> None:
        """Preset embedder."""
        with patch("sentence_transformers.SentenceTransformer"):
            embedder = EmbedderFactory.create(model="fast")
            assert embedder.config.model == EmbedderFactory.MODELS["fast"]

    @pytest.mark.asyncio
    async def test_real_embedding(self) -> None:
        """Real embedding test."""
        pytest.importorskip("sentence_transformers")
        config = EmbeddingConfig(model="sentence-transformers/all-MiniLM-L6-v2")
        embedder = Embedder(config=config)
        texts = ["Hello", "World"]
        results = await embedder.embed(texts)
        assert len(results) == 2
        assert len(results[0]) == 384


class TestDiskCache:
    """DiskCache tests."""

    @pytest.mark.asyncio
    async def test_set_get(self, temp_cache_dir: Path) -> None:
        """Basic set/get."""
        cache = DiskCache(path=temp_cache_dir)
        await cache.set("key1", "value1")
        assert await cache.get("key1") == "value1"

    @pytest.mark.asyncio
    async def test_json_serialization(self, temp_cache_dir: Path) -> None:
        """JSON serialization."""
        cache = DiskCache(path=temp_cache_dir)
        value = {"embedding": [0.1, 0.2], "text": "test"}
        await cache.set("key", value)
        assert await cache.get("key") == value
