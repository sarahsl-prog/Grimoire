"""Comprehensive tests for the embedding service.

Tests cover:
- Happy path: Basic embedding functionality
- Edge cases: Empty inputs, single items, boundaries
- Input validation: Type checking, error handling
- Error handling: Model loading, GPU detection, caching failures
- Async behavior: Concurrent operations
- State management: Cache invalidation, model switching
"""

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

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_cache_dir():
    """Provide a temporary directory for cache testing."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield Path(tmp_dir)


@pytest.fixture
def disk_cache(temp_cache_dir):
    """Provide a DiskCache instance for testing."""
    return DiskCache(path=temp_cache_dir)


@pytest.fixture
def mock_sentence_transformer():
    """Provide a mock SentenceTransformer for testing."""
    with patch("sentence_transformers.SentenceTransformer") as mock:
        model = Mock()
        model.get_sentence_embedding_dimension.return_value = 768

        # Mock encode to return numpy arrays
        def mock_encode(texts, **kwargs):
            if isinstance(texts, str):
                # Single text
                return np.random.rand(768).astype(np.float32)
            else:
                # Batch of texts
                return np.random.rand(len(texts), 768).astype(np.float32)

        model.encode = mock_encode
        mock.return_value = model
        yield model


@pytest.fixture
def embedder_config():
    """Provide a default embedding configuration."""
    return EmbeddingConfig(
        model="sentence-transformers/all-MiniLM-L6-v2",
        device="cpu",
        batch_size=4,
        cache_ttl=3600,
    )


# =============================================================================
# Test Cache Helper Classes
# =============================================================================


class MockCache(Cache):
    """Mock cache for testing without dependencies."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self.get_calls: int = 0
        self.set_calls: int = 0
        self.delete_calls: int = 0

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
        self.delete_calls += 1
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


# =============================================================================
# Happy Path Tests
# =============================================================================


class TestEmbedderHappyPath:
    """Standard use cases - basic functionality works."""

    @pytest.mark.asyncio
    async def test_embed_single_text(self, mock_sentence_transformer, mock_cache):
        """Embed a single text string."""
        config = EmbeddingConfig(model="test-model", device="cpu")
        embedder = Embedder(config=config, cache=mock_cache)

        result = await embedder.embed_single("Hello world")

        assert isinstance(result, list)
        assert len(result) == 768
        assert all(isinstance(x, float) for x in result)
        mock_cache.set_calls == 1  # Cached after generation

    @pytest.mark.asyncio
    async def test_embed_multiple_texts(self, mock_sentence_transformer, mock_cache):
        """Embed multiple texts in batch."""
        config = EmbeddingConfig(model="test-model", device="cpu", batch_size=4)
        embedder = Embedder(config=config, cache=mock_cache)

        texts = ["First text", "Second text", "Third text"]
        results = await embedder.embed(texts)

        assert len(results) == 3
        for result in results:
            assert len(result) == 768

    @pytest.mark.asyncio
    async def test_embed_returns_list_of_lists(self, mock_sentence_transformer, mock_cache):
        """Verify embed() returns List[List[float]]."""
        embedder = Embedder(cache=mock_cache)

        results = await embedder.embed(["Text one", "Text two"])

        assert isinstance(results, list)
        for emb in results:
            assert isinstance(emb, list)
            assert all(isinstance(x, (int, float)) for x in emb)

    @pytest.mark.asyncio
    async def test_embedding_dim_property(self, mock_sentence_transformer):
        """Verify embedding_dim property returns correct value."""
        embedder = Embedder()

        dim = embedder.embedding_dim

        assert dim == 768
        assert embedder._embedding_dim == 768

    @pytest.mark.asyncio
    async def test_similarity_computation(self, mock_sentence_transformer):
        """Compute similarity between two embeddings."""
        embedder = Embedder()

        # Generate two embeddings
        emb1 = await embedder.embed_single("Machine learning")
        emb2 = await embedder.embed_single("Artificial intelligence")

        # Compute similarity
        similarity = embedder.get_similarity(emb1, emb2)

        assert isinstance(similarity, float)
        assert -1.0 <= similarity <= 1.0


