"""Tests for the Query Agent and Hybrid Search.

Tests cover:
- Hybrid search: vector + FTS merging, deduplication, scoring
- Query agent: full pipeline, caching, error handling
- Context assembly and citation building
- Edge cases: empty queries, no results, LLM failures
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from grimoire.agents.query import (
    Citation,
    QueryAgent,
    QueryResult,
    SearchOnlyResult,
)
from grimoire.search.hybrid import HybridResult, HybridSearch


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_embedder() -> MagicMock:
    """Create a mock Embedder."""
    embedder = MagicMock()
    embedder.embed_single = AsyncMock(return_value=[0.1, 0.2, 0.3])
    embedder.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
    return embedder


@pytest.fixture
def mock_vector_store() -> MagicMock:
    """Create a mock VectorStore."""
    store = MagicMock()
    store.search = AsyncMock(
        return_value=[
            {
                "id": "chunk-1",
                "document": "Machine learning is a subset of AI.",
                "distance": 0.2,
                "metadata": {
                    "document_id": "doc-1",
                    "chunk_index": 0,
                    "token_count": 10,
                },
            },
            {
                "id": "chunk-2",
                "document": "Deep learning uses neural networks.",
                "distance": 0.4,
                "metadata": {
                    "document_id": "doc-2",
                    "chunk_index": 1,
                    "token_count": 8,
                },
            },
        ]
    )
    return store


@pytest.fixture
def mock_reranker() -> MagicMock:
    """Create a mock Reranker."""
    reranker = MagicMock()
    reranker.rerank = AsyncMock(return_value=[1, 0])  # Reverse order
    return reranker


@pytest.fixture
def mock_cache() -> MagicMock:
    """Create a mock Cache."""
    cache = MagicMock()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    return cache


@pytest.fixture
def mock_db() -> AsyncMock:
    """Create a mock database session."""
    db = AsyncMock()
    return db


@pytest.fixture
def hybrid_search(
    mock_vector_store: MagicMock,
    mock_embedder: MagicMock,
    mock_reranker: MagicMock,
) -> HybridSearch:
    """Create a HybridSearch instance with mocked dependencies."""
    return HybridSearch(
        vector_store=mock_vector_store,
        embedder=mock_embedder,
        reranker=mock_reranker,
        vector_weight=0.7,
        fts_weight=0.3,
    )


@pytest.fixture
def query_agent(
    hybrid_search: HybridSearch,
    mock_cache: MagicMock,
) -> QueryAgent:
    """Create a QueryAgent with mocked dependencies."""
    return QueryAgent(
        hybrid_search=hybrid_search,
        llm_url="http://localhost:11434",
        llm_model="test-model",
        cache=mock_cache,
        max_context_chunks=5,
    )


def make_hybrid_result(
    chunk_id: str = "chunk-1",
    document_id: str = "doc-1",
    content: str = "Test content",
    score: float = 0.8,
    **kwargs: Any,
) -> HybridResult:
    """Helper to create HybridResult instances."""
    return HybridResult(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        score=score,
        **kwargs,
    )


# =============================================================================
# Hybrid Search Tests
# =============================================================================


class TestHybridSearch:
    """Hybrid search merging and scoring."""

    @pytest.mark.asyncio
    async def test_vector_search_returns_results(
        self, hybrid_search: HybridSearch, mock_db: AsyncMock,
    ) -> None:
        """Vector search returns scored results."""
        # Patch FTS to return nothing
        with patch.object(hybrid_search, "_fts_search", new_callable=AsyncMock) as mock_fts:
            mock_fts.return_value = []

            results = await hybrid_search.search(mock_db, "machine learning")

        assert len(results) > 0
        for r in results:
            assert r.chunk_id
            assert r.score >= 0

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(
        self, hybrid_search: HybridSearch, mock_db: AsyncMock,
    ) -> None:
        """Empty query returns no results."""
        results = await hybrid_search.search(mock_db, "")
        assert results == []

        results = await hybrid_search.search(mock_db, "   ")
        assert results == []

    @pytest.mark.asyncio
    async def test_merge_deduplicates(self, hybrid_search: HybridSearch) -> None:
        """Merging deduplicates by chunk_id and combines scores."""
        vector_results = [
            make_hybrid_result(
                chunk_id="shared", score=0.5, vector_score=0.7,
            ),
            make_hybrid_result(
                chunk_id="vector-only", score=0.3, vector_score=0.4,
            ),
        ]
        fts_results = [
            make_hybrid_result(
                chunk_id="shared", score=0.2, fts_score=0.6,
            ),
            make_hybrid_result(
                chunk_id="fts-only", score=0.4, fts_score=0.8,
            ),
        ]

        merged = hybrid_search._merge_results(vector_results, fts_results)

        assert len(merged) == 3  # shared + vector-only + fts-only

        # Find the shared result
        shared = [r for r in merged if r.chunk_id == "shared"][0]
        assert shared.score == 0.7  # 0.5 + 0.2
        assert shared.vector_score == 0.7
        assert shared.fts_score == 0.6

    @pytest.mark.asyncio
    async def test_reranking_applied(
        self,
        hybrid_search: HybridSearch,
        mock_db: AsyncMock,
        mock_reranker: MagicMock,
    ) -> None:
        """Reranking reorders results."""
        with patch.object(hybrid_search, "_fts_search", new_callable=AsyncMock) as mock_fts:
            mock_fts.return_value = []

            results = await hybrid_search.search(
                mock_db, "test query", rerank=True,
            )

        mock_reranker.rerank.assert_called_once()

    @pytest.mark.asyncio
    async def test_reranking_skipped_when_disabled(
        self,
        hybrid_search: HybridSearch,
        mock_db: AsyncMock,
        mock_reranker: MagicMock,
    ) -> None:
        """Reranking is skipped when rerank=False."""
        with patch.object(hybrid_search, "_fts_search", new_callable=AsyncMock) as mock_fts:
            mock_fts.return_value = []

            results = await hybrid_search.search(
                mock_db, "test query", rerank=False,
            )

        mock_reranker.rerank.assert_not_called()

    @pytest.mark.asyncio
    async def test_vector_search_only(
        self, hybrid_search: HybridSearch,
    ) -> None:
        """vector_search_only skips FTS."""
        results = await hybrid_search.vector_search_only("test query")
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_vector_search_failure_returns_empty(
        self,
        hybrid_search: HybridSearch,
        mock_vector_store: MagicMock,
    ) -> None:
        """Vector search failure returns empty list."""
        mock_vector_store.search = AsyncMock(
            side_effect=RuntimeError("Connection failed")
        )
        results = await hybrid_search.vector_search_only("test")
        assert results == []

    @pytest.mark.asyncio
    async def test_distance_to_score_conversion(
        self, hybrid_search: HybridSearch,
    ) -> None:
        """ChromaDB distances are converted to similarity scores."""
        results = await hybrid_search.vector_search_only("test")
        # Distance 0.2 -> score 0.8 * weight 0.7 = 0.56
        # Distance 0.4 -> score 0.6 * weight 0.7 = 0.42
        assert results[0].vector_score == pytest.approx(0.8)
        assert results[1].vector_score == pytest.approx(0.6)


# =============================================================================
# Query Agent Tests
# =============================================================================


class TestQueryAgentHappyPath:
    """Standard query scenarios."""

    @pytest.mark.asyncio
    async def test_full_query_pipeline(
        self,
        query_agent: QueryAgent,
        mock_db: AsyncMock,
    ) -> None:
        """Full query pipeline produces answer with citations."""
        with patch.object(
            query_agent._hybrid_search, "_fts_search",
            new_callable=AsyncMock, return_value=[],
        ):
            with patch.object(
                query_agent, "_generate_answer",
                new_callable=AsyncMock,
                return_value=("Machine learning is a subset of AI [Source 1].", False),
            ):
                result = await query_agent.query(
                    mock_db, "What is machine learning?",
                )

        assert result.query == "What is machine learning?"
        assert "Machine learning" in result.answer
        assert len(result.citations) > 0
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_search_only(
        self,
        query_agent: QueryAgent,
        mock_db: AsyncMock,
    ) -> None:
        """Search without LLM generation."""
        with patch.object(
            query_agent._hybrid_search, "search",
            new_callable=AsyncMock,
            return_value=[
                make_hybrid_result(content="Result 1"),
                make_hybrid_result(chunk_id="c2", content="Result 2"),
            ],
        ):
            result = await query_agent.search(mock_db, "test query")

        assert result.total_results == 2
        assert len(result.results) == 2
        assert result.query == "test query"

    @pytest.mark.asyncio
    async def test_empty_query_returns_default(
        self,
        query_agent: QueryAgent,
        mock_db: AsyncMock,
    ) -> None:
        """Empty query returns a default response."""
        result = await query_agent.query(mock_db, "")
        assert "provide a question" in result.answer.lower()

    @pytest.mark.asyncio
    async def test_no_results_returns_message(
        self,
        query_agent: QueryAgent,
        mock_db: AsyncMock,
    ) -> None:
        """No search results returns an appropriate message."""
        with patch.object(
            query_agent._hybrid_search, "search",
            new_callable=AsyncMock, return_value=[],
        ):
            result = await query_agent.query(mock_db, "obscure question")

        assert "couldn't find" in result.answer.lower()
        assert result.search_results_count == 0


# =============================================================================
# Caching Tests
# =============================================================================


class TestQueryCaching:
    """Query result caching."""

    @pytest.mark.asyncio
    async def test_cache_hit(
        self,
        query_agent: QueryAgent,
        mock_cache: MagicMock,
        mock_db: AsyncMock,
    ) -> None:
        """Cached results are returned directly."""
        cached_data = {
            "query": "cached question",
            "answer": "cached answer",
            "citations": [],
            "model_used": "test",
            "search_results_count": 3,
        }
        mock_cache.get = AsyncMock(return_value=cached_data)

        result = await query_agent.query(mock_db, "cached question")

        assert result.cached is True
        assert result.answer == "cached answer"

    @pytest.mark.asyncio
    async def test_cache_miss_stores_result(
        self,
        query_agent: QueryAgent,
        mock_cache: MagicMock,
        mock_db: AsyncMock,
    ) -> None:
        """Cache misses store new results."""
        with patch.object(
            query_agent._hybrid_search, "search",
            new_callable=AsyncMock,
            return_value=[make_hybrid_result(content="Some result")],
        ):
            with patch.object(
                query_agent, "_generate_answer",
                new_callable=AsyncMock,
                return_value=("Generated answer", False),
            ):
                await query_agent.query(mock_db, "uncached question")

        mock_cache.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_bypass(
        self,
        query_agent: QueryAgent,
        mock_cache: MagicMock,
        mock_db: AsyncMock,
    ) -> None:
        """Cache can be bypassed with use_cache=False."""
        with patch.object(
            query_agent._hybrid_search, "search",
            new_callable=AsyncMock, return_value=[],
        ):
            await query_agent.query(
                mock_db, "test", use_cache=False,
            )

        mock_cache.get.assert_not_called()
        mock_cache.set.assert_not_called()

    def test_cache_key_deterministic(self, query_agent: QueryAgent) -> None:
        """Same query produces same cache key."""
        key1 = query_agent._cache_key("test query", None)
        key2 = query_agent._cache_key("test query", None)
        assert key1 == key2

    def test_cache_key_different_filters(self, query_agent: QueryAgent) -> None:
        """Different filters produce different cache keys."""
        key1 = query_agent._cache_key("test", {"tag": "a"})
        key2 = query_agent._cache_key("test", {"tag": "b"})
        assert key1 != key2

    def test_cache_key_case_insensitive(self, query_agent: QueryAgent) -> None:
        """Cache key is case-insensitive for queries."""
        key1 = query_agent._cache_key("Test Query", None)
        key2 = query_agent._cache_key("test query", None)
        assert key1 == key2


# =============================================================================
# Context Assembly Tests
# =============================================================================


class TestContextAssembly:
    """Context and citation building."""

    def test_build_citations(self, query_agent: QueryAgent) -> None:
        """Citations are built from search results."""
        results = [
            make_hybrid_result(
                chunk_id="c1", document_id="d1",
                content="Short content",
                score=0.9,
                document_title="Doc 1",
            ),
            make_hybrid_result(
                chunk_id="c2", document_id="d2",
                content="A" * 300,
                score=0.7,
                document_title="Doc 2",
            ),
        ]

        citations = query_agent._build_citations(results)
        assert len(citations) == 2
        assert citations[0].document_id == "d1"
        assert citations[0].relevance_score == 0.9
        # Long content should be truncated
        assert len(citations[1].content_snippet) <= 210  # 200 + "..."

    def test_assemble_context(self, query_agent: QueryAgent) -> None:
        """Context is assembled with source labels."""
        results = [
            make_hybrid_result(
                content="First chunk", document_title="Doc A",
            ),
            make_hybrid_result(
                chunk_id="c2", content="Second chunk",
                document_title="Doc B",
            ),
        ]

        context = query_agent._assemble_context(results)
        assert "[Source 1: Doc A]" in context
        assert "[Source 2: Doc B]" in context
        assert "First chunk" in context
        assert "Second chunk" in context

    def test_assemble_context_limits_chunks(self, query_agent: QueryAgent) -> None:
        """Context respects max_context_chunks limit."""
        results = [
            make_hybrid_result(chunk_id=f"c{i}", content=f"Chunk {i}")
            for i in range(20)
        ]

        context = query_agent._assemble_context(results)
        # Should only include max_context_chunks (5)
        assert "[Source 5:" in context
        assert "[Source 6:" not in context


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestQueryErrorHandling:
    """Error handling behavior."""

    @pytest.mark.asyncio
    async def test_cache_error_does_not_fail_query(
        self,
        query_agent: QueryAgent,
        mock_cache: MagicMock,
        mock_db: AsyncMock,
    ) -> None:
        """Cache errors don't prevent query execution."""
        mock_cache.get = AsyncMock(side_effect=RuntimeError("Redis down"))

        with patch.object(
            query_agent._hybrid_search, "search",
            new_callable=AsyncMock, return_value=[],
        ):
            result = await query_agent.query(mock_db, "test query")

        # Query should still work
        assert result.query == "test query"


# =============================================================================
# Data Model Tests
# =============================================================================


class TestQueryModels:
    """Data model validation."""

    def test_query_result_defaults(self) -> None:
        result = QueryResult(query="test")
        assert result.answer == ""
        assert result.citations == []
        assert result.cached is False

    def test_citation_model(self) -> None:
        citation = Citation(
            document_id="d1",
            chunk_id="c1",
            content_snippet="test snippet",
            relevance_score=0.95,
        )
        assert citation.document_id == "d1"
        assert citation.relevance_score == 0.95

    def test_search_only_result(self) -> None:
        result = SearchOnlyResult(query="test")
        assert result.total_results == 0
        assert result.results == []
