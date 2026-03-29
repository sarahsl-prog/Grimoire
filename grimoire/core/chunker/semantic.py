"""Semantic chunking using embedding-based boundary detection.

This module implements semantic chunking which splits documents at
points of low semantic similarity, preserving natural boundaries in
the text. This is ideal for mixed documents where topic boundaries
are more important than fixed character counts.
"""

from typing import Any, List, Optional

import numpy as np
from pydantic import Field, field_validator

from grimoire.core.chunker.base import Chunk, ChunkConfig, Chunker, ChunkingStrategy


class SemanticChunkConfig(ChunkConfig):
    """Configuration for semantic chunking.

    Extends base ChunkConfig with semantic-specific parameters.

    Attributes:
        threshold: Cosine similarity threshold for boundary detection.
            Lower values create more chunks (stricter boundaries).
        min_chunk_size: Minimum chunk size in characters.
        sentence_window: Number of sentences to consider for embedding.

    Example:
        ```python
        config = SemanticChunkConfig(
            chunk_size=1000,
            threshold=0.5,
            min_chunk_size=100,
        )
        ```
    """

    strategy: ChunkingStrategy = ChunkingStrategy.SEMANTIC

    threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Cosine similarity threshold for split boundaries (lower = more splits)",
    )
    min_chunk_size: int = Field(
        default=100, ge=10, description="Minimum chunk size in characters"
    )
    sentence_window: int = Field(
        default=3, ge=1, description="Sentences to embed for boundary detection"
    )

    @field_validator("min_chunk_size")
    @classmethod
    def min_chunk_size_reasonable(cls, v: int) -> int:
        """Validate minimum chunk size is reasonable."""
        if v < 10:
            raise ValueError("min_chunk_size must be at least 10 characters")
        return v


class SemanticChunker(Chunker):
    """Semantic chunking using embedding-based boundary detection.

    This chunker detects topic boundaries by comparing sentence embeddings.
    When the cosine similarity between consecutive sentences drops below
    the threshold, a new chunk is created. This preserves semantic coherence
    within each chunk.

    Note:
        Requires sentence-transformers for embeddings. Falls back to
        simple splitting if embeddings are not available.

    Example:
        ```python
        config = SemanticChunkConfig(threshold=0.5)
        chunker = SemanticChunker(config)
        chunks = await chunker.chunk(document_text, doc_id="doc-123")
        ```
    """

    def __init__(self, config: Optional[SemanticChunkConfig] = None) -> None:
        """Initialize semantic chunker.

        Args:
            config: Semantic chunking configuration. Uses defaults if not provided.
        """
        super().__init__(config or SemanticChunkConfig())
        self.config: SemanticChunkConfig  # Type hint for IDE
        self._embedding_model: Optional[Any] = None

    def _get_embedding_model(self) -> Optional[Any]:
        """Lazy-load the embedding model.

        Returns:
            SentenceTransformer model or None if not available.
        """
        if self._embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer

                self._embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
            except ImportError:
                # sentence-transformers not installed, will use fallback
                pass
        return self._embedding_model

    def _split_into_sentences(self, text: str) -> List[str]:
        """Split text into sentences.

        Args:
            text: Text to split.

        Returns:
            List of sentences.
        """
        import re

        # Simple sentence splitting on punctuation followed by space and capital
        # Handles . ! ? followed by whitespace and capital letter or end
        sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])|(?<=[.!?])\s*$", text.strip())
        return [s.strip() for s in sentences if s.strip()]

    def _compute_embeddings(self, sentences: List[str]) -> Optional[np.ndarray]:
        """Compute embeddings for sentences.

        Args:
            sentences: Sentences to embed.

        Returns:
            Array of embeddings or None if model unavailable.
        """
        model = self._get_embedding_model()
        if model is None or not sentences:
            return None
        try:
            return model.encode(sentences, show_progress_bar=False)
        except Exception:
            return None

    def _find_semantic_boundaries(
        self, sentences: List[str], embeddings: np.ndarray
    ) -> List[int]:
        """Find indices where semantic similarity drops below threshold.

        Args:
            sentences: List of sentences.
            embeddings: Sentence embeddings array.

        Returns:
            List of boundary indices (0 = before first sentence).
        """
        if len(sentences) <= 1 or embeddings is None:
            return []

        boundaries = [0]  # Always start at 0

        # Compute cosine similarities between consecutive sentences
        for i in range(1, len(sentences)):
            prev_emb = embeddings[i - 1]
            curr_emb = embeddings[i]

            # Cosine similarity
            similarity = np.dot(prev_emb, curr_emb) / (
                np.linalg.norm(prev_emb) * np.linalg.norm(curr_emb) + 1e-10
            )

            if similarity < self.config.threshold:
                boundaries.append(i)

        return boundaries

    async def chunk(self, text: str, doc_id: Optional[str] = None) -> List[Chunk]:
        """Split text into semantically coherent chunks.

        Uses embeddings to detect topic boundaries and split accordingly.
        Falls back to sentence-based splitting if embeddings unavailable.

        Args:
            text: The text content to chunk.
            doc_id: Optional document ID for chunk metadata.

        Returns:
            List of Chunk objects with continuity links.

        Raises:
            ValueError: If text is empty or invalid.
        """
        if not text or not text.strip():
            return []

        # Split into sentences
        sentences = self._split_into_sentences(text)
        if not sentences:
            return []

        # Compute embeddings
        embeddings = self._compute_embeddings(sentences)

        # Find semantic boundaries
        if embeddings is not None and len(sentences) > 1:
            boundaries = self._find_semantic_boundaries(sentences, embeddings)
        else:
            # Fallback: split based on min_chunk_size
            boundaries = self._fallback_boundaries(sentences)

        # Create chunks from boundaries
        chunks: List[Chunk] = []
        for i in range(len(boundaries)):
            start_idx = boundaries[i]
            end_idx = boundaries[i + 1] if i + 1 < len(boundaries) else len(sentences)

            chunk_sentences = sentences[start_idx:end_idx]
            content = " ".join(chunk_sentences)

            # Skip chunks that are too small
            if len(content) < self.config.min_chunk_size and i > 0:
                # Merge with previous chunk if possible
                if chunks:
                    prev_chunk = chunks[-1]
                    prev_chunk.content = f"{prev_chunk.content} {content}"
                    prev_chunk.token_count = self._count_tokens(prev_chunk.content)
                continue

            token_count = self._count_tokens(content)

            chunk = Chunk(
                content=content,
                token_count=token_count,
                index=len(chunks),
                metadata={
                    "start_sentence": start_idx,
                    "end_sentence": end_idx - 1,
                    "sentence_count": len(chunk_sentences),
                    "strategy": "semantic",
                },
            )
            chunks.append(chunk)

        # Set continuity links
        if chunks:
            self._set_continuity_links(chunks, doc_id or "doc")

        return chunks

    def _fallback_boundaries(self, sentences: List[str]) -> List[int]:
        """Create boundaries based on min_chunk_size when embeddings unavailable.

        Args:
            sentences: List of sentences.

        Returns:
            List of boundary indices.
        """
        boundaries = [0]
        current_size = 0

        for i, sentence in enumerate(sentences):
            current_size += len(sentence) + 1  # +1 for space

            if current_size >= self.config.min_chunk_size:
                boundaries.append(i + 1)
                current_size = 0

        return boundaries