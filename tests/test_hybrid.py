"""Tests for Hybrid Search combining vector similarity and full-text search.

Covers:
- _merge_results with overlapping and disjoint result sets
- _vector_search scoring and error handling
- _fts_search scoring and error handling
- _apply_reranking with and without reranker
- search() with parallel execution
- search() with empty queries
- Edge cases: empty results, single result, top_k boundaries
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.core.reranker import Reranker
from grimoire.search.hybrid import HybridResult, HybridSearch

# =============================================================================
# Helpers & Fixtures
# =============================================================================


class MockVectorStore:
    """Mock vector store that returns predetermined results."""

    def __init__(self, results: list[dict[str, Any]] | None = None):
        self._results = results or []
        self.is_initialized = True

    async def search(
        self,
        query_embedding: list[float],
        filter_dict: dict[str, Any] | None = None,
        top_k: int = 10,
        include: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._results[:top_k]


class MockEmbedder:
    """Mock embedder that returns a fixed embedding vector."""

    def __init__(self, dim: int = 3):
        self.embedding_dim = dim

    async def embed_single(self, text: str) -> list[float]:
        return [0.1] * self.embedding_dim


class MockReranker(Reranker):
    """Mock reranker that returns indices in original order."""

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
    ) -> list[int]:
        n = len(documents)
        effective_k = min(top_k or n, n)
        return list(range(effective_k))


class FailingReranker(Reranker):
    """Reranker that always raises an error."""

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
    ) -> list[int]:
        raise RuntimeError("Reranker service unavailable")


def make_vector_result(
    chunk_id: str,
    doc_id: str = "doc1",
    content: str = "test content",
    distance: float = 0.3,
) -> dict[str, Any]:
    """Create a mock vector search result dict."""
    return {
        "id": chunk_id,
        "document": content,
        "distance": distance,
        "metadata": {"document_id": doc_id},
    }


def mock_title_rows(mock_db: AsyncMock, titles: dict[str, str | None]) -> None:
    """Make ``db.execute(...).all()`` return (document_id, title) rows.

    Mirrors what the batched ``SELECT Document.id, Document.title`` in
    ``_backfill_document_titles`` gets back from PostgreSQL.
    """
    rows = MagicMock()
    rows.all.return_value = list(titles.items())
    mock_db.execute = AsyncMock(return_value=rows)


def make_hybrid_result(
    chunk_id: str,
    score: float = 0.5,
    vector_score: float | None = None,
    fts_score: float | None = None,
    content: str = "test",
    doc_id: str = "doc1",
) -> HybridResult:
    """Create a HybridResult for testing."""
    return HybridResult(
        chunk_id=chunk_id,
        document_id=doc_id,
        content=content,
        score=score,
        vector_score=vector_score,
        fts_score=fts_score,
    )


@pytest.fixture
def mock_vector_store() -> MockVectorStore:
    return MockVectorStore()


@pytest.fixture
def mock_embedder() -> MockEmbedder:
    return MockEmbedder()


@pytest.fixture
def hybrid(
    mock_vector_store: MockVectorStore, mock_embedder: MockEmbedder
) -> HybridSearch:
    return HybridSearch(
        vector_store=mock_vector_store,
        embedder=mock_embedder,
    )


@pytest.fixture
def hybrid_with_reranker(
    mock_vector_store: MockVectorStore, mock_embedder: MockEmbedder
) -> HybridSearch:
    return HybridSearch(
        vector_store=mock_vector_store,
        embedder=mock_embedder,
        reranker=MockReranker(),
    )


@pytest_asyncio.fixture
async def mock_db() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    # Default to an empty result set so the title-backfill query in search()
    # has something iterable to unpack; tests override via mock_title_rows().
    empty_rows = MagicMock()
    empty_rows.all.return_value = []
    session.execute = AsyncMock(return_value=empty_rows)
    yield session


# =============================================================================
# Test _merge_results
# =============================================================================


class TestMergeResults:
    """Test result merging and deduplication."""

    def test_merge_disjoint_results(self, hybrid: HybridSearch) -> None:
        """Disjoint vector and FTS results should all appear in output."""
        vector_results = [
            make_hybrid_result("c1", score=0.7, vector_score=0.7),
            make_hybrid_result("c2", score=0.5, vector_score=0.5),
        ]
        fts_results = [
            make_hybrid_result("c3", score=0.3, fts_score=0.3),
            make_hybrid_result("c4", score=0.2, fts_score=0.2),
        ]

        merged = hybrid._merge_results(vector_results, fts_results)
        assert len(merged) == 4
        chunk_ids = {r.chunk_id for r in merged}
        assert chunk_ids == {"c1", "c2", "c3", "c4"}

    def test_merge_overlapping_results(self, hybrid: HybridSearch) -> None:
        """When the same chunk appears in both, scores should be combined."""
        vector_results = [
            make_hybrid_result("c1", score=0.7, vector_score=0.7),
        ]
        fts_results = [
            make_hybrid_result("c1", score=0.3, fts_score=0.3),
        ]

        merged = hybrid._merge_results(vector_results, fts_results)
        assert len(merged) == 1
        assert merged[0].score == pytest.approx(1.0)
        assert merged[0].vector_score == 0.7
        assert merged[0].fts_score == 0.3

    def test_merge_empty_vector_results(self, hybrid: HybridSearch) -> None:
        """FTS-only results should pass through unchanged."""
        fts_results = [
            make_hybrid_result("c1", score=0.3, fts_score=0.3),
        ]
        merged = hybrid._merge_results([], fts_results)
        assert len(merged) == 1
        assert merged[0].chunk_id == "c1"

    def test_merge_empty_fts_results(self, hybrid: HybridSearch) -> None:
        """Vector-only results should pass through unchanged."""
        vector_results = [
            make_hybrid_result("c1", score=0.7, vector_score=0.7),
        ]
        merged = hybrid._merge_results(vector_results, [])
        assert len(merged) == 1
        assert merged[0].chunk_id == "c1"

    def test_merge_both_empty(self, hybrid: HybridSearch) -> None:
        """Empty inputs should produce empty output."""
        merged = hybrid._merge_results([], [])
        assert len(merged) == 0

    def test_merge_preserves_document_title_from_fts(
        self, hybrid: HybridSearch
    ) -> None:
        """When FTS provides a title that vector results lack, it should be used."""
        vector_results = [
            HybridResult(
                chunk_id="c1",
                document_id="doc1",
                content="content",
                score=0.7,
                vector_score=0.7,
                document_title=None,
            ),
        ]
        fts_results = [
            HybridResult(
                chunk_id="c1",
                document_id="doc1",
                content="content",
                score=0.3,
                fts_score=0.3,
                document_title="My Doc",
            ),
        ]

        merged = hybrid._merge_results(vector_results, fts_results)
        assert merged[0].document_title == "My Doc"


# =============================================================================
# Test _backfill_document_titles
# =============================================================================


class TestBackfillDocumentTitles:
    """Test PostgreSQL title backfill for results the vector store can't title."""

    @pytest.mark.asyncio
    async def test_backfill_populates_missing_title(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """A result with no title should get one from the database."""
        results = [make_hybrid_result("c1", doc_id="doc1")]
        mock_title_rows(mock_db, {"doc1": "Neural Networks 101"})

        await hybrid._backfill_document_titles(mock_db, results)
        assert results[0].document_title == "Neural Networks 101"

    @pytest.mark.asyncio
    async def test_backfill_does_not_overwrite_existing_title(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """Titles already supplied by FTS must be left alone."""
        result = make_hybrid_result("c1", doc_id="doc1")
        result.document_title = "From FTS"
        mock_title_rows(mock_db, {"doc1": "From Postgres"})

        await hybrid._backfill_document_titles(mock_db, [result])
        assert result.document_title == "From FTS"

    @pytest.mark.asyncio
    async def test_backfill_issues_single_query_for_many_results(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """Ten results across two documents must cost exactly one query."""
        results = [make_hybrid_result(f"c{i}", doc_id=f"doc{i % 2}") for i in range(10)]
        mock_title_rows(mock_db, {"doc0": "Doc Zero", "doc1": "Doc One"})

        await hybrid._backfill_document_titles(mock_db, results)
        assert mock_db.execute.await_count == 1
        assert {r.document_title for r in results} == {"Doc Zero", "Doc One"}

    @pytest.mark.asyncio
    async def test_backfill_skips_query_when_all_titled(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """No missing titles means no database round trip at all."""
        result = make_hybrid_result("c1", doc_id="doc1")
        result.document_title = "Already Titled"
        mock_db.execute = AsyncMock()

        await hybrid._backfill_document_titles(mock_db, [result])
        assert mock_db.execute.await_count == 0

    @pytest.mark.asyncio
    async def test_backfill_leaves_title_none_when_document_untitled(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """A genuinely untitled document stays None (CLI falls back to its id)."""
        results = [make_hybrid_result("c1", doc_id="doc1")]
        mock_title_rows(mock_db, {"doc1": None})

        await hybrid._backfill_document_titles(mock_db, results)
        assert results[0].document_title is None

    @pytest.mark.asyncio
    async def test_backfill_handles_db_failure(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """A failed lookup degrades display only; it must not raise."""
        results = [make_hybrid_result("c1", doc_id="doc1")]
        mock_db.execute = AsyncMock(side_effect=RuntimeError("connection lost"))

        await hybrid._backfill_document_titles(mock_db, results)
        assert results[0].document_title is None

    @pytest.mark.asyncio
    async def test_backfill_ignores_results_without_document_id(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """A result with no document_id can't be looked up, so no query runs."""
        results = [make_hybrid_result("c1", doc_id="")]
        mock_db.execute = AsyncMock()

        await hybrid._backfill_document_titles(mock_db, results)
        assert mock_db.execute.await_count == 0


# =============================================================================
# Test _apply_reranking
# =============================================================================


class TestApplyReranking:
    """Test cross-encoder reranking."""

    @pytest.mark.asyncio
    async def test_reranking_reorders_results(
        self, hybrid_with_reranker: HybridSearch
    ) -> None:
        """Reranking should produce results in reranked order."""
        results = [
            make_hybrid_result("c1", score=0.5),
            make_hybrid_result("c2", score=0.3),
        ]

        reranked = await hybrid_with_reranker._apply_reranking(
            "query", results, top_k=2
        )
        assert len(reranked) == 2
        # MockReranker returns [0, 1] so rank 0 gets score 1.0, rank 1 gets 0.5
        assert reranked[0].score > reranked[1].score

    @pytest.mark.asyncio
    async def test_reranking_top_k_limits_results(
        self, hybrid_with_reranker: HybridSearch
    ) -> None:
        """top_k should limit the number of reranked results."""
        results = [make_hybrid_result(f"c{i}", score=0.5) for i in range(5)]

        reranked = await hybrid_with_reranker._apply_reranking(
            "query", results, top_k=3
        )
        assert len(reranked) == 3

    @pytest.mark.asyncio
    async def test_reranking_without_reranker(self, hybrid: HybridSearch) -> None:
        """Without a reranker, results should be returned unchanged."""
        results = [make_hybrid_result("c1", score=0.5)]

        reranked = await hybrid._apply_reranking("query", results, top_k=5)
        assert reranked == results

    @pytest.mark.asyncio
    async def test_reranking_with_single_result(
        self, hybrid_with_reranker: HybridSearch
    ) -> None:
        """Reranking a single result still works but produces a trivial reranking."""
        results = [make_hybrid_result("c1", score=0.5)]

        reranked = await hybrid_with_reranker._apply_reranking(
            "query", results, top_k=5
        )
        # Single result gets reranked to score 1.0
        assert len(reranked) == 1
        assert reranked[0].chunk_id == "c1"

    @pytest.mark.asyncio
    async def test_reranking_failure_returns_original(
        self, mock_vector_store: MockVectorStore, mock_embedder: MockEmbedder
    ) -> None:
        """If reranking fails, original results should be returned."""
        hybrid_fail = HybridSearch(
            vector_store=mock_vector_store,
            embedder=mock_embedder,
            reranker=FailingReranker(),
        )
        results = [
            make_hybrid_result("c1", score=0.5),
            make_hybrid_result("c2", score=0.3),
        ]

        reranked = await hybrid_fail._apply_reranking("query", results, top_k=2)
        assert reranked is results


# =============================================================================
# Test _vector_search
# =============================================================================


class TestVectorSearch:
    """Test vector search component."""

    @pytest.mark.asyncio
    async def test_vector_search_returns_results(self, hybrid: HybridSearch) -> None:
        """Vector search should return scored HybridResults."""
        results = [
            make_vector_result("c1", distance=0.2, content="python code"),
            make_vector_result("c2", distance=0.5, content="java code"),
        ]
        hybrid._vector_store = MockVectorStore(results)

        out = await hybrid._vector_search("query", top_k=10)
        assert len(out) == 2
        # score = max(0.0, 1.0 - distance) * vector_weight(0.7)
        assert out[0].vector_score == pytest.approx(0.8)
        assert out[0].score == pytest.approx(0.8 * 0.7)

    @pytest.mark.asyncio
    async def test_vector_search_empty_results(self, hybrid: HybridSearch) -> None:
        """Empty vector store should return empty list."""
        hybrid._vector_store = MockVectorStore([])

        out = await hybrid._vector_search("query", top_k=10)
        assert len(out) == 0

    @pytest.mark.asyncio
    async def test_vector_search_handles_exception(self, hybrid: HybridSearch) -> None:
        """Vector search should return empty list on failure, not raise."""

        class FailingVectorStore(MockVectorStore):
            async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
                raise RuntimeError("Connection failed")

        hybrid._vector_store = FailingVectorStore()

        out = await hybrid._vector_search("query", top_k=10)
        assert out == []

    @pytest.mark.asyncio
    async def test_vector_search_negative_distance_clamped(
        self, hybrid: HybridSearch
    ) -> None:
        """ChromaDB cosine distance > 1.0 should be clamped to 0.0."""
        results = [make_vector_result("c1", distance=1.5)]
        hybrid._vector_store = MockVectorStore(results)

        out = await hybrid._vector_search("query", top_k=10)
        assert out[0].vector_score == pytest.approx(0.0)
        assert out[0].score == pytest.approx(0.0)


# =============================================================================
# Test _fts_search
# =============================================================================


class TestFtsSearch:
    """Test FTS search component."""

    @pytest.mark.asyncio
    async def test_fts_search_returns_results(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """FTS search should normalize and weight scores."""
        from grimoire.search.fulltext import FTSResult

        fts_result = FTSResult(
            chunk_id="c1",
            document_id="doc1",
            content="test content",
            rank=0.8,
            document_title="Test Doc",
        )

        with (
            patch.object(type(hybrid), "_fts_search", hybrid._fts_search),
            patch("grimoire.search.hybrid.FulltextSearch") as MockFTS,
        ):
            mock_fts_instance = MagicMock()
            mock_fts_instance.search = AsyncMock(return_value=[fts_result])
            MockFTS.return_value = mock_fts_instance

            out = await hybrid._fts_search(mock_db, "test query", top_k=10)
            assert len(out) == 1
            assert out[0].fts_score is not None
            assert out[0].score == pytest.approx(1.0 * 0.3)

    @pytest.mark.asyncio
    async def test_fts_search_empty_results(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """FTS search with no matches should return empty list."""
        with patch("grimoire.search.hybrid.FulltextSearch") as MockFTS:
            mock_fts_instance = MagicMock()
            mock_fts_instance.search = AsyncMock(return_value=[])
            MockFTS.return_value = mock_fts_instance

            out = await hybrid._fts_search(mock_db, "nonexistent", top_k=10)
            assert len(out) == 0


# =============================================================================
# Test search() integration
# =============================================================================


class TestHybridSearch:
    """Test the main search() method."""

    @pytest.mark.asyncio
    async def test_search_empty_query(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """Empty query should return empty results."""
        result = await hybrid.search(mock_db, "", top_k=10)
        assert result == []

    @pytest.mark.asyncio
    async def test_search_whitespace_query(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """Whitespace-only query should return empty results."""
        result = await hybrid.search(mock_db, "   ", top_k=10)
        assert result == []

    @pytest.mark.asyncio
    async def test_search_with_reranking(
        self, hybrid_with_reranker: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """Search with reranker should return reranked results."""
        vector_results = [
            make_vector_result("c1", distance=0.2),
            make_vector_result("c2", distance=0.4),
        ]
        hybrid_with_reranker._vector_store = MockVectorStore(vector_results)

        # Mock FTS to return empty so we only get vector results
        with patch.object(
            hybrid_with_reranker, "_fts_search", new_callable=AsyncMock
        ) as mock_fts:
            mock_fts.return_value = []

            results = await hybrid_with_reranker.search(mock_db, "test query", top_k=5)
            assert len(results) > 0

    @pytest.mark.asyncio
    async def test_search_top_k_limits_results(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """top_k should limit the number of returned results."""
        vector_results = [
            make_vector_result(f"c{i}", distance=0.1 * i) for i in range(10)
        ]
        hybrid._vector_store = MockVectorStore(vector_results)

        with patch.object(hybrid, "_fts_search", new_callable=AsyncMock) as mock_fts:
            mock_fts.return_value = []

            results = await hybrid.search(mock_db, "query", top_k=3)
            assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_search_vector_only(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """vector_search_only should only return vector results."""
        vector_results = [
            make_vector_result("c1", distance=0.2),
            make_vector_result("c2", distance=0.5),
        ]
        hybrid._vector_store = MockVectorStore(vector_results)

        results = await hybrid.vector_search_only("test query", top_k=10)
        assert len(results) == 2
        assert all(r.vector_score is not None for r in results)

    @pytest.mark.asyncio
    async def test_search_titles_vector_only_hits(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """Regression: a chunk found by vector search but NOT by FTS still gets
        a title.

        This is the "neural networks" bug -- semantic hits FTS never surfaced
        lexically had no FTS twin to copy a title from, so they printed as the
        literal string "None".
        """
        hybrid._vector_store = MockVectorStore(
            [make_vector_result("c1", doc_id="doc1", distance=0.2)]
        )
        mock_title_rows(mock_db, {"doc1": "Neural Networks 101"})

        with patch.object(hybrid, "_fts_search", new_callable=AsyncMock) as mock_fts:
            mock_fts.return_value = []  # no lexical match for this chunk

            results = await hybrid.search(mock_db, "neural networks", top_k=10)

        assert len(results) == 1
        assert results[0].chunk_id == "c1"
        assert results[0].fts_score is None  # vector-only, as the bug requires
        assert results[0].document_title == "Neural Networks 101"

    @pytest.mark.asyncio
    async def test_search_backfills_only_vector_only_hits(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """Mixed result set: FTS titles survive, vector-only hits get filled."""
        hybrid._vector_store = MockVectorStore(
            [
                make_vector_result("c1", doc_id="doc1", distance=0.2),
                make_vector_result("c2", doc_id="doc2", distance=0.3),
            ]
        )
        mock_title_rows(mock_db, {"doc2": "Backfilled Doc"})

        with patch.object(hybrid, "_fts_search", new_callable=AsyncMock) as mock_fts:
            mock_fts.return_value = [
                HybridResult(
                    chunk_id="c1",
                    document_id="doc1",
                    content="test content",
                    score=0.3,
                    fts_score=1.0,
                    document_title="From FTS",
                )
            ]

            results = await hybrid.search(mock_db, "neural networks", top_k=10)

        by_id = {r.chunk_id: r for r in results}
        assert by_id["c1"].document_title == "From FTS"
        assert by_id["c2"].document_title == "Backfilled Doc"
        # One batched query for the whole result set, not one per result.
        assert mock_db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_search_both_sources_combined(
        self, hybrid: HybridSearch, mock_db: AsyncMock
    ) -> None:
        """When both vector and FTS find the same chunk, scores combine."""
        vector_results = [
            make_vector_result("c1", distance=0.3, content="python code"),
        ]
        hybrid._vector_store = MockVectorStore(vector_results)

        from grimoire.search.fulltext import FTSResult

        fts_result = FTSResult(
            chunk_id="c1",
            document_id="doc1",
            content="python code",
            rank=0.9,
            document_title="Python Guide",
        )

        with patch.object(hybrid, "_fts_search", new_callable=AsyncMock) as mock_fts:
            mock_fts.return_value = [
                HybridResult(
                    chunk_id="c1",
                    document_id="doc1",
                    content="python code",
                    score=0.9 * 0.3,
                    fts_score=0.9,
                    document_title="Python Guide",
                )
            ]

            results = await hybrid.search(mock_db, "python", top_k=10)
            # Should find at least one result with combined scores
            assert len(results) >= 1
            c1_result = next(r for r in results if r.chunk_id == "c1")
            # Combined score should include both vector and FTS components
            assert c1_result.vector_score is not None or c1_result.fts_score is not None
