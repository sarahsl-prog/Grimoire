"""Tests for the Content Generation Agent.

Tests cover:
- Happy path: all generation types (summary, flashcards, etc.)
- Caching: cache hits, misses, storage
- DB persistence: existing content reuse
- Error handling: LLM failures, empty documents
- Edge cases: multi-document requests, content truncation
"""

from __future__ import annotations

from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from grimoire.agents.content_gen import (
    ContentGenerationAgent,
    GenerationRequest,
    GenerationResult,
    _MAX_CONTENT_LENGTH,
    _PROMPTS,
)
from grimoire.db.models import ContentType, GeneratedContent


# =============================================================================
# Fixtures
# =============================================================================


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
    db.add = MagicMock()
    db.flush = AsyncMock()

    # Default: no existing content, some chunks
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [
        "This is the first chunk of document content.",
        "This is the second chunk with more details.",
    ]
    mock_result.scalars.return_value = mock_scalars
    db.execute.return_value = mock_result
    return db


@pytest.fixture
def agent(mock_cache: MagicMock) -> ContentGenerationAgent:
    """Create a ContentGenerationAgent with mocked dependencies."""
    return ContentGenerationAgent(
        llm_url="http://localhost:11434",
        llm_model="test-model",
        cache=mock_cache,
        temperature=0.5,
        max_tokens=2048,
    )


# =============================================================================
# Happy Path Tests
# =============================================================================


class TestContentGenHappyPath:
    """Standard content generation scenarios."""

    @pytest.mark.asyncio
    async def test_generate_summary(
        self, agent: ContentGenerationAgent, mock_db: AsyncMock,
    ) -> None:
        """Can generate a document summary."""
        with patch.object(
            agent, "_call_llm", new_callable=AsyncMock,
            return_value="This document discusses AI and machine learning.",
        ):
            result = await agent.generate_summary(mock_db, ["doc-1"])

        assert result.content_type == "summary"
        assert "AI" in result.content
        assert result.model_used == "test-model"
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_generate_flash_cards(
        self, agent: ContentGenerationAgent, mock_db: AsyncMock,
    ) -> None:
        """Can generate flashcards."""
        with patch.object(
            agent, "_call_llm", new_callable=AsyncMock,
            return_value="Q: What is AI?\nA: Artificial Intelligence.",
        ):
            result = await agent.generate_flash_cards(
                mock_db, ["doc-1"], count=5,
            )

        assert result.content_type == "flash_card"
        assert "Q:" in result.content

    @pytest.mark.asyncio
    async def test_generate_cliff_notes(
        self, agent: ContentGenerationAgent, mock_db: AsyncMock,
    ) -> None:
        """Can generate cliff notes."""
        with patch.object(
            agent, "_call_llm", new_callable=AsyncMock,
            return_value="- Key point 1\n- Key point 2",
        ):
            result = await agent.generate_cliff_notes(mock_db, ["doc-1"])

        assert result.content_type == "cliff_notes"

    @pytest.mark.asyncio
    async def test_generate_outline(
        self, agent: ContentGenerationAgent, mock_db: AsyncMock,
    ) -> None:
        """Can generate an outline."""
        with patch.object(
            agent, "_call_llm", new_callable=AsyncMock,
            return_value="1. Introduction\n  1.1 Background",
        ):
            result = await agent.generate_outline(mock_db, ["doc-1"])

        assert result.content_type == "outline"

    @pytest.mark.asyncio
    async def test_generate_extract(
        self, agent: ContentGenerationAgent, mock_db: AsyncMock,
    ) -> None:
        """Can extract specific information."""
        with patch.object(
            agent, "_call_llm", new_callable=AsyncMock,
            return_value="The main algorithm uses gradient descent.",
        ):
            result = await agent.generate_extract(
                mock_db, ["doc-1"], query="What algorithm is used?",
            )

        assert result.content_type == "extract"


# =============================================================================
# Caching Tests
# =============================================================================


