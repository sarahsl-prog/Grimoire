"""Tests for PostgreSQL Full-Text Search implementation.

Comprehensive tests covering:
- Happy path searches
- Edge cases (empty queries, special characters)
- Input validation
- Error handling
- Async behavior
- State management
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from grimoire.search.fulltext import (
    FTSQuery,
    FTSResult,
    FulltextSearch,
    escape_special_chars,
    parse_query,
    search_chunks,
    search_with_title,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest_asyncio.fixture
async def mock_session() -> AsyncGenerator[AsyncMock, None]:
    """Create a mock SQLAlchemy async session."""
    session = AsyncMock(spec=AsyncSession)
    yield session


@pytest.fixture
def sample_chunks() -> list[dict[str, Any]]:
    """Provide sample chunk data for tests."""
    return [
        {
            "chunk_id": "123e4567-e89b-12d3-a456-426614174000",
            "document_id": "123e4567-e89b-12d3-a456-426614174001",
            "content": "Python is a versatile programming language.",
            "rank": 0.95,
            "document_title": "Python Programming Guide",
        },
        {
            "chunk_id": "123e4567-e89b-12d3-a456-426614174002",
            "document_id": "123e4567-e89b-12d3-a456-426614174001",
            "content": "It supports multiple programming paradigms.",
            "rank": 0.85,
            "document_title": "Python Programming Guide",
        },
        {
            "chunk_id": "123e4567-e89b-12d3-a456-426614174003",
            "document_id": "123e4567-e89b-12d3-a456-426614174002",
            "content": "Machine learning with Python and scikit-learn.",
            "rank": 0.75,
            "document_title": "ML Handbook",
        },
    ]


# =============================================================================
# Test Escape Special Characters
# =============================================================================


class TestEscapeSpecialChars:
    """Tests for escape_special_chars function."""

    def test_no_special_chars(self) -> None:
        """Test string with no special characters."""
        query = "hello world"
        result = escape_special_chars(query)
        assert result == "hello world"

    def test_backslash_escape(self) -> None:
        """Test backslash escaping."""
        query = "path\\to\\file"
        result = escape_special_chars(query)
        assert result == "path\\\\to\\\\file"

    def test_single_quote_escape(self) -> None:
        """Test single quote escaping."""
        query = "it's working"
        result = escape_special_chars(query)
        assert result == "it''s working"

    def test_multiple_special_chars(self) -> None:
        """Test multiple special characters."""
        query = "it's a test\\path"
        result = escape_special_chars(query)
        assert "''" in result
        assert "\\\\" in result

    def test_empty_string(self) -> None:
        """Test empty string."""
        result = escape_special_chars("")
        assert result == ""


# =============================================================================
# Test Query Parsing
# =============================================================================


class TestParseQuery:
    """Tests for parse_query function."""

    def test_single_word(self) -> None:
        """Test single word query."""
        result = parse_query("python")
        assert result == "python"

    def test_multiple_words_implicit_and(self) -> None:
        """Test multiple words with implicit AND."""
        result = parse_query("python programming")
        assert result == "python & programming"

    def test_explicit_and_operator(self) -> None:
        """Test explicit AND operator."""
        result = parse_query("python AND django")
        assert result == "python & django"

    def test_or_operator(self) -> None:
        """Test OR operator."""
        result = parse_query("python OR javascript")
        assert result == "python | javascript"

    def test_phrase_search(self) -> None:
        """Test phrase search with quotes."""
        result = parse_query('"hello world"')
        assert result == "hello <-> world"

    def test_phrase_single_word(self) -> None:
        """Test phrase search with single word."""
        result = parse_query('"python"')
        assert result == "python"

    def test_mixed_operators_and_phrases(self) -> None:
        """Test mixed operators and phrases."""
        result = parse_query('python AND "machine learning" OR django')
        assert "python" in result
        assert "&" in result
        assert "machine <-> learning" in result or "(machine <-> learning)" in result
        assert "|" in result
        assert "django" in result

    def test_no_operators_mode(self) -> None:
        """Test query parsing with operators disabled."""
        result = parse_query("python OR django", operators=False)
        assert result == "python & OR & django"

    def test_empty_string(self) -> None:
        """Test empty string query."""
        result = parse_query("")
        assert result == ""

    def test_whitespace_only(self) -> None:
        """Test whitespace-only query."""
        result = parse_query("   ")
        assert result == ""

    def test_case_insensitive_operators(self) -> None:
        """Test case-insensitive AND/OR operators."""
        result_lower = parse_query("python and django")
        result_upper = parse_query("python AND django")
        assert result_lower == result_upper


# =============================================================================
# Test FTSQuery Model
# =============================================================================


class TestFTSQuery:
    """Tests for FTSQuery dataclass."""

    def test_create_simple_query(self) -> None:
        """Test creating FTSQuery from simple query."""
        ftq = FTSQuery.create("python programming")
        assert ftq.query == "python programming"
        assert ftq.parsed == "python & programming"
        assert ftq.is_phrase is False

    def test_create_phrase_query(self) -> None:
        """Test creating FTSQuery from phrase query."""
        ftq = FTSQuery.create('"hello world"')
        assert ftq.query == '"hello world"'
        assert ftq.parsed == "hello <-> world"
        assert ftq.is_phrase is True

    def test_create_with_operators_disabled(self) -> None:
        """Test creating FTSQuery with operators disabled."""
        ftq = FTSQuery.create("python OR django", operators=False)
        assert "python" in ftq.parsed
        assert "django" in ftq.parsed
        # OR should be treated as literal word
        assert "|" not in ftq.parsed


# =============================================================================
# Test FTSResult Model
# =============================================================================


class TestFTSResult:
    """Tests for FTSResult dataclass."""

    def test_basic_result(self) -> None:
        """Test basic FTSResult creation."""
        result = FTSResult(
            chunk_id="123e4567-e89b-12d3-a456-426614174000",
            document_id="123e4567-e89b-12d3-a456-426614174001",
            content="Test content",
            rank=0.95,
        )
        assert result.chunk_id == "123e4567-e89b-12d3-a456-426614174000"
        assert result.document_id == "123e4567-e89b-12d3-a456-426614174001"
        assert result.content == "Test content"
        assert result.rank == 0.95
        assert result.document_title is None

    def test_result_with_title(self) -> None:
        """Test FTSResult with document title."""
        result = FTSResult(
            chunk_id="123e4567-e89b-12d3-a456-426614174000",
            document_id="123e4567-e89b-12d3-a456-426614174001",
            content="Test content",
            rank=0.95,
            document_title="Test Document",
        )
        assert result.document_title == "Test Document"


# =============================================================================
# Test FulltextSearch Class
# =============================================================================


class TestFulltextSearch:
    """Tests for FulltextSearch class."""

    @pytest_asyncio.fixture
    async def mock_search_results(
        self, sample_chunks: list[dict[str, Any]]
    ) -> list[tuple[Any, ...]]:
        """Create mock search results."""
        # Create named tuples to simulate SQLAlchemy result rows
        from collections import namedtuple

        Row = namedtuple(
            "Row",
            ["chunk_id", "document_id", "content", "rank", "document_title"],
        )
        return [
            Row(
                chunk_id=c["chunk_id"],
                document_id=c["document_id"],
                content=c["content"],
                rank=c["rank"],
                document_title=c["document_title"],
            )
            for c in sample_chunks
        ]

    @pytest.mark.asyncio
    async def test_init(self, mock_session: AsyncMock) -> None:
        """Test FulltextSearch initialization."""
        fts = FulltextSearch(mock_session)
        assert fts.session == mock_session
        assert fts.language == "english"
        assert fts.include_title_weight is True

        fts_custom = FulltextSearch(
            mock_session, language="spanish", include_title_weight=False
        )
        assert fts_custom.language == "spanish"
        assert fts_custom.include_title_weight is False

    @pytest.mark.asyncio
    async def test_search_empty_query(self, mock_session: AsyncMock) -> None:
        """Test search with empty query returns empty list."""
        fts = FulltextSearch(mock_session)
        results = await fts.search("")
        assert results == []

        results_whitespace = await fts.search("   ")
        assert results_whitespace == []

    @pytest.mark.asyncio
    async def test_search_chunks_only_empty(self, mock_session: AsyncMock) -> None:
        """Test search_chunks_only with empty query."""
        fts = FulltextSearch(mock_session)
        results = await fts.search_chunks_only("")
        assert results == []


# =============================================================================
# Test Edge Cases and Error Handling
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_parse_query_only_whitespace(self) -> None:
        """Test parsing query with only whitespace."""
        result = parse_query("   \t\n   ")
        assert result == ""

    def test_parse_query_special_characters(self) -> None:
        """Test parsing query with various special characters."""
        # These shouldn't crash, though results may vary
        queries = [
            "test@email.com",
            "file://path/to/file",
            "price \u003e 100",
            "result #1",
            "C++ programming",
        ]
        for query in queries:
            result = parse_query(query)
            assert isinstance(result, str)

    def test_parse_query_unicode(self) -> None:
        """Test parsing query with unicode characters."""
        result = parse_query("日本語の検索")
        assert result is not None
        assert isinstance(result, str)

    def test_parse_query_long_query(self) -> None:
        """Test parsing very long query."""
        long_query = "word " * 1000
        result = parse_query(long_query)
        assert isinstance(result, str)
        # Should contain many & operators
        assert result.count(" & ") > 900


# =============================================================================
# Test Async Behavior
# =============================================================================


class TestAsyncBehavior:
    """Tests for async behavior."""

    @pytest.mark.asyncio
    async def test_search_is_async(self, mock_session: AsyncMock) -> None:
        """Test that search method is properly async."""
        fts = FulltextSearch(mock_session)
        # Mock the execute method
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await fts.search("test query")
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_highlight_is_async(self, mock_session: AsyncMock) -> None:
        """Test that highlight method is properly async."""
        fts = FulltextSearch(mock_session)
        # Mock the execute method
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = "Highlighted content"
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await fts.highlight(
            "123e4567-e89b-12d3-a456-426614174000", "test"
        )
        assert result is not None


# =============================================================================
# Test Integration-style Tests (with mocked DB)
# =============================================================================


class TestSearchIntegration:
    """Integration-style tests with mocked database."""

    @pytest.mark.asyncio
    async def test_search_returns_ranked_results(self) -> None:
        """Test that search returns properly ranked results."""
        from collections import namedtuple

        # Create mock session
        session = AsyncMock(spec=AsyncSession)

        # Create mock result rows
        Row = namedtuple(
            "Row", ["chunk_id", "document_id", "content", "rank", "document_title"]
        )
        mock_rows = [
            Row(
                chunk_id="id1",
                document_id="doc1",
                content="Python programming",
                rank=0.95,
                document_title="Python Guide",
            ),
            Row(
                chunk_id="id2",
                document_id="doc1",
                content="Advanced Python",
                rank=0.85,
                document_title="Python Guide",
            ),
        ]

        mock_result = MagicMock()
        mock_result.all.return_value = mock_rows
        session.execute = AsyncMock(return_value=mock_result)

        fts = FulltextSearch(session)
        results = await fts.search("python", top_k=10)

        assert len(results) == 2
        assert results[0].rank > results[1].rank  # Should be sorted by rank

    @pytest.mark.asyncio
    async def test_search_chunks_only_integration(self) -> None:
        """Test chunks-only search."""
        from collections import namedtuple

        session = AsyncMock(spec=AsyncSession)

        Row = namedtuple(
            "Row", ["chunk_id", "document_id", "content", "rank", "document_title"]
        )
        mock_rows = [
            Row(
                chunk_id="id1",
                document_id="doc1",
                content="Content only",
                rank=0.9,
                document_title="Title",
            ),
        ]

        mock_result = MagicMock()
        mock_result.all.return_value = mock_rows
        session.execute = AsyncMock(return_value=mock_result)

        fts = FulltextSearch(session, include_title_weight=False)
        results = await fts.search_chunks_only("content", top_k=5)

        assert len(results) == 1
        assert results[0].content == "Content only"

    @pytest.mark.asyncio
    async def test_highlight_integration(self) -> None:
        """Test highlight functionality."""
        session = AsyncMock(spec=AsyncSession)

        # Mock for getting chunk content
        content_result = MagicMock()
        content_result.scalar_one_or_none.return_value = "Python is great"

        # Mock for ts_headline
        headline_result = MagicMock()
        headline_result.scalar_one_or_none.return_value = (
            "<mark>Python</mark> is great"
        )

        # Set up side effect for multiple executes
        session.execute = AsyncMock(side_effect=[content_result, headline_result])

        fts = FulltextSearch(session)
        result = await fts.highlight("id1", "python")

        assert result is not None
        assert "<mark>" in result

    @pytest.mark.asyncio
    async def test_highlight_no_content(self) -> None:
        """Test highlight when chunk doesn't exist."""
        session = AsyncMock(spec=AsyncSession)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        fts = FulltextSearch(session)
        result = await fts.highlight("nonexistent-id", "python")

        assert result is None


