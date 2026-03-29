"""Embedding service for Grimoire.

Provides sentence embedding generation using HuggingFace
sentence-transformers library with GPU acceleration support and caching.
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
    """Configuration for the embedding service."""

    model: str = "sentence-transformers/all-mpnet-base-v2"
    fallback_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    device: str = "auto"
    batch_size: int = 32
    cache_ttl: int = 604800  # 7 days
    normalize_embeddings: bool = True


class Embedder:
    """HuggingFace embedding service with caching support."""

    def __init__(
        self,
        config: Optional[EmbeddingConfig] = None,
        cache: Optional[Cache] = None,
    ) -> None:
        """Initialize the embedding service."""
        self.config = config or EmbeddingConfig()
        self._model: Optional[Any] = None
        self._cache = cache
        self._embedding_dim: Optional[int] = None

    def _get_device(self) -> str:
        """Determine the best available compute device."""
        if self.config.device != "auto":
            requested = self.config.device.lower()
            if requested == "cuda":
                import torch

                if torch.cuda.is_available():
                    return "cuda"
                logger.warning("CUDA requested but not available, using CPU")
                return "cpu"
            elif requested == "mps":
                import torch

                if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                    return "mps"
                logger.warning("MPS requested but not available, using CPU")
                return "cpu"
            return requested

        try:
            import torch

            if torch.cuda.is_available():
                logger.info(f"Using CUDA: {torch.cuda.get_device_name(0)}")
                return "cuda"
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                logger.info("Using MPS")
                return "mps"
        except ImportError:
            logger.debug("torch not available")
        except Exception as e:
            logger.warning(f"Error detecting GPU: {e}")

        logger.info("Using CPU")
        return "cpu"

    def _load_model(self) -> Any:
        """Lazy load the sentence-transformers model."""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer

                device = self._get_device()
                logger.info(f"Loading model: {self.config.model} (device={device})")
                try:
                    self._model = SentenceTransformer(self.config.model, device=device)
                except Exception:
                    logger.warning(
                        f"Failed to load {self.config.model}, using fallback"
                    )
                    self._model = SentenceTransformer(
                        self.config.fallback_model, device=device
                    )
                self._embedding_dim = self._model.get_sentence_embedding_dimension()
                logger.info(f"Model loaded, embedding_dim={self._embedding_dim}")
            except Exception as e:
                logger.error(f"Failed to load model: {e}")
                raise RuntimeError(f"Model loading failed: {e}") from e
        return self._model

    @property
    def embedding_dim(self) -> int:
        """Get embedding dimension."""
        if self._embedding_dim is None:
            model = self._load_model()
            self._embedding_dim = model.get_sentence_embedding_dimension()
        return self._embedding_dim

    def _compute_cache_key(self, text: str) -> str:
        """Compute cache key for text."""
        key_content = f"{self.config.model}:{text}"
        return hashlib.sha256(key_content.encode()).hexdigest()

    async def _get_from_cache(self, text: str) -> Optional[List[float]]:
        """Try to get embedding from cache."""
        if self._cache is None:
            return None
        try:
            key = self._compute_cache_key(text)
            cached: Any = await self._cache.get(key)
            if cached is not None and isinstance(cached, list):
                validated: List[float] = [float(x) for x in cached]
                return validated
        except Exception as e:
            logger.warning(f"Cache lookup failed: {e}")
        return None

    async def _save_to_cache(self, text: str, embedding: List[float]) -> None:
        """Save embedding to cache."""
        if self._cache is None:
            return
        try:
            key = self._compute_cache_key(text)
            await self._cache.set(key, embedding, ttl=self.config.cache_ttl)
        except Exception as e:
            logger.warning(f"Cache save failed: {e}")

    async def embed_single(self, text: str) -> List[float]:
        """Embed a single text."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Text must be a non-empty string")
        cached = await self._get_from_cache(text)
        if cached is not None:
            return cached
        try:
            model = self._load_model()
            embedding_arr: np.ndarray = model.encode(
                text,
                convert_to_numpy=True,
                normalize_embeddings=self.config.normalize_embeddings,
            )
            if not isinstance(embedding_arr, np.ndarray):
                raise RuntimeError("Model returned unexpected type")
            result: List[float] = embedding_arr.tolist()
            await self._save_to_cache(text, result)
            return result
        except Exception as e:
            logger.error(f"Embedding failed: {e}")
            raise RuntimeError(f"Failed to generate embedding: {e}") from e

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed multiple texts with batch processing."""
        if not texts:
            raise ValueError("texts must be a non-empty list")
        invalid = [
            (i, t)
            for i, t in enumerate(texts)
            if not isinstance(t, str) or not t.strip()
        ]
        if invalid:
            indices = [i for i, _ in invalid]
            raise ValueError(f"Invalid texts at indices: {indices}")
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
            logger.info(f"Embedding {len(uncached_texts)} texts")
            try:
                model = self._load_model()
                embeddings = model.encode(
                    uncached_texts,
                    batch_size=self.config.batch_size,
                    convert_to_numpy=True,
                    normalize_embeddings=self.config.normalize_embeddings,
                    show_progress_bar=False,
                )
                for idx, orig_idx in enumerate(uncached_indices):
                    emb = embeddings[idx].tolist()
                    results[orig_idx] = emb
                    await self._save_to_cache(uncached_texts[idx], emb)
            except Exception as e:
                logger.error(f"Batch embedding failed: {e}")
                raise RuntimeError(f"Failed to embed texts: {e}") from e
        if None in results:
            missing = [i for i, r in enumerate(results) if r is None]
            raise RuntimeError(f"Failed to generate embeddings for indices: {missing}")
        return [r for r in results if r is not None]

    def get_similarity(self, emb1: List[float], emb2: List[float]) -> float:
        """Compute cosine similarity between two embeddings."""
        if len(emb1) != len(emb2):
            raise ValueError(f"Dimension mismatch: {len(emb1)} vs {len(emb2)}")
        v1 = np.array(emb1)
        v2 = np.array(emb2)
        norm1 = np.linalg.norm(v1)
        norm2 = np.linalg.norm(v2)
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return float(np.dot(v1, v2) / (norm1 * norm2))

    def set_cache(self, cache: Cache) -> None:
        """Set or update cache instance."""
        self._cache = cache
        logger.debug("Cache updated")


class EmbedderFactory:
    """Factory for creating Embedder instances."""

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
        """Create an Embedder instance."""
        if model and model in cls.MODELS:
            model_name = cls.MODELS[model]
        elif model:
            model_name = model
        else:
            model_name = cls.MODELS["general"]
        if cache is None and cache_path is not None:
            cache = DiskCache(path=cache_path)
        config = EmbeddingConfig(
            model=model_name,
            device=device,
            batch_size=batch_size,
            **kwargs,
        )
        return Embedder(config=config, cache=cache)

    @classmethod
    def create_technical(cls, **kwargs: Any) -> Embedder:
        """Create technical embedder."""
        return cls.create(model="technical", **kwargs)

    @classmethod
    def create_fast(cls, **kwargs: Any) -> Embedder:
        """Create fast embedder."""
        return cls.create(model="fast", **kwargs)
