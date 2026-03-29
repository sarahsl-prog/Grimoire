"""Embedding service for Grimoire.

This module provides sentence embedding generation using HuggingFace
transformers with GPU acceleration support and caching.

Features:
- Configurable embedding models (default: all-mpnet-base-v2)
- GPU auto-detection (CUDA, MPS) with CPU fallback
- Batch processing with progress logging
- Redis/Disk caching support
- Per-index model support (technical vs general indices)
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Union

import numpy as np
from loguru import logger

from grimoire.core.cache import Cache, DiskCache


@dataclass
class EmbeddingConfig:
    """Configuration for the embedding service.

    Attributes:
        model: HuggingFace model name for embeddings.
        fallback_model: Model to use if primary fails to load.
        device: Device to use ("cuda", "mps", "cpu", or "auto").
        batch_size: Number of texts to process per batch.
        cache_ttl: Time-to-live for embedding cache in seconds.
        normalize_embeddings: Whether to L2-normalize embeddings.
    """

    model: str = "sentence-transformers/all-mpnet-base-v2"
    fallback_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    device: str = "auto"
    batch_size: int = 32
    cache_ttl: int = 604800  # 7 days
    normalize_embeddings: bool = True


class Embedder:
    """HuggingFace embedding service with caching support.

    Provides text embedding generation using sentence-transformers with
    automatic device selection, batch processing, and caching.

    Args:
        config: Embedding configuration.
        cache: Optional cache instance for storing computed embeddings.

    Example:
        ```python
        embedder = Embedder()
        embeddings = await embedder.embed(["Hello world", "Another text"])
        print(f"Generated {len(embeddings)} embeddings")
        ```
    """

    def __init__(
        self,
        config: Optional[EmbeddingConfig] = None,
        cache: Optional[Cache] = None,
    ) -> None:
        """Initialize the embedding service.

        Args:
            config: Embedding configuration. Uses defaults if None.
            cache: Cache instance for storing embeddings. Uses DiskCache
                if None and caching is available.
        """
        self.config = config or EmbeddingConfig()
        self._model: Optional[Any] = None  # sentence_transformers model
        self._cache = cache
        self._embedding_dim: Optional[int] = None

    def _get_device(self) -> str:
        """Determine the best available compute device.

        Returns:
            Device string ("cuda", "mps", or "cpu") based on availability.
        """
        if self.config.device != "auto":
            # Validate requested device
            requested = self.config.device.lower()
            if requested == "cuda":
                import torch

                if torch.cuda.is_available():
                    return "cuda"
                logger.warning("CUDA requested but not available, falling back to CPU")
                return "cpu"
            elif requested == "mps":
                import torch

                if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                    return "mps"
                logger.warning("MPS requested but not available, falling back to CPU")
                return "cpu"
            return requested

        # Auto-detect best available device
        try:
            import torch

            if torch.cuda.is_available():
                logger.info(f"Using CUDA device: {torch.cuda.get_device_name(0)}")
                return "cuda"
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                logger.info("Using MPS (Metal Performance Shaders) device")
                return "mps"
        except ImportError:
            logger.debug("torch not available for device detection")
        except Exception as e:
            logger.warning(f"Error detecting GPU: {e}")

        logger.info("Using CPU for embeddings")
        return "cpu"

    def _load_model(self) -> Any:
        """Lazy load the sentence-transformers model.

        Returns:
            Loaded sentence-transformers model.

        Raises:
            RuntimeError: If model loading fails.
        """
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer

                device = self._get_device()
                model_name = self.config.model

                logger.info(f"Loading embedding model: {model_name} (device={device})")

                try:
                    self._model = SentenceTransformer(model_name, device=device)
                except Exception as e:
                    logger.warning(
                        f"Failed to load {model_name}, trying fallback: "
                        f"{self.config.fallback_model}"
                    )
                    self._model = SentenceTransformer(
                        self.config.fallback_model, device=device
                    )

                self._embedding_dim = self._model.get_sentence_embedding_dimension()
                logger.info(
                    f"Model loaded successfully: embedding_dim={self._embedding_dim}"
                )

            except Exception as e:
                logger.error(f"Failed to load embedding model: {e}")
                raise RuntimeError(f"Model loading failed: {e}") from e

        return self._model

    @property
    def embedding_dim(self) -> int:
        """Get the embedding dimension of the loaded model.

        Returns:
            Size of embedding vectors (e.g., 768 for all-mpnet-base-v2).

        Raises:
            RuntimeError: If model is not loaded.
        """
        if self._embedding_dim is None:
            model = self._load_model()
            self._embedding_dim = model.get_sentence_embedding_dimension()
        return self._embedding_dim

    def _compute_cache_key(self, text: str) -> str:
        """Compute cache key for a text.

        Uses SHA-256 hash of model name + text for cache key uniqueness.

        Args:
            text: Input text to hash.

        Returns:
            Cache key string.
        """
        # Include model name in key to invalidate on model change
        key_content = f"{self.config.model}:{text}"
        return hashlib.sha256(key_content.encode()).hexdigest()

    async def _get_from_cache(self, text: str) -> Optional[List[float]]:
        """Try to get embedding from cache.

        Args:
            text: Input text to lookup.

        Returns:
            Cached embedding if found, None otherwise.
        """
        if self._cache is None:
            return None

        try:
            key = self._compute_cache_key(text)
            cached = await self._cache.get(key)
            if cached is not None:
                logger.debug(f"Cache hit for text hash: {key[:8]}")
                return cached
        except Exception as e:
            logger.warning(f"Cache lookup failed: {e}")

        return None

    async def _save_to_cache(self, text: str, embedding: List[float]) -> None:
        """Save embedding to cache.

        Args:
            text: Input text.
            embedding: Embedding vector to cache.
        """
        if self._cache is None:
            return

        try:
            key = self._compute_cache_key(text)
            await self._cache.set(key, embedding, ttl=self.config.cache_ttl)
            logger.debug(f"Cache saved for text hash: {key[:8]}")
        except Exception as e:
            logger.warning(f"Cache save failed: {e}")

    async def embed_single(self, text: str) -> List[float]:
        """Embed a single text string.

        Args:
            text: Text to embed.

        Returns:
            Embedding vector as list of floats.

        Raises:
            ValueError: If text is empty or invalid.
            RuntimeError: If embedding generation fails.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Text must be a non-empty string")

        # Check cache first
        cached = await self._get_from_cache(text)
        if cached is not None:
            return cached

        # Generate embedding
        try:
            model = self._load_model()
            embedding = model.encode(
                text,
                convert_to_numpy=True,
                normalize_embeddings=self.config.normalize_embeddings,
            )
            result = embedding.tolist()

            # Cache the result
            await self._save_to_cache(text, result)

            return result

        except Exception as e:
            logger.error(f"Embedding generation failed: {e}")
            raise RuntimeError(f"Failed to generate embedding: {e}") from e

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed multiple texts with batch processing.

        Processes texts in batches according to config.batch_size.
        Uses caching to avoid recomputing embeddings.

        Args:
            texts: List of texts to embed.

        Returns:
            List of embedding vectors (one per input text).

        Raises:
            ValueError: If texts is empty or contains invalid values.
            RuntimeError: If batch processing fails.

        Example:
            ```python
            texts = ["First document", "Second document", "Third document"]
            embeddings = await embedder.embed(texts)
            print(f"Generated {len(embeddings)} embeddings")
            ```
        """
        if not texts:
            raise ValueError("texts must be a non-empty list")

        # Validate all texts (check for non-strings or empty/whitespace-only)
        invalid_items = [
            (i, t) for i, t in enumerate(texts)
            if not isinstance(t, str) or not t.strip()
        ]
        if invalid_items:
            indices = [i for i, _ in invalid_items]
            raise ValueError(f"Invalid texts at indices: {indices}")

        # Check for cached embeddings
        results: List[Optional[List[float]]] = [None] * len(texts)
        uncached_indices: List[int] = []
        uncached_texts: List[str] = []

        for i, text in enumerate(texts):
            cached = await self._get_from_cache(text)
            if cached is not None:
                results[i] = cached
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)

        if uncached_texts:
            logger.info(
                f"Embedding {len(uncached_texts)} texts "
                f"(batch_size={self.config.batch_size})"
            )

            try:
                model = self._load_model()

                # Process in batches with progress logging
                batch_embeddings: List[List[float]] = []
                total_batches = (len(uncached_texts) + self.config.batch_size - 1) // (
                    self.config.batch_size
                )

                for batch_idx in range(0, len(uncached_texts), self.config.batch_size):
                    batch = uncached_texts[batch_idx : batch_idx + self.config.batch_size]
                    current_batch = batch_idx // self.config.batch_size + 1

                    logger.debug(
                        f"Processing batch {current_batch}/{total_batches} "
                        f"({len(batch)} texts)"
                    )

                    embeddings = model.encode(
                        batch,
                        batch_size=len(batch),
                        convert_to_numpy=True,
                        normalize_embeddings=self.config.normalize_embeddings,
                        show_progress_bar=False,  # We log progress manually
                    )

                    for emb in embeddings:
                        batch_embeddings.append(emb.tolist())

                # Cache and place results
                for idx, orig_idx in enumerate(uncached_indices):
                    embedding = batch_embeddings[idx]
                    results[orig_idx] = embedding
                    await self._save_to_cache(uncached_texts[idx], embedding)

                logger.info(f"Successfully embedded {len(uncached_texts)} texts")

            except Exception as e:
                logger.error(f"Batch embedding failed: {e}")
                raise RuntimeError(f"Failed to embed texts: {e}") from e

        # Verify all results are present
        if None in results:
            missing = [i for i, r in enumerate(results) if r is None]
            raise RuntimeError(f"Failed to generate embeddings for indices: {missing}")

        return results  # type: ignore[return-value]

    def get_similarity(
        self,
        embedding1: List[float],
        embedding2: List[float],
    ) -> float:
        """Compute cosine similarity between two embeddings.

        Args:
            embedding1: First embedding vector.
            embedding2: Second embedding vector.

        Returns:
            Cosine similarity score between -1 and 1.

        Raises:
            ValueError: If embeddings have different dimensions.
        """
        if len(embedding1) != len(embedding2):
            raise ValueError(
                f"Embedding dimensions mismatch: {len(embedding1)} vs {len(embedding2)}"
            )

        vec1 = np.array(embedding1)
        vec2 = np.array(embedding2)

        # Cosine similarity = dot(a, b) / (||a|| * ||b||)
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return float(np.dot(vec1, vec2) / (norm1 * norm2))

    def set_cache(self, cache: Cache) -> None:
        """Set or update the cache instance.

        Args:
            cache: New cache instance to use.
        """
        self._cache = cache
        logger.debug("Cache instance updated")


class EmbedderFactory:
    """Factory for creating Embedder instances with different configurations.

    Supports creating embedders optimized for different content types.

    Example:
        ```python
        # General purpose embedder (default)
        embedder = EmbedderFactory.create()

        # Technical documents embedder (better for code/papers)
        technical = EmbedderFactory.create_technical()

        # Custom embedder with specific cache
        custom = EmbedderFactory.create(
            model="BAAI/bge-base-en-v1.5",
            cache=my_redis_cache
        )
        ```
    """

    # Pre-configured model presets
    MODELS = {
        "general": "sentence-transformers/all-mpnet-base-v2",
        "technical": "BAAI/bge-base-en-v1.5",
        "fast": "sentence-transformers/all-MiniLM-L6-v2",
        "multilingual": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    }

    @classmethod
    def create(
        cls,
        model: Optional[str] = None,
        device: str = "auto",
        batch_size: int = 32,
        cache: Optional[Cache] = None,
        cache_path: Optional[Union[str, Path]] = None,
        **kwargs: Any,
    ) -> Embedder:
        """Create an Embedder instance.

        Args:
            model: Model name or preset ("general", "technical", "fast").
                Uses "general" if None.
            device: Device to use ("cuda", "mps", "cpu", "auto").
            batch_size: Batch size for processing.
            cache: Cache instance to use. Creates DiskCache if None.
            cache_path: Path for disk cache (if cache is None).
            **kwargs: Additional configuration options.

        Returns:
            Configured Embedder instance.
        """
        # Resolve model preset
        if model and model in cls.MODELS:
            model_name = cls.MODELS[model]
        elif model:
            model_name = model
        else:
            model_name = cls.MODELS["general"]

        # Create cache if not provided
        if cache is None and cache_path is not None:
            cache = DiskCache(path=cache_path)

        # Build configuration
        config = EmbeddingConfig(
            model=model_name,
            device=device,
            batch_size=batch_size,
            **kwargs,
        )

        return Embedder(config=config, cache=cache)

    @classmethod
    def create_technical(cls, **kwargs: Any) -> Embedder:
        """Create an embedder optimized for technical content.

        Uses BAAI/bge-base-en-v1.5 which performs well on technical papers
        and code documentation.

        Args:
            **kwargs: Additional configuration options.

        Returns:
            Embedder configured for technical content.
        """
        return cls.create(model="technical", **kwargs)

    @classmethod
    def create_fast(cls, **kwargs: Any) -> Embedder:
        """Create a fast, lightweight embedder.

        Uses all-MiniLM-L6-v2 which is smaller and faster but slightly
        less accurate than the default model.

        Args:
            **kwargs: Additional configuration options.

        Returns:
            Embedder optimized for speed.
        """
        return cls.create(model="fast", **kwargs)