# =============================================================================
# Test Helper Functions
# =============================================================================


class TestHelperFunctions:
    """Tests for module-level helper functions."""

    @pytest.mark.asyncio
    async def test_search_chunks_helper(self) -> None:
        """Test search_chunks helper function."""
        session = AsyncMock(spec=AsyncSession)

        from collections import namedtuple

        Row = namedtuple(
            "Row", ["chunk_id", "document_id", "content", "rank", "document_title"]
        )
        mock_rows = [
            Row(
                chunk_id="id1",
                document_id="doc1",
                content="Test content",
                rank=0.8,
                document_title="Test Doc",
            ),
        ]

        mock_result = MagicMock()
        mock_result.all.return_value = mock_rows
        session.execute = AsyncMock(return_value=mock_result)

        results = await search_chunks(session, "test", top_k=5)

        assert len(results) == 1
        assert results[0].chunk_id == "id1"

    @pytest.mark.asyncio
    async def test_search_with_title_helper(self) -> None:
        """Test search_with_title helper function."""
        session = AsyncMock(spec=AsyncSession)

        from collections import namedtuple

        Row = namedtuple(
            "Row", ["chunk_id", "document_id", "content", "rank", "document_title"]
        )
        mock_rows = [
            Row(
                chunk_id="id1",
                document_id="doc1",
                content="Test with title weight",
                rank=0.9,
                document_title="Important Doc",
            ),
        ]

        mock_result = MagicMock()
        mock_result.all.return_value = mock_rows
        session.execute = AsyncMock(return_value=mock_result)

        results = await search_with_title(session, "test", top_k=5)

        assert len(results) == 1
        assert results[0].document_title == "Important Doc"


