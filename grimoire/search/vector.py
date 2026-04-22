"""
Vector Search Wrapper for Grimoire.

This module provides a wrapper around vectorstore operations for semantic search functionality.
"""

from typing import List, Dict, Any, Optional
from grimoire.vectorstore.base import VectorStore
from grimoire.config.settings import settings


class VectorSearch:
    """Wrapper for vector search operations."""

    def __init__(self, vector_store: VectorStore):
        """
        Initialize vector search wrapper.

        Args:
            vector_store: Vector store instance to use for search operations
        """
        self.vector_store = vector_store
        self.logger = settings.LOGGER.bind(component="vector_search")

    async def search(
        self,
        query_embedding: List[float],
        filter_dict: Optional[Dict[str, Any]] = None,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Perform vector similarity search.

        Args:
            query_embedding: Query embedding vector
            filter_dict: Optional metadata filters
            top_k: Number of results to return

        Returns:
            List of search results
        """
        try:
            # Validate inputs
            if not query_embedding:
                raise ValueError("Query embedding cannot be empty")

            if top_k <= 0:
                raise ValueError("top_k must be positive")

            self.logger.debug(f"Performing vector search with top_k={top_k}")

            # Perform search
            results = await self.vector_store.search(
                query_embedding=query_embedding, filter_dict=filter_dict, top_k=top_k
            )

            self.logger.debug(f"Vector search returned {len(results)} results")
            return results

        except Exception as e:
            self.logger.error(f"Error performing vector search: {e}")
            raise

    async def search_by_text(
        self,
        query_text: str,
        embedder: Any,  # Embedder instance
        filter_dict: Optional[Dict[str, Any]] = None,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Perform vector similarity search with text query.

        Args:
            query_text: Text query to search for
            embedder: Embedder instance to convert text to embedding
            filter_dict: Optional metadata filters
            top_k: Number of results to return

        Returns:
            List of search results
        """
        try:
            # Validate inputs
            if not query_text:
                raise ValueError("Query text cannot be empty")

            self.logger.debug(f"Generating embedding for query: {query_text}")

            # Generate embedding for query text
            query_embeddings = await embedder.embed([query_text])
            query_embedding = query_embeddings[0]

            # Perform vector search
            return await self.search(
                query_embedding=query_embedding, filter_dict=filter_dict, top_k=top_k
            )

        except Exception as e:
            self.logger.error(f"Error performing text-based vector search: {e}")
            raise

    async def get_document_count(self) -> int:
        """
        Get the total number of documents in the vector store.

        Returns:
            Total document count
        """
        try:
            count = await self.vector_store.count()
            self.logger.debug(f"Vector store contains {count} documents")
            return count
        except Exception as e:
            self.logger.error(f"Error getting document count: {e}")
            raise

    async def delete_documents(self, ids: List[str]) -> None:
        """
        Delete documents from the vector store.

        Args:
            ids: List of document IDs to delete
        """
        try:
            if not ids:
                self.logger.debug("No document IDs provided for deletion")
                return

            self.logger.debug(f"Deleting {len(ids)} documents from vector store")
            await self.vector_store.delete(ids)
            self.logger.debug(f"Successfully deleted {len(ids)} documents")

        except Exception as e:
            self.logger.error(f"Error deleting documents: {e}")
            raise