# =============================================================================
# Edge Cases & Boundary Conditions
# =============================================================================


class TestEmbedderEdgeCases:
    """Boundary conditions and unusual inputs."""

    @pytest.mark.asyncio
    async def test_single_text_list(self, mock_sentence_transformer, mock_cache):
        """Embed a list with single element."""
        embedder = Embedder(cache=mock_cache)

        results = await embedder.embed(["Only one text"])

        assert len(results) == 1
        assert len(results[0]) == 768

    @pytest.mark.asyncio
    async def test_empty_text_raises_error(self, mock_sentence_transformer):
        """Empty text should raise ValueError."""
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
    async def test_unicode_and_special_chars(self, mock_sentence_transformer, mock_cache):
        """Handle Unicode and special characters."""
        embedder = Embedder(cache=mock_cache)

        texts = [
            "日本語テキスト",  # Japanese
            "Emojis: 🎉🚀💯 🎨",
            "Math: ∫f(x)dx = ∑ᵢⁿ",
            "Code: def hello(): pass",
        ]

        results = await embedder.embed(texts)

        assert len(results) == 4
        for result in results:
            assert len(result) == 768

    @pytest.mark.asyncio
    async def test_very_long_text(self, mock_sentence_transformer, mock_cache):
        """Handle very long input text."""
        embedder = Embedder(cache=mock_cache)

        long_text = "word " * 10000  # 50k+ characters
        result = await embedder.embed_single(long_text)

        assert len(result) == 768


# =============================================================================
# Input Validation Tests
# =============================================================================


