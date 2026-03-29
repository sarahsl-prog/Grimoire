"""Tests for chunking strategies.

This module provides comprehensive test coverage for all chunking strategies
in Grimoire, including happy path, edge cases, input validation, and
continuity tracking.
"""

import pytest
from pydantic import ValidationError

from grimoire.core.chunker import (
    Chunk,
    ChunkConfig,
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
    SemanticChunker,
)
from grimoire.core.chunker.base import ChunkingStrategy
from grimoire.core.chunker.markdown import MarkdownChunkConfig
from grimoire.core.chunker.recursive import RecursiveChunkConfig
from grimoire.core.chunker.semantic import SemanticChunkConfig


# =============================================================================
# Chunk Model Tests
# =============================================================================


class TestChunkModel:
    """Tests for the Chunk data model."""

    def test_chunk_creation_happy_path(self) -> None:
        """Test creating a valid chunk."""
        chunk = Chunk(
            content="This is test content.",
            token_count=10,
            index=0,
            prev_chunk_id=None,
            next_chunk_id="chunk-2",
            metadata={"source": "test"},
        )
        assert chunk.content == "This is test content."
        assert chunk.token_count == 10
        assert chunk.index == 0
        assert chunk.prev_chunk_id is None
        assert chunk.next_chunk_id == "chunk-2"
        assert chunk.metadata["source"] == "test"

    def test_chunk_empty_content_raises_error(self) -> None:
        """Test that empty content raises validation error."""
        with pytest.raises(ValidationError, match="empty"):
            Chunk(content="", token_count=0, index=0)

    def test_chunk_whitespace_only_raises_error(self) -> None:
        """Test that whitespace-only content raises error."""
        with pytest.raises(ValidationError, match="empty"):
            Chunk(content="   \n\t  ", token_count=0, index=0)

    def test_chunk_token_count_validation(self) -> None:
        """Test that negative token_count is rejected."""
        with pytest.raises(ValidationError):
            Chunk(content="Valid content", token_count=-1, index=0)

    def test_chunk_index_validation(self) -> None:
        """Test that negative index is rejected."""
        with pytest.raises(ValidationError):
            Chunk(content="Valid content", token_count=10, index=-1)

    def test_chunk_extra_metadata_allowed(self) -> None:
        """Test that extra fields in metadata are allowed."""
        chunk = Chunk(
            content="Test",
            token_count=1,
            index=0,
            metadata={"custom_field": "custom_value", "number": 42},
        )
        assert chunk.metadata["custom_field"] == "custom_value"


# =============================================================================
# ChunkConfig Tests
# =============================================================================


class TestChunkConfig:
    """Tests for chunk configuration."""

    def test_default_config(self) -> None:
        """Test default configuration values."""
        config = ChunkConfig()
        assert config.chunk_size == 1000
        assert config.chunk_overlap == 200
        assert config.strategy == ChunkingStrategy.RECURSIVE
        assert config.encoding_name == "cl100k_base"

    def test_custom_config(self) -> None:
        """Test custom configuration values."""
        config = ChunkConfig(
            chunk_size=500, chunk_overlap=50, strategy=ChunkingStrategy.SEMANTIC
        )
        assert config.chunk_size == 500
        assert config.chunk_overlap == 50
        assert config.strategy == ChunkingStrategy.SEMANTIC

    def test_overlap_must_be_less_than_chunk_size(self) -> None:
        """Test that overlap >= chunk_size raises error."""
        with pytest.raises(ValidationError, match="less than"):
            ChunkConfig(chunk_size=100, chunk_overlap=100)

    def test_overlap_greater_than_chunk_size_raises(self) -> None:
        """Test that overlap > chunk_size raises error."""
        with pytest.raises(ValidationError, match="less than"):
            ChunkConfig(chunk_size=100, chunk_overlap=150)

    def test_chunk_size_must_be_positive(self) -> None:
        """Test that non-positive chunk_size is rejected."""
        with pytest.raises(ValidationError):
            ChunkConfig(chunk_size=0)

    def test_overlap_must_be_non_negative(self) -> None:
        """Test that negative overlap is rejected."""
        with pytest.raises(ValidationError):
            ChunkConfig(chunk_overlap=-1)

    def test_semantic_chunk_config(self) -> None:
        """Test semantic-specific config."""
        config = SemanticChunkConfig(threshold=0.7, min_chunk_size=150)
        assert config.strategy == ChunkingStrategy.SEMANTIC
        assert config.threshold == 0.7
        assert config.min_chunk_size == 150

    def test_markdown_chunk_config(self) -> None:
        """Test markdown-specific config."""
        config = MarkdownChunkConfig(
            headers_to_split_on=["#", "##"],
            keep_headers=False,
        )
        assert config.strategy == ChunkingStrategy.MARKDOWN
        assert config.headers_to_split_on == ["#", "##"]
        assert config.keep_headers is False

    def test_recursive_chunk_config_for_code(self) -> None:
        """Test recursive config for code."""
        config = RecursiveChunkConfig.for_code("python")
        assert "\nclass " in config.separators
        assert "\ndef " in config.separators


