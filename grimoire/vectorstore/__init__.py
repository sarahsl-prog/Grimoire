"""Vector store abstraction for Grimoire.

This module provides vector store implementations for Grimoire, including
ChromaDB and Qdrant backends. All implementations conform to the VectorStore
abstract base class for easy swapping.

Example:
    >>> from grimoire.vectorstore import ChromaDBStore
    >>> store = ChromaDBStore("./chroma_db")
    >>> await store.initialize("documents", embedding_dim=768)
    >>> await store.add_documents(...)

Exports:
    VectorStore: Abstract base class for vector stores.
    ChromaDBStore: ChromaDB implementation of VectorStore.
"""

from grimoire.vectorstore.base import VectorStore
from grimoire.vectorstore.chromadb import ChromaDBStore

__all__ = ["VectorStore", "ChromaDBStore"]