class TestEmbedderInputValidation:
    """Invalid inputs are rejected gracefully."""

    @pytest.mark.asyncio
    async def test_none_text_raises_error(self, mock_sentence_transformer):
        """None as text should raise ValueError."""
        embedder = Embedder()

        with pytest.raises(ValueError, match="non-empty string"):
            await embedder.embed_single(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_non_string_single_raises_error(self, mock_sentence_transformer):
        """Non-string input to embed_single should raise ValueError."""
        embedder = Embedder()

        with pytest.raises(ValueError, match="non-empty string"):
            await embedder.embed_single(123)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_list_with_empty_strings_raises_error(self, mock_sentence_transformer):
        """List containing empty strings should raise ValueError."""
        embedder = Embedder()

        with pytest.raises(ValueError, match="Invalid texts"):
            await embedder.embed(["Valid text", ""])

    @pytest.mark.asyncio
    async def test_list_with_whitespace_strings_raises_error(self, mock_sentence_transformer):
        """List containing whitespace-only strings should raise ValueError."""
        embedder = Embedder()

        with pytest.raises(ValueError, match="Invalid texts"):
            await embedder.embed(["Valid text", "   ", "Another valid"])

    @pytest.mark.asyncio
    async def test_list_with_non_strings_raises_error(self, mock_sentence_transformer):
        """List containing non-strings should raise ValueError."""
        embedder = Embedder()

        with pytest.raises(ValueError, match="Invalid texts"):
            await embedder.embed(["Valid", 123, "Another"])  # type: ignore[list-item]


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestEmbedderErrorHandling:
    """Errors are caught and handled appropriately."""

    @pytest.mark.asyncio
    async def test_missing_sentence_transformers_raises_error(self):
        """Missing sentence-transformers should raise RuntimeError on use."""
        # Mock the import to fail within the embedder module
        with patch.object(
            Embedder, "_load_model",
            side_effect=RuntimeError("Model loading failed: sentence_transformers not installed")
        ):
            embedder = Embedder()
            with pytest.raises(RuntimeError, match="Model loading failed"):
                await embedder.embed_single("test")

    @pytest.mark.asyncio
    async def test_model_loading_failure_raises_runtime_error(self):
        """Model loading failure should raise RuntimeError."""
        with patch("sentence_transformers.SentenceTransformer") as mock:
            mock.side_effect = Exception("Model download failed")

            embedder = Embedder()

            with pytest.raises(RuntimeError, match="Model loading failed"):
                await embedder.embed_single("test")

    @pytest.mark.asyncio
    async def test_embedding_dim_mismatch_raises_error(self):
        """Similarity with mismatched dimensions should raise ValueError."""
        embedder = Embedder()

        with pytest.raises(ValueError, match="dimensions mismatch"):
            embedder.get_similarity([1.0] * 768, [1.0] * 512)


# =============================================================================
# Caching Tests
# =============================================================================


class TestEmbedderCaching:
    """Cache hit/miss behavior and edge cases."""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_value(self, mock_sentence_transformer):
        """Cache hit returns cached embedding without recomputing."""
        cache = MockCache()
        embedder = Embedder(cache=cache)

        # Pre-populate cache
        cached_embedding = [0.1] * 768
        text = "Test text"
        cache_key = embedder._compute_cache_key(text)
        await cache.set(cache_key, cached_embedding)

        # Should return cached value
        result = await embedder.embed_single(text)

        assert result == cached_embedding
        assert cache.get_calls >= 1

    @pytest.mark.asyncio
    async def test_cache_miss_computes_and_stores(self, mock_sentence_transformer, mock_cache):
        """Cache miss computes embedding and stores in cache."""
        embedder = Embedder(cache=mock_cache)

        result = await embedder.embed_single("New text")

        assert result is not None
        assert mock_cache.set_calls >= 1

    @pytest.mark.asyncio
    async def test_batch_partial_cache_hit(self, mock_sentence_transformer):
        """Batch with some cached, some new."""
        cache = MockCache()
        embedder = Embedder(cache=cache)

        # Pre-cache first text
        text1 = "First text"
        cached_emb = [0.1] * 768
        await cache.set(embedder._compute_cache_key(text1), cached_emb)

        # Process both
        results = await embedder.embed([text1, "Second text"])

        assert len(results) == 2
        assert results[0] == cached_emb

    @pytest.mark.asyncio
    async def test_cache_ttl_respected(self, mock_sentence_transformer, temp_cache_dir):
        """Cache TTL configuration is respected."""
        cache = DiskCache(path=temp_cache_dir)
        config = EmbeddingConfig(cache_ttl=7200)
        embedder = Embedder(config=config, cache=cache)

        assert embedder.config.cache_ttl == 7200


# =============================================================================
# GPU/Device Detection Tests
# =============================================================================


class TestEmbedderDeviceDetection:
    """GPU auto-detection and fallback behavior."""

    def test_auto_device_detection(self):
        """Auto device detection returns appropriate device."""
        embedder = Embedder()
        device = embedder._get_device()

        assert device in ["cuda", "mps", "cpu"]

    def test_explicit_cpu_device(self):
        """Explicit CPU device selection works."""
        config = EmbeddingConfig(device="cpu")
        embedder = Embedder(config=config)

        device = embedder._get_device()
        assert device == "cpu"

    @pytest.mark.skipif(
        not __import__("torch").cuda.is_available(),
        reason="CUDA not available",
    )
    def test_explicit_cuda_device(self):
        """Explicit CUDA device selection works when available."""
        config = EmbeddingConfig(device="cuda")
        embedder = Embedder(config=config)

        device = embedder._get_device()
        assert device == "cuda"

    def test_unavailable_cuda_falls_back_to_cpu(self):
        """CUDA request falls back to CPU when unavailable."""
        config = EmbeddingConfig(device="cuda")
        embedder = Embedder(config=config)

        with patch("torch.cuda.is_available", return_value=False):
            device = embedder._get_device()
            assert device == "cpu"


# =============================================================================
# Batch Processing Tests
# =============================================================================


class TestEmbedderBatchProcessing:
    """Batch processing with different sizes and configurations."""

    @pytest.mark.asyncio
    async def test_large_batch_processes_correctly(self, mock_sentence_transformer):
        """Large batches are processed in chunks."""
        config = EmbeddingConfig(batch_size=4, device="cpu")
        embedder = Embedder(config=config)

        # 10 texts with batch size 4 = 3 batches
        texts = [f"Text {i}" for i in range(10)]
        results = await embedder.embed(texts)

        assert len(results) == 10
        for result in results:
            assert len(result) == 768

    @pytest.mark.asyncio
    async def test_exact_batch_size(self, mock_sentence_transformer):
        """Batch exactly matching batch_size."""
        config = EmbeddingConfig(batch_size=5, device="cpu")
        embedder = Embedder(config=config)

        texts = [f"Text {i}" for i in range(5)]
        results = await embedder.embed(texts)

        assert len(results) == 5

    @pytest.mark.asyncio
    async def test_batch_size_one(self, mock_sentence_transformer):
        """Batch size of 1 processes correctly."""
        config = EmbeddingConfig(batch_size=1, device="cpu")
        embedder = Embedder(config=config)

        texts = ["A", "B", "C"]
        results = await embedder.embed(texts)

        assert len(results) == 3


# =============================================================================
# State Management Tests
# =============================================================================


class TestEmbedderStateManagement:
    """State management and lifecycle."""

    def test_embedder_initialization_defaults(self):
        """Embedder initializes with sensible defaults."""
        embedder = Embedder()

        assert embedder.config.model == "sentence-transformers/all-mpnet-base-v2"
        assert embedder.config.device == "auto"
        assert embedder.config.batch_size == 32
        assert embedder._model is None  # Lazy loading

    def test_embedder_with_custom_config(self):
        """Embedder accepts custom configuration."""
        config = EmbeddingConfig(
            model="custom-model",
            device="cpu",
            batch_size=64,
        )
        embedder = Embedder(config=config)

        assert embedder.config.model == "custom-model"
        assert embedder.config.batch_size == 64

    def test_set_cache_updates_instance(self):
        """set_cache updates the cache instance."""
        cache1 = MockCache()
        cache2 = MockCache()

        embedder = Embedder(cache=cache1)
        assert embedder._cache is cache1

        embedder.set_cache(cache2)
        assert embedder._cache is cache2


# =============================================================================
# Factory Tests
# =============================================================================


class TestEmbedderFactory:
    """Factory methods for creating embedders."""

    def test_factory_create_default(self):
        """Factory creates embedder with defaults."""
        with patch("sentence_transformers.SentenceTransformer"):
            embedder = EmbedderFactory.create()

            assert isinstance(embedder, Embedder)
            assert embedder.config.model == EmbedderFactory.MODELS["general"]

    def test_factory_create_preset(self):
        """Factory creates embedder with preset model."""
        with patch("sentence_transformers.SentenceTransformer"):
            embedder = EmbedderFactory.create(model="fast")

            assert embedder.config.model == EmbedderFactory.MODELS["fast"]

    def test_factory_create_custom_model(self):
        """Factory creates embedder with custom model name."""
        with patch("sentence_transformers.SentenceTransformer"):
            embedder = EmbedderFactory.create(model="custom/model-name")

            assert embedder.config.model == "custom/model-name"

    def test_factory_create_technical(self):
        """Factory creates technical content embedder."""
        with patch("sentence_transformers.SentenceTransformer"):
            embedder = EmbedderFactory.create_technical()

            assert embedder.config.model == EmbedderFactory.MODELS["technical"]

    def test_factory_create_fast(self):
        """Factory creates fast embedder."""
        with patch("sentence_transformers.SentenceTransformer"):
            embedder = EmbedderFactory.create_fast()

            assert embedder.config.model == EmbedderFactory.MODELS["fast"]

    def test_factory_with_cache_path(self, temp_cache_dir):
        """Factory creates embedder with disk cache path."""
        with patch("sentence_transformers.SentenceTransformer"):
            embedder = EmbedderFactory.create(cache_path=temp_cache_dir)

            assert embedder._cache is not None
            assert isinstance(embedder._cache, DiskCache)


# =============================================================================
# Similarity Computation Tests
# =============================================================================


class TestEmbedderSimilarity:
    """Cosine similarity computation."""

    def test_identical_embeddings_have_similarity_one(self):
        """Identical embeddings have similarity of 1.0."""
        embedder = Embedder()

        emb = [0.5] * 768
        similarity = embedder.get_similarity(emb, emb.copy())

        assert pytest.approx(similarity) == 1.0

    def test_orthogonal_embeddings_can_be_computed(self):
        """Similarity between distinct embeddings is valid."""
        embedder = Embedder()

        emb1 = [1.0] + [0.0] * 767  # Unit vector along first dimension
        emb2 = [0.0] * 767 + [1.0]  # Unit vector along last dimension

        similarity = embedder.get_similarity(emb1, emb2)

        # Orthogonal vectors should have similarity 0
        assert pytest.approx(similarity, abs=1e-10) == 0.0

    def test_zero_embedding_returns_zero_similarity(self):
        """Zero vectors return zero similarity."""
        embedder = Embedder()

        emb1 = [0.0] * 768
        emb2 = [1.0, 0.0] * 384

        similarity = embedder.get_similarity(emb1, emb2)

        assert similarity == 0.0


# =============================================================================
# DiskCache Specific Tests
# =============================================================================


class TestDiskCache:
    """DiskCache implementation specific tests."""

    @pytest.mark.asyncio
    async def test_disk_cache_set_get(self, temp_cache_dir):
        """Basic set and get operations."""
        cache = DiskCache(path=temp_cache_dir)

        await cache.set("key1", "value1")
        result = await cache.get("key1")

        assert result == "value1"

    @pytest.mark.asyncio
    async def test_disk_cache_ttl_expires(self, temp_cache_dir):
        """Values with TTL expire."""
        import asyncio

        cache = DiskCache(path=temp_cache_dir)

        await cache.set("key", "value", ttl=1)  # 1 second TTL
        assert await cache.get("key") == "value"

        await asyncio.sleep(1.1)
        assert await cache.get("key") is None

    @pytest.mark.asyncio
    async def test_disk_cache_delete(self, temp_cache_dir):
        """Delete removes value."""
        cache = DiskCache(path=temp_cache_dir)

        await cache.set("key", "value")
        await cache.delete("key")

        assert await cache.get("key") is None

    @pytest.mark.asyncio
    async def test_disk_cache_clear(self, temp_cache_dir):
        """Clear removes all values."""
        cache = DiskCache(path=temp_cache_dir)

        await cache.set("key1", "value1")
        await cache.set("key2", "value2")
        await cache.clear()

        assert await cache.get("key1") is None
        assert await cache.get("key2") is None

    @pytest.mark.asyncio
    async def test_disk_cache_json_serialization(self, temp_cache_dir):
        """Complex values are JSON serialized."""
        cache = DiskCache(path=temp_cache_dir)

        complex_value = {"embedding": [0.1, 0.2, 0.3], "text": "test"}
        await cache.set("key", complex_value)
        result = await cache.get("key")

        assert result == complex_value


# =============================================================================
# Integration Tests
# =============================================================================


@pytest.mark.slow
class TestEmbedderIntegration:
    """Integration tests with real sentence-transformers (slow)."""

    @pytest.mark.asyncio
    async def test_real_embedding_generation(self):
        """Test with actual sentence-transformers model."""
        pytest.importorskip("sentence_transformers")

        config = EmbeddingConfig(model="sentence-transformers/all-MiniLM-L6-v2")
        embedder = Embedder(config=config)

        results = await embedder.embed([
            "Hello world",
            "Machine learning is fascinating",
        ])

        assert len(results) == 2
        assert len(results[0]) == 384  # MiniLM-L6 has 384 dimensions

    @pytest.mark.asyncio
    async def test_real_caching_behavior(self, temp_cache_dir):
        """Test actual caching with real models."""
        pytest.importorskip("sentence_transformers")

        cache = DiskCache(path=temp_cache_dir)
        config = EmbeddingConfig(model="sentence-transformers/all-MiniLM-L6-v2")
        embedder = Embedder(config=config, cache=cache)

        text = "This is a test embedding"

        # First call - computes
        result1 = await embedder.embed_single(text)

        # Second call - should hit cache
        result2 = await embedder.embed_single(text)

        # Results should be identical (from cache)
        assert result1 == result2