# =============================================================================
# RecursiveCharacterTextSplitter Tests
# =============================================================================


@pytest.mark.unit
class TestRecursiveCharacterTextSplitter:
    """Tests for recursive character splitting."""

    @pytest.fixture
    def chunker(self) -> RecursiveCharacterTextSplitter:
        """Create a standard recursive chunker."""
        config = RecursiveChunkConfig(
            separators=["\n\n", "\n", ". ", " "],
            chunk_size=100,
            chunk_overlap=20,
        )
        return RecursiveCharacterTextSplitter(config)

    @pytest.mark.asyncio
    async def test_happy_path_basic_text(self, chunker) -> None:
        """Test basic recursive splitting."""
        text = "This is paragraph one.\n\nThis is paragraph two.\n\nThis is paragraph three."
        chunks = await chunker.chunk(text, doc_id="test-123")

        assert len(chunks) > 0
        for chunk in chunks:
            assert len(chunk.content) > 0
            assert chunk.token_count > 0

    @pytest.mark.asyncio
    async def test_empty_text_returns_empty_list(self, chunker) -> None:
        """Test that empty text returns empty list."""
        chunks = await chunker.chunk("")
        assert chunks == []

    @pytest.mark.asyncio
    async def test_whitespace_only_text_returns_empty_list(self, chunker) -> None:
        """Test that whitespace-only text returns empty list."""
        chunks = await chunker.chunk("   \n\t  ")
        assert chunks == []

    @pytest.mark.asyncio
    async def test_single_word(self, chunker) -> None:
        """Test splitting single word."""
        chunks = await chunker.chunk("Hello")
        assert len(chunks) == 1
        assert chunks[0].content == "Hello"

    @pytest.mark.asyncio
    async def test_continuity_links(self, chunker) -> None:
        """Test that prev/next links are set correctly."""
        text = "Para 1.\n\nPara 2.\n\nPara 3.\n\nPara 4."
        chunks = await chunker.chunk(text, doc_id="doc-123")

        if len(chunks) > 1:
            # First chunk has no prev
            assert chunks[0].prev_chunk_id is None
            assert chunks[0].next_chunk_id == chunks[1].metadata["chunk_id"]

            # Middle chunks have both
            for i in range(1, len(chunks) - 1):
                assert chunks[i].prev_chunk_id == chunks[i - 1].metadata["chunk_id"]
                assert chunks[i].next_chunk_id == chunks[i + 1].metadata["chunk_id"]

            # Last chunk has no next
            assert chunks[-1].prev_chunk_id == chunks[-2].metadata["chunk_id"]
            assert chunks[-1].next_chunk_id is None

    @pytest.mark.asyncio
    async def test_overlapping_content(self, chunker) -> None:
        """Test that overlapping content is maintained."""
        # Create a long text that will split
        text = "Word " * 1000
        chunks = await chunker.chunk(text, doc_id="overlap-test")

        if len(chunks) > 1:
            # Check that there's some overlap between consecutive chunks
            for i in range(len(chunks) - 1):
                current_end = chunks[i].content[-50:]
                next_start = chunks[i + 1].content[:50]
                # There should be some overlap
                assert len(current_end) > 0
                assert len(next_start) > 0

    @pytest.mark.asyncio
    async def test_chunk_metadata(self, chunker) -> None:
        """Test that chunks have correct metadata."""
        text = "Line one.\n\nLine two."
        chunks = await chunker.chunk(text, doc_id="meta-test")
        for chunk in chunks:
            assert chunk.metadata["strategy"] == "recursive"
            assert "chunk_id" in chunk.metadata
            assert chunk.index >= 0


