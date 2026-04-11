"""Abstract base class for document reranking implementations."""

import asyncio
from abc import ABC, abstractmethod
from typing import List

import numpy as np


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


class CrossEncoderReranker(Reranker):
    """Cross-encoder reranker using sentence-transformers.

    Uses a pre-trained cross-encoder model to score query-document pairs.
    Default model: cross-encoder/ms-marco-MiniLM-L-6-v2

    Typical Pipeline:
        1. Vector search returns Top 50 results
        2. Reranker scores all 50 against query
        3. Top-k indices are returned for LLM context
    """

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        self._model_name = model_name
        self._model = None

    def _get_model(self):
        """Lazy-load the cross-encoder model."""
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self._model_name)
        return self._model

    async def rerank(self, query: str, documents: List[str], top_k: int = 5) -> List[int]:
        if top_k <= 0 or not documents:
            return []

        model = self._get_model()
        pairs = [[query, doc] for doc in documents]

        def _score():
            return model.predict(pairs)

        scores = await asyncio.get_running_loop().run_in_executor(None, _score)

        if isinstance(scores, np.ndarray):
            scores = scores.tolist()

        doc_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return doc_indices[:top_k]
