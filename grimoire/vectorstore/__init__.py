"""Vector store abstraction for Grimoire.

This module provides Grimoire's vector store. ChromaDB is the only
implementation (Qdrant has settings models but no store yet); it conforms to
the VectorStore abstract base class. Build stores with
:mod:`grimoire.vectorstore.factory` rather than constructing them by hand.

Example:
    >>> from grimoire.vectorstore.factory import create_vector_store
    >>> store = create_vector_store()
    >>> await store.initialize("documents", embedding_dim=768)
    >>> await store.add_documents(...)

Exports:
    VectorStore: Abstract base class for vector stores.
    ChromaDBStore: ChromaDB implementation of VectorStore.
"""

from grimoire.vectorstore.base import VectorStore
from grimoire.vectorstore.chromadb import ChromaDBStore

__all__ = ["VectorStore", "ChromaDBStore"]
