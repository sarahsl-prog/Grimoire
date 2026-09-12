"""Abstract base class for vector store implementations."""

from abc import ABC, abstractmethod
from typing import Any


class VectorStore(ABC):
    """Abstract base class for vector database implementations.

    This interface supports ChromaDB, Qdrant, and other vector databases.
    All implementations must provide async methods for document management
    and similarity search with metadata filtering.

    Example:
        ```python
        class ChromaDBStore(VectorStore):
            async def initialize(self, collection_name: str, embedding_dim: int) -> None:
                # Implementation
                pass
        ```
    """

    @abstractmethod
    async def initialize(self, collection_name: str, embedding_dim: int) -> None:
        """Initialize connection and create collection if needed.

        Args:
            collection_name: Name of the collection to create/use.
            embedding_dim: Dimension of embedding vectors.

        Raises:
            ConnectionError: If unable to connect to vector database.
            ValueError: If embedding_dim is invalid.
        """
        raise NotImplementedError("Subclasses must implement initialize()")

    @abstractmethod
    async def add_documents(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
        documents: list[str],
    ) -> None:
        """Add or update documents in the vector store.

        Args:
            ids: Unique identifiers for documents.
            embeddings: Vector embeddings for each document.
            metadatas: Metadata dictionaries for each document.
            documents: Original text content for each document.

        Raises:
            ValueError: If input lists have mismatched lengths.
            RuntimeError: If storage operation fails.
        """
        raise NotImplementedError("Subclasses must implement add_documents()")

    @abstractmethod
    async def search(
        self,
        query_embedding: list[float],
        filter_dict: dict[str, Any] | None = None,
        top_k: int = 10,
        include: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Perform vector similarity search with optional metadata filtering.

        Args:
            query_embedding: The query vector to search for.
            filter_dict: Optional metadata filters (e.g., {"tags": {"$contains": "research"}}).
            top_k: Number of results to return.
            include: Fields to include in results ("metadatas", "documents", "distances").

        Returns:
            List of result dictionaries with requested fields.

        Raises:
            ValueError: If top_k is invalid or query_embedding has wrong dimensions.
            RuntimeError: If search operation fails.
        """
        raise NotImplementedError("Subclasses must implement search()")

    @abstractmethod
    async def delete(self, ids: list[str]) -> None:
        """Delete documents by ID.

        Args:
            ids: Document IDs to delete.

        Raises:
            RuntimeError: If deletion operation fails.
        """
        raise NotImplementedError("Subclasses must implement delete()")

    @abstractmethod
    async def get(self, ids: list[str]) -> list[dict[str, Any]]:
        """Retrieve documents by ID.

        Args:
            ids: Document IDs to retrieve.

        Returns:
            List of document dictionaries with embeddings and metadata.

        Raises:
            RuntimeError: If retrieval operation fails.
        """
        raise NotImplementedError("Subclasses must implement get()")

    @abstractmethod
    async def count(self) -> int:
        """Get total document count in the collection.

        Returns:
            Number of documents stored.

        Raises:
            RuntimeError: If count operation fails.
        """
        raise NotImplementedError("Subclasses must implement count()")
