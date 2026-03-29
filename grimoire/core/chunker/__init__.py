"""Chunking strategies for document processing.

This module provides multiple chunking strategies for splitting documents
into meaningful chunks while preserving context and continuity.

Example:
    ```python
    from grimoire.core.chunker import SemanticChunker, ChunkConfig

    config = ChunkConfig(chunk_size=1000, chunk_overlap=200)
    chunker = SemanticChunker(config)
    chunks = await chunker.chunk(document_text)
    ```
"""

from grimoire.core.chunker.base import Chunk, ChunkConfig, Chunker, ChunkingStrategy
from grimoire.core.chunker.markdown import MarkdownHeaderTextSplitter
from grimoire.core.chunker.recursive import RecursiveCharacterTextSplitter
from grimoire.core.chunker.semantic import SemanticChunker

__all__ = [
    "Chunk",
    "ChunkConfig",
    "Chunker",
    "ChunkingStrategy",
    "MarkdownHeaderTextSplitter",
    "RecursiveCharacterTextSplitter",
    "SemanticChunker",
]