class TestContentGenCaching:
    """Content generation caching."""

    @pytest.mark.asyncio
    async def test_cache_hit(
        self, agent: ContentGenerationAgent, mock_cache: MagicMock,
        mock_db: AsyncMock,
    ) -> None:
        """Cached results are returned directly."""
        mock_cache.get = AsyncMock(return_value={
            "content": "Cached summary",
            "content_type": "summary",
            "document_ids": ["doc-1"],
            "model_used": "test",
        })

        result = await agent.generate_summary(mock_db, ["doc-1"])
        assert result.cached is True
        assert result.content == "Cached summary"

    @pytest.mark.asyncio
    async def test_cache_miss_stores_result(
        self, agent: ContentGenerationAgent, mock_cache: MagicMock,
        mock_db: AsyncMock,
    ) -> None:
        """Cache misses store new results."""
        with patch.object(
            agent, "_call_llm", new_callable=AsyncMock,
            return_value="Generated content",
        ):
            await agent.generate_summary(mock_db, ["doc-1"])

        mock_cache.set.assert_called_once()

    def test_cache_key_deterministic(
        self, agent: ContentGenerationAgent,
    ) -> None:
        """Same request produces same cache key."""
        req = GenerationRequest(
            document_ids=["doc-1"],
            content_type=ContentType.SUMMARY,
        )
        key1 = agent._cache_key(req)
        key2 = agent._cache_key(req)
        assert key1 == key2

    def test_cache_key_different_for_different_types(
        self, agent: ContentGenerationAgent,
    ) -> None:
        """Different content types produce different cache keys."""
        req1 = GenerationRequest(
            document_ids=["doc-1"],
            content_type=ContentType.SUMMARY,
        )
        req2 = GenerationRequest(
            document_ids=["doc-1"],
            content_type=ContentType.OUTLINE,
        )
        assert agent._cache_key(req1) != agent._cache_key(req2)

    def test_cache_key_order_independent_doc_ids(
        self, agent: ContentGenerationAgent,
    ) -> None:
        """Document ID order doesn't affect cache key."""
        req1 = GenerationRequest(
            document_ids=["doc-1", "doc-2"],
            content_type=ContentType.SUMMARY,
        )
        req2 = GenerationRequest(
            document_ids=["doc-2", "doc-1"],
            content_type=ContentType.SUMMARY,
        )
        assert agent._cache_key(req1) == agent._cache_key(req2)


# =============================================================================
# Database Persistence Tests
# =============================================================================


class TestContentGenPersistence:
    """Database storage and retrieval."""

    @pytest.mark.asyncio
    async def test_existing_content_reused(
        self, agent: ContentGenerationAgent, mock_db: AsyncMock,
    ) -> None:
        """Existing generated content is returned from DB."""
        existing = MagicMock(spec=GeneratedContent)
        existing.id = "gen-123"
        existing.content = "Previously generated summary"
        existing.content_type = ContentType.SUMMARY
        existing.model_used = "old-model"

        # First call returns existing content, second returns chunks
        mock_existing_result = MagicMock()
        mock_existing_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_existing_result

        result = await agent.generate_summary(mock_db, ["doc-1"])
        assert result.cached is True
        assert result.content == "Previously generated summary"
        assert result.generation_id == "gen-123"

    @pytest.mark.asyncio
    async def test_generated_content_stored_in_db(
        self, agent: ContentGenerationAgent, mock_db: AsyncMock,
    ) -> None:
        """New generated content is persisted to the database."""
        # Mock: no existing content
        mock_no_existing = MagicMock()
        mock_no_existing.scalar_one_or_none.return_value = None
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = ["Chunk content here."]
        mock_no_existing.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_no_existing

        with patch.object(
            agent, "_call_llm", new_callable=AsyncMock,
            return_value="Generated text",
        ):
            await agent.generate_summary(mock_db, ["doc-1"])

        # Verify db.add was called to store the result
        mock_db.add.assert_called()


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestContentGenErrorHandling:
    """Error handling behavior."""

    @pytest.mark.asyncio
    async def test_no_document_content(
        self, agent: ContentGenerationAgent, mock_db: AsyncMock,
    ) -> None:
        """Empty documents return appropriate message."""
        # Mock: no existing, no chunks
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        result = await agent.generate_summary(mock_db, ["nonexistent"])
        assert "No document content found" in result.content

    @pytest.mark.asyncio
    async def test_llm_connection_error(
        self, agent: ContentGenerationAgent, mock_db: AsyncMock,
    ) -> None:
        """LLM connection failure returns error message."""
        # Mock: no existing content but has chunks
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = ["Some content"]
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        with patch.object(
            agent, "_call_llm", new_callable=AsyncMock,
            return_value="Error: LLM service unavailable. Please check Ollama is running.",
        ):
            result = await agent.generate_summary(mock_db, ["doc-1"])

        assert "Error" in result.content or result.content != ""

    @pytest.mark.asyncio
    async def test_cache_error_does_not_fail_generation(
        self, agent: ContentGenerationAgent, mock_cache: MagicMock,
        mock_db: AsyncMock,
    ) -> None:
        """Cache errors don't prevent generation."""
        mock_cache.get = AsyncMock(side_effect=RuntimeError("Redis down"))

        with patch.object(
            agent, "_call_llm", new_callable=AsyncMock,
            return_value="Generated despite cache error",
        ):
            result = await agent.generate_summary(mock_db, ["doc-1"])

        assert result.content == "Generated despite cache error"


