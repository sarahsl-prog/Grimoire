"""Abstract base class and models for document chunking.

This module defines the core data models and abstract base class for all
chunking strategies in Grimoire. It provides a common interface for creating
and managing document chunks with continuity tracking.
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class ChunkingStrategy(str, Enum):
    """Enumeration of available chunking strategies."""

    SEMANTIC = "semantic"
    MARKDOWN = "markdown"
    RECURSIVE = "recursive"


class Chunk(BaseModel):
    """Represents a single document chunk with continuity tracking.

    Chunks maintain bidirectional links to previous and next chunks,
    enabling context restoration during retrieval. Each chunk tracks
    its token count and position within the document.

    Attributes:
        content: The text content of this chunk.
        token_count: Approximate token count (for context window planning).
        index: Position of this chunk in the document sequence (0-based).
        prev_chunk_id: ID of the previous chunk, if any.
        next_chunk_id: ID of the next chunk, if any.
        metadata: Additional metadata (source doc, headers, etc.).
        chunk_type: Optional chunk shape category (e.g. ``"prose"``,
            ``"sigma_rule"``, ``"cve_block"``). ``None`` for legacy/default
            chunks; populated by domain-specific chunkers.
        source_type: Optional origin source type (e.g. ``"sigma"``,
            ``"nvd_cve"``, ``"mitre_attack"``). ``None`` for general docs.

    Example:
        ```python
        chunk = Chunk(
            content="This is chunk content...",
            token_count=150,
            index=0,
            prev_chunk_id=None,
            next_chunk_id="chunk-uuid-2",
            metadata={"source_doc": "doc.pdf", "header": "Section 1"},
        )
        ```
    """

    model_config = ConfigDict(frozen=False, extra="allow")

    content: str = Field(..., description="Text content of the chunk")
    token_count: int = Field(
        ..., ge=0, description="Approximate token count for context planning"
    )
    index: int = Field(..., ge=0, description="Position in document sequence (0-based)")
    prev_chunk_id: Optional[str] = Field(
        default=None, description="ID of previous chunk for continuity"
    )
    next_chunk_id: Optional[str] = Field(
        default=None, description="ID of next chunk for continuity"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata (headers, source, etc.)"
    )
    chunk_type: Optional[str] = Field(
        default=None,
        description="Chunk shape category, e.g. 'prose', 'sigma_rule', 'cve_block'. None for legacy/default chunks.",
    )
    source_type: Optional[str] = Field(
        default=None,
        description="Origin source type, e.g. 'sigma', 'nvd_cve', 'mitre_attack'. None for general docs.",
    )

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        """Validate that content is not empty or whitespace-only."""
        if not v or not v.strip():
            raise ValueError("Chunk content cannot be empty")
        return v


class ChunkConfig(BaseModel):
    """Configuration for chunking strategies.

    This model provides common configuration options across all chunking
    strategies. Strategy-specific configurations inherit from this base.

    Attributes:
        chunk_size: Target size of each chunk (in tokens or characters).
        chunk_overlap: Number of tokens/characters to overlap between chunks.
        strategy: The chunking strategy to use.
        encoding_name: Name of the tiktoken encoding for token counting.

    Example:
        ```python
        config = ChunkConfig(
            chunk_size=1000,
            chunk_overlap=200,
            strategy=ChunkingStrategy.SEMANTIC,
        )
        ```
    """

    model_config = ConfigDict(frozen=False, extra="allow")

    chunk_size: int = Field(
        default=1000, ge=1, description="Target chunk size (tokens or characters)"
    )
    chunk_overlap: int = Field(
        default=200, ge=0, description="Overlap between consecutive chunks"
    )
    strategy: ChunkingStrategy = Field(
        default=ChunkingStrategy.RECURSIVE, description="Chunking strategy to use"
    )
    encoding_name: str = Field(
        default="cl100k_base", description="Tiktoken encoding for token counting"
    )

    @field_validator("chunk_overlap")
    @classmethod
    def overlap_less_than_chunk_size(cls, v: int, info: ValidationInfo) -> int:
        """Validate that overlap is less than chunk size."""
        data = info.data
        if "chunk_size" in data and v >= data["chunk_size"]:
            raise ValueError("chunk_overlap must be less than chunk_size")
        return v


class Chunker(ABC):
    """Abstract base class for document chunking strategies.

    All chunking implementations must provide async chunk() method that
    splits text into Chunk objects with proper continuity tracking.

    Implementations should:
        1. Respect the chunk_size and chunk_overlap settings
        2. Maintain continuity links (prev_chunk_id, next_chunk_id)
        3. Calculate accurate token counts
        4. Handle edge cases (empty text, very long words, etc.)

    Example:
        ```python
        class MyChunker(Chunker):
            def __init__(self, config: ChunkConfig):
                self.config = config

            async def chunk(self, text: str, doc_id: Optional[str] = None) -> List[Chunk]:
                # Implementation
                pass
        ```
    """

    def __init__(self, config: Optional[ChunkConfig] = None) -> None:
        """Initialize the chunker with configuration.

        Args:
            config: Chunking configuration. Uses defaults if not provided.
        """
        self.config = config or ChunkConfig()

    @abstractmethod
    async def chunk(self, text: str, doc_id: Optional[str] = None) -> List[Chunk]:
        """Split text into chunks with continuity tracking.

        Args:
            text: The text content to chunk.
            doc_id: Optional document ID for chunk metadata.

        Returns:
            List of Chunk objects with prev/next links set.

        Raises:
            ValueError: If text is malformed or invalid.
            RuntimeError: If chunking fails unexpectedly.
        """
        raise NotImplementedError("Subclasses must implement chunk()")

    def _count_tokens(self, text: str) -> int:
        """Count tokens in text using tiktoken.

        Falls back to character-based estimation if tiktoken fails.

        Args:
            text: Text to count tokens for.

        Returns:
            Approximate token count.
        """
        try:
            import tiktoken

            encoder = tiktoken.get_encoding(self.config.encoding_name)
            return len(encoder.encode(text))
        except Exception:
            # Fallback: approximate 1 token ≈ 4 characters for English text
            return len(text) // 4

    def _set_continuity_links(self, chunks: List[Chunk], doc_id: str) -> List[Chunk]:
        """Set prev/next chunk IDs for continuity tracking.

        Mutates chunks in place to establish bidirectional links.

        Args:
            chunks: List of chunks to link.
            doc_id: Document ID for generating chunk IDs.

        Returns:
            The same list with continuity links set.
        """
        from uuid import uuid4

        # Generate IDs for all chunks first
        for i, chunk in enumerate(chunks):
            chunk_id = str(uuid4())
            chunk.metadata["chunk_id"] = chunk_id

        # Set continuity links
        for i, chunk in enumerate(chunks):
            if i > 0:
                chunk.prev_chunk_id = chunks[i - 1].metadata.get("chunk_id")
            if i < len(chunks) - 1:
                chunk.next_chunk_id = chunks[i + 1].metadata.get("chunk_id")

        return chunks
