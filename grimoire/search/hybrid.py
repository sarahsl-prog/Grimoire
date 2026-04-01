"""Hybrid search combining vector similarity and full-text search.

Merges results from vector search (semantic) and PostgreSQL FTS (lexical)
with configurable weights, deduplication, and optional reranking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.core.embedder import Embedder
from grimoire.core.reranker import Reranker
from grimoire.search.fulltext import FTSResult, FulltextSearch
from grimoire.vectorstore.base import VectorStore


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class HybridResult:
    """A single hybrid search result.

    Attributes:
        chunk_id: UUID of the matching chunk.
        document_id: UUID of the parent document.
        content: Chunk text content.
        score: Combined relevance score (0-1, higher is better).
        vector_score: Score from vector search (if applicable).
        fts_score: Score from full-text search (if applicable).
        document_title: Title of the parent document.
        metadata: Additional metadata from vector store.
    """

    chunk_id: str
    document_id: str
    content: str
    score: float
    vector_score: Optional[float] = None
    fts_score: Optional[float] = None
    document_title: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


# =============================================================================
# Hybrid Search Engine
# =============================================================================


class HybridSearch:
    """Combines vector similarity search with full-text search.

    Performs both searches in parallel, merges and deduplicates results,
    then normalizes and combines scores using configurable weights.

    Args:
        vector_store: Vector store for semantic search.
        embedder: Embedding service for query vectorization.
        reranker: Optional cross-encoder reranker.
        vector_weight: Weight for vector search scores (0-1).
        fts_weight: Weight for FTS scores (0-1).

    Example:
        ```python
        search = HybridSearch(
            vector_store=chromadb,
            embedder=embedder,
            vector_weight=0.7,
            fts_weight=0.3,
        )
        results = await search.search(db, "machine learning basics", top_k=10)
        ```
    """

    def __init__(
        self,
        vector_store: VectorStore,
        embedder: Embedder,
        reranker: Optional[Reranker] = None,
        vector_weight: float = 0.7,
        fts_weight: float = 0.3,
    ) -> None:
        self._vector_store = vector_store
        self._embedder = embedder
        self._reranker = reranker
        self._vector_weight = vector_weight
        self._fts_weight = fts_weight

        logger.debug(
            f"HybridSearch initialized (vector_weight={vector_weight}, "
            f"fts_weight={fts_weight})"
        )

    async def search(
        self,
        db: AsyncSession,
        query: str,
        *,
        top_k: int = 10,
        vector_top_k: int = 50,
        fts_top_k: int = 20,
        filter_dict: Optional[Dict[str, Any]] = None,
        rerank: bool = True,
        rerank_top_k: Optional[int] = None,
    ) -> List[HybridResult]:
        """Execute a hybrid search combining vector and full-text results.

        Args:
            db: Database session for FTS queries.
            query: Search query string.
            top_k: Number of final results to return.
            vector_top_k: Number of vector search candidates.
            fts_top_k: Number of FTS candidates.
            filter_dict: Optional metadata filters for vector search.
            rerank: Whether to apply reranking (if reranker is available).
            rerank_top_k: Number of results after reranking (defaults to top_k).

        Returns:
            List of HybridResult, sorted by combined score.
        """
        if not query or not query.strip():
            return []

        rerank_top_k = rerank_top_k or top_k

        # Run vector search and FTS in parallel
        vector_results = await self._vector_search(
            query, top_k=vector_top_k, filter_dict=filter_dict,
        )
        fts_results = await self._fts_search(db, query, top_k=fts_top_k)

        # Merge and deduplicate
        merged = self._merge_results(vector_results, fts_results)

        if not merged:
            return []

        # Apply reranking if available
        if rerank and self._reranker and len(merged) > 1:
            merged = await self._apply_reranking(
                query, merged, top_k=rerank_top_k,
            )

        # Sort by score and limit
        merged.sort(key=lambda r: r.score, reverse=True)
        return merged[:top_k]

    async def vector_search_only(
        self,
        query: str,
        *,
        top_k: int = 10,
        filter_dict: Optional[Dict[str, Any]] = None,
    ) -> List[HybridResult]:
        """Perform vector search only (no FTS).

        Args:
            query: Search query.
            top_k: Number of results.
            filter_dict: Optional metadata filters.

        Returns:
            List of HybridResult from vector search.
        """
        return await self._vector_search(query, top_k=top_k, filter_dict=filter_dict)

    async def fts_search_only(
        self,
        db: AsyncSession,
        query: str,
        *,
        top_k: int = 10,
    ) -> List[HybridResult]:
        """Perform full-text search only (no vector search).

        Args:
            db: Database session.
            query: Search query.
            top_k: Number of results.

        Returns:
            List of HybridResult from FTS.
        """
        return await self._fts_search(db, query, top_k=top_k)

    # -------------------------------------------------------------------------
    # Internal search methods
    # -------------------------------------------------------------------------

    async def _vector_search(
        self,
        query: str,
        top_k: int = 50,
        filter_dict: Optional[Dict[str, Any]] = None,
    ) -> List[HybridResult]:
        """Execute vector similarity search.

        Args:
            query: Search query to embed.
            top_k: Number of results.
            filter_dict: Optional metadata filters.

        Returns:
            List of HybridResult with vector scores.
        """
        try:
            query_embedding = await self._embedder.embed_single(query)
            raw_results = await self._vector_store.search(
                query_embedding=query_embedding,
                filter_dict=filter_dict,
                top_k=top_k,
                include=["metadatas", "documents", "distances"],
            )

            results: List[HybridResult] = []
            for item in raw_results:
                # ChromaDB returns distance; convert to similarity score
                distance = item.get("distance", 0.0)
                score = max(0.0, 1.0 - distance)

                metadata = item.get("metadata", {})
                results.append(
                    HybridResult(
                        chunk_id=item.get("id", ""),
                        document_id=metadata.get("document_id", ""),
                        content=item.get("document", ""),
                        score=score * self._vector_weight,
                        vector_score=score,
                        metadata=metadata,
                    )
                )

            logger.debug(f"Vector search returned {len(results)} results")
            return results

        except Exception as e:
            logger.error(f"Vector search failed: {e}")
            return []

    async def _fts_search(
        self,
        db: AsyncSession,
        query: str,
        top_k: int = 20,
    ) -> List[HybridResult]:
        """Execute full-text search.

        Args:
            db: Database session.
            query: Search query.
            top_k: Number of results.

        Returns:
            List of HybridResult with FTS scores.
        """
        try:
            fts = FulltextSearch(db)
            fts_results = await fts.search(query, top_k=top_k)

            if not fts_results:
                return []

            # Normalize FTS ranks to 0-1 range
            max_rank = max(r.rank for r in fts_results) if fts_results else 1.0
            max_rank = max(max_rank, 0.001)  # Avoid division by zero

            results: List[HybridResult] = []
            for r in fts_results:
                normalized_score = r.rank / max_rank
                results.append(
                    HybridResult(
                        chunk_id=r.chunk_id,
                        document_id=r.document_id,
                        content=r.content,
                        score=normalized_score * self._fts_weight,
                        fts_score=normalized_score,
                        document_title=r.document_title,
                    )
                )

            logger.debug(f"FTS returned {len(results)} results")
            return results

        except Exception as e:
            logger.error(f"FTS search failed: {e}")
            return []

    # -------------------------------------------------------------------------
    # Merging and reranking
    # -------------------------------------------------------------------------

    def _merge_results(
        self,
        vector_results: List[HybridResult],
        fts_results: List[HybridResult],
    ) -> List[HybridResult]:
        """Merge and deduplicate vector and FTS results.

        When both searches return the same chunk, their scores are combined.

        Args:
            vector_results: Results from vector search.
            fts_results: Results from full-text search.

        Returns:
            Merged and deduplicated results.
        """
        merged: Dict[str, HybridResult] = {}

        # Add vector results
        for r in vector_results:
            merged[r.chunk_id] = r

        # Merge FTS results
        for r in fts_results:
            if r.chunk_id in merged:
                # Combine scores for chunks found by both searches
                existing = merged[r.chunk_id]
                existing.score += r.score
                existing.fts_score = r.fts_score
                if not existing.document_title and r.document_title:
                    existing.document_title = r.document_title
            else:
                merged[r.chunk_id] = r

        logger.debug(
            f"Merged {len(vector_results)} vector + {len(fts_results)} FTS "
            f"= {len(merged)} unique results"
        )
        return list(merged.values())

    async def _apply_reranking(
        self,
        query: str,
        results: List[HybridResult],
        top_k: int = 10,
    ) -> List[HybridResult]:
        """Apply cross-encoder reranking to merged results.

        Args:
            query: Original query for reranking.
            results: Merged results to rerank.
            top_k: Number of top results to keep.

        Returns:
            Reranked and filtered results.
        """
        if not self._reranker:
            return results

        try:
            documents = [r.content for r in results]
            top_indices = await self._reranker.rerank(
                query, documents, top_k=top_k,
            )

            reranked: List[HybridResult] = []
            for rank, idx in enumerate(top_indices):
                result = results[idx]
                # Override score with reranking position
                result.score = 1.0 - (rank / len(top_indices))
                reranked.append(result)

            logger.debug(f"Reranked to {len(reranked)} results")
            return reranked

        except Exception as e:
            logger.warning(f"Reranking failed, using original scores: {e}")
            return results