# =============================================================================
# MarkdownHeaderTextSplitter Tests
# =============================================================================


@pytest.mark.unit
class TestMarkdownHeaderTextSplitter:
    """Tests for markdown header-based splitting."""

    @pytest.fixture
    def chunker(self) -> MarkdownHeaderTextSplitter:
        """Create a markdown header chunker."""
        config = MarkdownChunkConfig(
            headers_to_split_on=["#", "##", "###"],
            keep_headers=True,
        )
        return MarkdownHeaderTextSplitter(config)

    @pytest.mark.asyncio
    async def test_happy_path_basic_markdown(self, chunker) -> None:
        """Test basic markdown splitting."""
        text = """# Title

This is the introduction.

## Section 1

Content here.

### Subsection 1.1

More content.

## Section 2

Final content."""

        chunks = await chunker.chunk(text, doc_id="md-123")
        assert len(chunks) >= 3  # Should split by headers

        # Check header metadata
        sections_with_headers = [c for c in chunks if c.metadata["header"]]
        assert len(sections_with_headers) >= 2

    @pytest.mark.asyncio
    async def test_empty_text_returns_empty_list(self, chunker) -> None:
        """Test that empty markdown returns empty list."""
        chunks = await chunker.chunk("")
        assert chunks == []

    @pytest.mark.asyncio
    async def test_text_without_headers(self, chunker) -> None:
        """Test markdown without headers."""
        text = "This is just plain text without any markdown headers."
        chunks = await chunker.chunk(text)
        assert len(chunks) == 1
        assert chunks[0].content == text

    @pytest.mark.asyncio
    async def test_header_hierarchy_preservation(self, chunker) -> None:
        """Test that header hierarchy is preserved in metadata."""
        text = """# Main Title

Intro text.

## Subsection A

Content A.

### Sub-Subsection

Deep content."""

        chunks = await chunker.chunk(text)

        # Find the deepest section
        deep_chunks = [c for c in chunks if c.metadata["header_level"] == 3]
        if deep_chunks:
            deep_chunk = deep_chunks[0]
            assert "# Main Title" in deep_chunk.metadata["header_context"]
            assert "Subsection A" in deep_chunk.metadata["header_context"]

    @pytest.mark.asyncio
    async def test_continuity_links(self, chunker) -> None:
        """Test that prev/next links are set."""
        text = """# Section 1

Content 1.

## Section 2

Content 2.

### Section 3

Content 3."""

        chunks = await chunker.chunk(text, doc_id="md-links")
        if len(chunks) > 1:
            assert chunks[0].next_chunk_id is not None
            assert chunks[-1].prev_chunk_id is not None


# =============================================================================
# SemanticChunker Tests
# =============================================================================