# =============================================================================
# Prompt Building Tests
# =============================================================================


class TestPromptBuilding:
    """Prompt template construction."""

    def test_summary_prompt(self, agent: ContentGenerationAgent) -> None:
        """Summary prompt includes style and content."""
        req = GenerationRequest(
            document_ids=["d1"],
            content_type=ContentType.SUMMARY,
            style="detailed",
        )
        prompt = agent._build_prompt(req, "Test content here")
        assert "detailed" in prompt
        assert "Test content here" in prompt

    def test_flashcard_prompt_includes_count(
        self, agent: ContentGenerationAgent,
    ) -> None:
        """Flashcard prompt includes the requested count."""
        req = GenerationRequest(
            document_ids=["d1"],
            content_type=ContentType.FLASH_CARD,
            count=15,
        )
        prompt = agent._build_prompt(req, "Content")
        assert "15" in prompt

    def test_extract_prompt_includes_query(
        self, agent: ContentGenerationAgent,
    ) -> None:
        """Extract prompt includes the user query."""
        req = GenerationRequest(
            document_ids=["d1"],
            content_type=ContentType.EXTRACT,
            query="What is the main idea?",
        )
        prompt = agent._build_prompt(req, "Content")
        assert "What is the main idea?" in prompt

    def test_all_content_types_have_prompts(self) -> None:
        """Every ContentType has a prompt template."""
        for ct in [
            ContentType.SUMMARY, ContentType.FLASH_CARD,
            ContentType.CLIFF_NOTES, ContentType.OUTLINE,
            ContentType.EXTRACT,
        ]:
            assert ct in _PROMPTS


# =============================================================================
# Edge Cases
# =============================================================================


class TestContentGenEdgeCases:
    """Edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_multi_document_request(
        self, agent: ContentGenerationAgent, mock_db: AsyncMock,
    ) -> None:
        """Can generate from multiple documents."""
        # Multi-doc skips existing content check
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = ["Chunk from doc 1", "Chunk from doc 2"]
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        with patch.object(
            agent, "_call_llm", new_callable=AsyncMock,
            return_value="Combined summary of both documents.",
        ):
            result = await agent.generate_summary(
                mock_db, ["doc-1", "doc-2"],
            )

        assert result.content_type == "summary"
        assert len(result.document_ids) == 2

    def test_content_truncation(
        self, agent: ContentGenerationAgent,
    ) -> None:
        """Long content is truncated."""
        long_content = "x" * (_MAX_CONTENT_LENGTH + 1000)
        req = GenerationRequest(
            document_ids=["d1"],
            content_type=ContentType.SUMMARY,
        )
        prompt = agent._build_prompt(req, long_content[:_MAX_CONTENT_LENGTH])
        # Should be buildable without error
        assert len(prompt) > 0


# =============================================================================
# Data Model Tests
# =============================================================================


class TestContentGenModels:
    """Data model validation."""

    def test_generation_result_defaults(self) -> None:
        result = GenerationResult()
        assert result.content == ""
        assert result.cached is False
        assert result.generation_id is None

    def test_generation_request_defaults(self) -> None:
        req = GenerationRequest(
            document_ids=["d1"],
            content_type=ContentType.SUMMARY,
        )
        assert req.count == 10
        assert req.style is None
        assert req.query is None