# =============================================================================
# Test State Management
# =============================================================================


class TestStateManagement:
    """Tests for state management."""

    @pytest.mark.asyncio
    async def test_session_state_preserved(self) -> None:
        """Test that session state is preserved during search."""
        session = AsyncMock(spec=AsyncSession)
        fts = FulltextSearch(session)

        assert fts.session is session

        # Multiple searches should use same session
        mock_result = MagicMock()
        mock_result.all.return_value = []
        session.execute = AsyncMock(return_value=mock_result)

        await fts.search("query1")
        await fts.search("query2")

        assert session.execute.call_count == 2

    def test_language_configuration(self) -> None:
        """Test language configuration is stored correctly."""
        session = MagicMock()
        fts = FulltextSearch(session, language="spanish")
        assert fts.language == "spanish"


# =============================================================================
# Test Query Coverage
# =============================================================================


class TestQueryCoverage:
    """Tests to ensure various query patterns work correctly."""

    def test_simple_term(self) -> None:
        """Test simple single term."""
        result = parse_query("python")
        assert result == "python"

    def test_multiple_terms(self) -> None:
        """Test multiple terms."""
        result = parse_query("python programming")
        assert "python" in result
        assert "programming" in result
        assert "&" in result

    def test_or_expression(self) -> None:
        """Test OR expression."""
        result = parse_query("python OR java")
        assert result == "python | java"

    def test_and_expression(self) -> None:
        """Test AND expression."""
        result = parse_query("python AND django")
        assert result == "python & django"

    def test_phrase_expression(self) -> None:
        """Test phrase expression."""
        result = parse_query('"machine learning"')
        assert result == "machine <-> learning"

    def test_complex_expression(self) -> None:
        """Test complex expression with operators and phrases."""
        result = parse_query('python AND "data science" OR java')
        assert "python" in result
        assert "&" in result
        assert "|" in result


# =============================================================================
# Coverage and Metrics
# =============================================================================

def test_module_has_all_exports() -> None:
    """Test that module exports expected functions and classes."""
    import grimoire.search.fulltext as fts_module

    expected_exports = [
        "FTSQuery",
        "FTSResult",
        "FulltextSearch",
        "escape_special_chars",
        "parse_query",
        "search_chunks",
        "search_with_title",
    ]

    for export in expected_exports:
        assert hasattr(fts_module, export), f"Missing export: {export}"