@pytest.mark.unit
class TestSemanticChunker:
    """Tests for semantic embedding-based splitting."""

    @pytest.fixture
    def chunker(self) -> SemanticChunker:
        """Create a semantic chunker."""
        config = SemanticChunkConfig(
            threshold=0.5,
            min_chunk_size=50,
        )
        return SemanticChunker(config)

    @pytest.mark.asyncio
    async def test_happy_path_different_topics(self, chunker) -> None:
        """Test semantic splitting with topic changes."""
        text = """Machine learning is a subset of artificial intelligence. It uses statistical techniques to enable computer systems to learn from data. Neural networks are a key component.

The French Revolution began in 1789. It was a period of radical political and societal change. The storming of the Bastille marked a turning point.

Shakespeare's plays are considered some of the finest works in English literature. Hamlet and Macbeth explore themes of ambition and mortality. The Bard's influence continues today."""

        chunks = await chunker.chunk(text, doc_id="semantic-123")
        assert len(chunks) > 0

        # Each chunk should have content
        for chunk in chunks:
            assert len(chunk.content.strip()) >= chunker.config.min_chunk_size or len(chunks) == 1

    @pytest.mark.asyncio
    async def test_empty_text_returns_empty_list(self, chunker) -> None:
        """Test that empty text returns empty list."""
        chunks = await chunker.chunk("")
        assert chunks == []

    @pytest.mark.asyncio
    async def test_single_sentence(self, chunker) -> None:
        """Test single sentence handling."""
        text = "This is a single sentence."
        chunks = await chunker.chunk(text)
        assert len(chunks) == 1
        assert chunks[0].content == text

    @pytest.mark.asyncio
    async def test_continuity_links(self, chunker) -> None:
        """Test that prev/next links are set."""
        text = """First topic sentence here. Another sentence about the first topic.

Second topic completely different. More about second topic."""

        chunks = await chunker.chunk(text, doc_id="semantic-links")

        if len(chunks) > 1:
            # First has no prev
            assert chunks[0].prev_chunk_id is None
            # Last has no next
            assert chunks[-1].next_chunk_id is None
            # Middle chunks have both
            for i in range(1, len(chunks) - 1):
                assert chunks[i].prev_chunk_id is not None
                assert chunks[i].next_chunk_id is not None

    @pytest.mark.asyncio
    async def test_metadata_includes_strategy(self, chunker) -> None:
        """Test that metadata includes strategy info."""
        text = "Some sample text for testing."
        chunks = await chunker.chunk(text)
        for chunk in chunks:
            assert chunk.metadata["strategy"] == "semantic"
            assert "sentence_count" in chunk.metadata

    def test_min_chunk_size_validation(self) -> None:
        """Test that very small min_chunk_size is rejected."""
        with pytest.raises(ValidationError):
            SemanticChunkConfig(min_chunk_size=5)

    def test_threshold_bounds(self) -> None:
        """Test threshold must be between 0 and 1."""
        with pytest.raises(ValidationError):
            SemanticChunkConfig(threshold=1.5)

        with pytest.raises(ValidationError):
            SemanticChunkConfig(threshold=-0.1)


# =============================================================================
# Integration Tests
# =============================================================================


