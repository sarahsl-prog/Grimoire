"""Abstract base class for document reranking implementations."""

from abc import ABC, abstractmethod
from typing import List


class Reranker(ABC):
    """Abstract base class for cross-encoder reranking.

    Rerankers improve retrieval quality by scoring query-document relevance
    with more precision than vector similarity alone. They are typically
    used after initial vector/FTS retrieval to refine top results.

    Example:
        ```python
        class CrossEncoderReranker(Reranker):
            async def rerank(
                self, query: str, documents: List[str], top_k: int = 5
            ) -> List[int]:
                # Implementation
                pass
        ```

    Typical Pipeline:
        1. Vector search returns Top 50 results
        2. Reranker scores all 50 against query
        3. Top-k indices are returned for LLM context
    """

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: List[str],
        top_k: int = 5,
    ) -> List[int]:
        """Rerank documents by relevance to query.

        Args:
            query: The original user query.
            documents: List of document texts or summaries to rerank.
            top_k: Number of top documents to return.

        Returns:
            List of indices into the documents list, sorted by relevance
            (most relevant first). Length = min(top_k, len(documents)).

        Raises:
            ValueError: If documents is empty or top_k is invalid.
            RuntimeError: If reranking operation fails.

        Example:
            ```python
            reranker = CrossEncoderReranker()
            documents = ["doc1 text", "doc2 text", "doc3 text"]
            top_indices = await reranker.rerank("my query", documents, top_k=2)
            # Returns [1, 2] meaning documents[1] and documents[2] are most relevant
            relevant_docs = [documents[i] for i in top_indices]
            ```
        """
        raise NotImplementedError("Subclasses must implement rerank()")