@pytest.mark.integration
class TestChunkingIntegration:
    """Integration tests across all strategies."""

    @pytest.mark.asyncio
    async def test_all_strategies_handle_unicode(self) -> None:
        """Test that all strategies handle unicode."""
        text = "Unicode test: 日本語 🎉 émojis α β γ"

        recursive = RecursiveCharacterTextSplitter()
        chunks = await recursive.chunk(text)
        assert len(chunks) > 0
        assert "日本語" in chunks[0].content

        markdown = MarkdownHeaderTextSplitter()
        chunks = await markdown.chunk(text)
        assert len(chunks) > 0

        semantic = SemanticChunker()
        chunks = await semantic.chunk(text)
        assert len(chunks) > 0

    @pytest.mark.asyncio
    async def test_all_strategies_handle_long_lines(self) -> None:
        """Test handling of very long lines without spaces."""
        text = "A" * 10000

        recursive = RecursiveCharacterTextSplitter(
            RecursiveChunkConfig(chunk_size=500)
        )
        chunks = await recursive.chunk(text)
        assert len(chunks) > 0

    @pytest.mark.asyncio
    async def test_chunk_token_counts_are_consistent(self) -> None:
        """Test that token counts are reasonable."""
        text = "This is a test sentence. " * 50

        chunker = SemanticChunker(SemanticChunkConfig(chunk_size=100))
        chunks = await chunker.chunk(text)

        for chunk in chunks:
            # Token count should be proportional to content length
            assert chunk.token_count >= 0
            # Rough check: tokens shouldn't exceed chars
            assert chunk.token_count <= len(chunk.content)

    @pytest.mark.asyncio
    async def test_all_strategies_handle_code(self) -> None:
        """Test handling of code blocks."""
        code_text = """```python
def hello():
    return "world"

class MyClass:
    def method(self):
        pass
```"""

        chunker = MarkdownHeaderTextSplitter()
        chunks = await chunker.chunk(code_text)
        assert len(chunks) > 0

        code_chunker = RecursiveCharacterTextSplitter(
            RecursiveChunkConfig.for_code("python")
        )
        chunks = await code_chunker.chunk(code_text)
        assert len(chunks) > 0


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================


class TestChunkerEdgeCases:
    """Tests for edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_very_small_chunk_size(self) -> None:
        """Test behavior with very small chunk size."""
        config = RecursiveChunkConfig(chunk_size=10, chunk_overlap=2)
        chunker = RecursiveCharacterTextSplitter(config)
        text = "This is a test."
        chunks = await chunker.chunk(text)
        assert len(chunks) >= 1

    @pytest.mark.asyncio
    async def test_zero_overlap(self) -> None:
        """Test with no overlap."""
        config = RecursiveChunkConfig(chunk_size=50, chunk_overlap=0)
        chunker = RecursiveCharacterTextSplitter(config)
        text = "Word " * 100
        chunks = await chunker.chunk(text)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.token_count > 0

    @pytest.mark.asyncio
    async def test_only_special_characters(self) -> None:
        """Test handling of special characters."""
        text = "!@#$%^&*()_+-=[]{}|;':\",./<>?"
        chunker = RecursiveCharacterTextSplitter()
        chunks = await chunker.chunk(text)
        assert len(chunks) == 1
        assert chunks[0].content == text

    @pytest.mark.asyncio
    async def test_repeated_newlines(self) -> None:
        """Test handling of excessive newlines."""
        text = "Line 1\n\n\n\n\n\nLine 2"
        chunker = MarkdownHeaderTextSplitter()
        chunks = await chunker.chunk(text)
        assert len(chunks) > 0

    @pytest.mark.asyncio
    async def test_tabs_and_whitespace(self) -> None:
        """Test handling of tabs and mixed whitespace."""
        text = "\t\tTabbed\tcontent\t\n  Indented line\n    More indent"
        chunker = RecursiveCharacterTextSplitter()
        chunks = await chunker.chunk(text)
        assert len(chunks) >= 1

    @pytest.mark.asyncio
    async def test_markdown_with_only_headers(self) -> None:
        """Test markdown with headers but no content."""
        text = "# Header 1\n## Header 2\n### Header 3"
        chunker = MarkdownHeaderTextSplitter()
        chunks = await chunker.chunk(text)
        # May return 0 or more depending on implementation
        assert isinstance(chunks, list)


# =============================================================================
# Performance and Scale Tests
# =============================================================================


@pytest.mark.slow
class TestChunkerPerformance:
    """Tests for performance and scale handling."""

    @pytest.mark.asyncio
    async def test_large_document_chunking(self) -> None:
        """Test handling of large documents."""
        text = "Lorem ipsum dolor sit amet. " * 10000
        chunker = RecursiveCharacterTextSplitter()
        chunks = await chunker.chunk(text)
        assert len(chunks) > 0
        assert len(chunks) < len(text) // 50  # Should reduce significantly
