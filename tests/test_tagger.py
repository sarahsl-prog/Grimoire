"""Tests for the LLM auto-tagging system.

This module contains comprehensive tests for the Tagger class including:
- Happy path tests with mocked LLM
- Edge cases and boundary conditions
- Input validation
- Error handling
- Async behavior
- Hierarchical category handling
- Database integration
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from grimoire.config.settings import GrimoireSettings
from grimoire.core.tagger import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    MAX_SAMPLE_LENGTH,
    CategoryContext,
    Tagger,
    TaggingResult,
    TagSuggestion,
)
from grimoire.db.base import Base
from grimoire.db.models import Category, Document, DocumentTag, FileType, TaggedBy

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_settings() -> GrimoireSettings:
    """Create mock settings for testing."""
    settings = MagicMock(spec=GrimoireSettings)
    settings.llm = MagicMock()
    settings.llm.model = "llama3.2"
    settings.llm.url = "http://localhost:11434"
    settings.llm.temperature = 0.7
    settings.llm.max_tokens = 4096
    settings.llm.timeout = 30
    settings.processing = MagicMock()
    settings.processing.auto_tag_threshold = DEFAULT_CONFIDENCE_THRESHOLD
    return settings


@pytest.fixture
def tagger(mock_settings: GrimoireSettings) -> Tagger:
    """Create a Tagger instance for testing."""
    return Tagger(mock_settings)


@pytest.fixture
def mock_categories() -> list[Category]:
    """Create sample category hierarchy."""
    root_id = str(uuid4())
    child1_id = str(uuid4())
    child2_id = str(uuid4())

    root = Category(
        id=root_id,
        name="Research",
        slug="research",
        description="Research papers and notes",
        color="#3498db",
        created_at=datetime.utcnow(),
    )

    child1 = Category(
        id=child1_id,
        name="AI",
        slug="ai",
        description="Artificial Intelligence",
        parent_id=root_id,
        color="#2ecc71",
        created_at=datetime.utcnow(),
    )

    child2 = Category(
        id=child2_id,
        name="Machine Learning",
        slug="machine-learning",
        description="Machine Learning subfield",
        parent_id=child1_id,
        color="#e74c3c",
        created_at=datetime.utcnow(),
    )

    # Link relationships
    root.children = [child1]
    child1.parent = root
    child1.children = [child2]
    child2.parent = child1

    return [root, child1, child2]


@pytest.fixture
def flat_categories() -> list[Category]:
    """Create non-hierarchical categories."""
    return [
        Category(
            id=str(uuid4()),
            name="Technology",
            slug="technology",
            created_at=datetime.utcnow(),
        ),
        Category(
            id=str(uuid4()),
            name="Science",
            slug="science",
            created_at=datetime.utcnow(),
        ),
        Category(
            id=str(uuid4()),
            name="History",
            slug="history",
            created_at=datetime.utcnow(),
        ),
    ]


@pytest.fixture
def sample_document_text() -> str:
    """Sample document text for testing."""
    return """
    Machine learning is a subset of artificial intelligence that enables
    systems to learn and improve from experience without being explicitly
    programmed. It focuses on developing computer programs that can access
    data and use it to learn for themselves.
    
    Deep learning is a specialized form of machine learning inspired by the
    structure and function of the human brain. Neural networks are the
    foundation of deep learning algorithms.
    """


@pytest_asyncio.fixture
async def mock_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Create an async in-memory SQLite database session for testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        yield session

    await engine.dispose()


# =============================================================================
# Test Classes
# =============================================================================


class TestTagSuggestionModel:
    """Test the TagSuggestion Pydantic model."""

    def test_valid_suggestion(self) -> None:
        """Test creating a valid tag suggestion."""
        suggestion = TagSuggestion(
            category="Research/AI",
            confidence=0.85,
            reasoning="Document is about AI",
            category_id=str(uuid4()),
        )
        assert suggestion.category == "Research/AI"
        assert suggestion.confidence == 0.85
        assert suggestion.reasoning == "Document is about AI"

    def test_confidence_bounds(self) -> None:
        """Test confidence must be within 0-1 range."""
        with pytest.raises(ValueError):
            TagSuggestion(category="Test", confidence=1.5)

        with pytest.raises(ValueError):
            TagSuggestion(category="Test", confidence=-0.5)

    def test_empty_category_validation(self) -> None:
        """Test empty category name is rejected."""
        with pytest.raises(ValueError):
            TagSuggestion(category="", confidence=0.8)

        with pytest.raises(ValueError):
            TagSuggestion(category="   ", confidence=0.8)

    def test_hash_equality(self) -> None:
        """Test TagSuggestion hash and equality."""
        s1 = TagSuggestion(category="AI", confidence=0.8)
        s2 = TagSuggestion(category="AI", confidence=0.9)
        s3 = TagSuggestion(category="Science", confidence=0.8)

        assert s1 == s2  # Same category
        assert s1 != s3  # Different category
        assert hash(s1) == hash(s2)


class TestTaggingResultModel:
    """Test the TaggingResult Pydantic model."""

    def test_default_result(self) -> None:
        """Test result with default values."""
        result = TaggingResult()
        assert result.suggestions == []
        assert result.applied_tags == []
        assert result.threshold == DEFAULT_CONFIDENCE_THRESHOLD
        assert not result.cached

    def test_result_with_suggestions(self) -> None:
        """Test result with suggestions."""
        suggestions = [
            TagSuggestion(category="AI", confidence=0.9),
            TagSuggestion(category="ML", confidence=0.8),
        ]
        result = TaggingResult(
            document_id=str(uuid4()),
            suggestions=suggestions,
            applied_tags=[suggestions[0]],
            threshold=0.7,
        )
        assert len(result.suggestions) == 2
        assert len(result.applied_tags) == 1


class TestCategoryContext:
    """Test the CategoryContext dataclass."""

    def test_category_context_creation(self, mock_categories: list[Category]) -> None:
        """Test creating category contexts."""
        ctx = CategoryContext(
            path="Research/AI",
            category=mock_categories[1],
            description="AI description",
        )
        assert ctx.path == "Research/AI"
        assert ctx.display_path == "Research/AI"
        assert "Research/AI" in ctx.to_prompt_line()
        assert "AI description" in ctx.to_prompt_line()

    def test_category_context_no_description(
        self, mock_categories: list[Category]
    ) -> None:
        """Test prompt line without description."""
        ctx = CategoryContext(path="Research", category=mock_categories[0])
        line = ctx.to_prompt_line()
        assert "Research" in line
        # Format is "  - Research" when no description
        assert line.strip() == "- Research"


class TestTaggerInitialization:
    """Test Tagger class initialization."""

    def test_tagger_creation(self, mock_settings: GrimoireSettings) -> None:
        """Test tagger can be created with settings."""
        tagger = Tagger(mock_settings)
        assert tagger.settings == mock_settings
        assert tagger.llm_config == mock_settings.llm
        assert tagger._client is None

    def test_threshold_from_config(self, mock_settings: GrimoireSettings) -> None:
        """Test threshold is read from config."""
        mock_settings.processing.auto_tag_threshold = 0.8
        tagger = Tagger(mock_settings)
        assert tagger.processing_config.auto_tag_threshold == 0.8


class TestTaggerSamplePreparation:
    """Test document sample preparation."""

    def test_prepare_sample_normal(self, tagger: Tagger) -> None:
        """Test normal sample cleaning."""
        sample = "  This   has\n\textra   whitespace  "
        result = tagger._prepare_sample(sample)
        assert result == "This has extra whitespace"

    def test_prepare_sample_truncate(self, tagger: Tagger) -> None:
        """Test long sample truncation."""
        sample = "x" * (MAX_SAMPLE_LENGTH + 1000)
        result = tagger._prepare_sample(sample)
        assert len(result) == MAX_SAMPLE_LENGTH + 3  # +3 for "..."
        assert result.endswith("...")

    def test_prepare_sample_empty(self, tagger: Tagger) -> None:
        """Test empty sample handling."""
        assert tagger._prepare_sample("") == ""
        assert tagger._prepare_sample("   ") == ""
        assert tagger._prepare_sample(None) == ""  # type: ignore[arg-type]


class TestTaggerCategoryFormatting:
    """Test category formatting for LLM prompt."""

    def test_format_hierarchical_categories(
        self, tagger: Tagger, mock_categories: list[Category]
    ) -> None:
        """Test hierarchical path building."""
        contexts = tagger._format_categories(mock_categories)
        assert len(contexts) == 3

        paths = [ctx.path for ctx in contexts]
        assert "Research" in paths
        assert "Research/AI" in paths
        assert "Research/AI/Machine Learning" in paths

    def test_format_flat_categories(
        self, tagger: Tagger, flat_categories: list[Category]
    ) -> None:
        """Test flat category formatting."""
        contexts = tagger._format_categories(flat_categories)
        assert len(contexts) == 3

        for ctx in contexts:
            assert "/" not in ctx.path  # No hierarchy

    def test_format_sorted(
        self, tagger: Tagger, flat_categories: list[Category]
    ) -> None:
        """Test categories are sorted by path."""
        contexts = tagger._format_categories(flat_categories)
        paths = [ctx.path for ctx in contexts]
        assert paths == sorted(paths)


class TestTaggerResponseParsing:
    """Test LLM response parsing."""

    def test_parse_valid_json_response(self, tagger: Tagger) -> None:
        """Test parsing valid JSON response."""
        response = json.dumps(
            {
                "suggestions": [
                    {"category": "AI", "confidence": 0.95, "reasoning": "About AI"},
                    {"category": "ML", "confidence": 0.8},
                ]
            }
        )
        suggestions = tagger._parse_llm_response(response)
        assert len(suggestions) == 2
        assert suggestions[0].category == "AI"
        assert suggestions[0].confidence == 0.95

    def test_parse_markdown_code_block(self, tagger: Tagger) -> None:
        """Test parsing response in markdown code block."""
        response = """```json
        {"suggestions": [{"category": "AI", "confidence": 0.9}]}
        ```"""
        suggestions = tagger._parse_llm_response(response)
        assert len(suggestions) == 1
        assert suggestions[0].category == "AI"

    def test_parse_alternative_keys(self, tagger: Tagger) -> None:
        """Test parsing with alternative key names."""
        response = json.dumps(
            {
                "categories": [
                    {"name": "AI", "score": 0.9, "reason": "Is AI"},
                ]
            }
        )
        suggestions = tagger._parse_llm_response(response)
        assert len(suggestions) == 1
        assert suggestions[0].category == "AI"

    def test_parse_list_response(self, tagger: Tagger) -> None:
        """Test parsing list response."""
        response = json.dumps(
            [
                {"category": "AI", "confidence": 0.9},
                {"category": "ML", "confidence": 0.8},
            ]
        )
        suggestions = tagger._parse_llm_response(response)
        assert len(suggestions) == 2

    def test_parse_empty_response(self, tagger: Tagger) -> None:
        """Test parsing empty response."""
        assert tagger._parse_llm_response("") == []
        assert tagger._parse_llm_response("   ") == []

    def test_parse_invalid_json(self, tagger: Tagger) -> None:
        """Test handling invalid JSON."""
        suggestions = tagger._parse_llm_response("not valid json")
        assert suggestions == []

    def test_confidence_clamping(self, tagger: Tagger) -> None:
        """Test confidence values are clamped to 0-1."""
        response = json.dumps({"suggestions": [{"category": "AI", "confidence": 1.5}]})
        suggestions = tagger._parse_llm_response(response)
        assert suggestions[0].confidence == 1.0

        response = json.dumps({"suggestions": [{"category": "AI", "confidence": -0.5}]})
        suggestions = tagger._parse_llm_response(response)
        assert suggestions[0].confidence == 0.0


class TestTaggerCategoryMatching:
    """Test suggestion to category matching."""

    def test_exact_path_match(
        self, tagger: Tagger, mock_categories: list[Category]
    ) -> None:
        """Test matching by exact path."""
        contexts = tagger._format_categories(mock_categories)
        suggestion = TagSuggestion(category="Research/AI", confidence=0.9)

        matched = tagger._match_suggestions_to_categories([suggestion], contexts)
        assert len(matched) == 1
        assert matched[0].category_id == mock_categories[1].id

    def test_name_fallback_match(
        self, tagger: Tagger, flat_categories: list[Category]
    ) -> None:
        """Test matching by name when path fails."""
        contexts = tagger._format_categories(flat_categories)
        suggestion = TagSuggestion(category="Technology", confidence=0.9)

        matched = tagger._match_suggestions_to_categories([suggestion], contexts)
        assert len(matched) == 1
        assert matched[0].category_id == flat_categories[0].id

    def test_partial_path_match(
        self, tagger: Tagger, mock_categories: list[Category]
    ) -> None:
        """Test matching when LLM returns partial path."""
        contexts = tagger._format_categories(mock_categories)
        suggestion = TagSuggestion(category="AI/Machine Learning", confidence=0.9)

        matched = tagger._match_suggestions_to_categories([suggestion], contexts)
        # Should match "Machine Learning" by name
        assert len(matched) == 1

    def test_no_match(self, tagger: Tagger, flat_categories: list[Category]) -> None:
        """Test when no match is found."""
        contexts = tagger._format_categories(flat_categories)
        suggestion = TagSuggestion(category="NonExistent", confidence=0.9)

        matched = tagger._match_suggestions_to_categories([suggestion], contexts)
        assert len(matched) == 0


@pytest.mark.asyncio
class TestTaggerSuggestTags:
    """Test the main suggest_tags method with mocked LLM."""

    async def test_suggest_tags_success(
        self,
        tagger: Tagger,
        flat_categories: list[Category],
        sample_document_text: str,
    ) -> None:
        """Test successful tag suggestion."""
        mock_response = {
            "suggestions": [
                {"category": "Technology", "confidence": 0.95},
                {"category": "Science", "confidence": 0.6},
            ]
        }

        with patch.object(
            tagger, "_call_ollama", return_value=json.dumps(mock_response)
        ):
            result = await tagger.suggest_tags(
                document_sample=sample_document_text,
                categories=flat_categories,
                document_id=str(uuid4()),
            )

        assert len(result.suggestions) == 2
        assert len(result.applied_tags) == 1  # Only Technology passes threshold
        assert result.applied_tags[0].category == "Technology"

    async def test_suggest_tags_custom_threshold(
        self,
        tagger: Tagger,
        flat_categories: list[Category],
        sample_document_text: str,
    ) -> None:
        """Test custom confidence threshold."""
        mock_response = {
            "suggestions": [
                {"category": "Technology", "confidence": 0.6},
                {"category": "Science", "confidence": 0.4},
            ]
        }

        with patch.object(
            tagger, "_call_ollama", return_value=json.dumps(mock_response)
        ):
            # With threshold 0.5, only Technology passes
            result = await tagger.suggest_tags(
                document_sample=sample_document_text,
                categories=flat_categories,
                threshold=0.5,
            )

        assert len(result.applied_tags) == 1
        assert result.applied_tags[0].category == "Technology"

    async def test_suggest_tags_no_categories(
        self,
        tagger: Tagger,
        sample_document_text: str,
    ) -> None:
        """Test with empty category list."""
        result = await tagger.suggest_tags(
            document_sample=sample_document_text,
            categories=[],
        )
        assert result.suggestions == []
        assert result.applied_tags == []

    async def test_suggest_tags_empty_sample(
        self,
        tagger: Tagger,
        flat_categories: list[Category],
    ) -> None:
        """Test empty sample raises ValueError."""
        with pytest.raises(ValueError, match="cannot be empty"):
            await tagger.suggest_tags(
                document_sample="",
                categories=flat_categories,
            )

    async def test_suggest_tags_whitespace_sample(
        self,
        tagger: Tagger,
        flat_categories: list[Category],
    ) -> None:
        """Test whitespace-only sample raises ValueError."""
        with pytest.raises(ValueError, match="cannot be empty"):
            await tagger.suggest_tags(
                document_sample="   \n\t   ",
                categories=flat_categories,
            )

    async def test_suggest_tags_http_error(
        self,
        tagger: Tagger,
        flat_categories: list[Category],
        sample_document_text: str,
    ) -> None:
        """Test HTTP error handling."""
        with patch.object(
            tagger,
            "_call_ollama",
            side_effect=httpx.HTTPStatusError(
                "500 Server Error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            ),
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await tagger.suggest_tags(
                    document_sample=sample_document_text,
                    categories=flat_categories,
                )

    async def test_suggest_tags_connection_error(
        self,
        tagger: Tagger,
        flat_categories: list[Category],
        sample_document_text: str,
    ) -> None:
        """Test connection error handling."""
        with patch.object(
            tagger, "_call_ollama", side_effect=httpx.RequestError("Connection refused")
        ):
            with pytest.raises(httpx.RequestError):
                await tagger.suggest_tags(
                    document_sample=sample_document_text,
                    categories=flat_categories,
                )


@pytest.mark.asyncio
class TestTaggerApplyTags:
    """Test the apply_tags database method."""

    async def test_apply_tags_creates_new(
        self,
        tagger: Tagger,
        mock_db_session: AsyncSession,
        flat_categories: list[Category],
    ) -> None:
        """Test creating new document tags."""
        # Create document
        doc = Document(
            id=str(uuid4()),
            source_path="/test/doc.pdf",
            storage_backend="local",
            file_type=FileType.PDF,
            file_hash="abc123",
            title="Test Document",
            size_bytes=1000,
        )
        mock_db_session.add(doc)
        await mock_db_session.flush()

        # Add category to DB
        for cat in flat_categories:
            mock_db_session.add(cat)
        await mock_db_session.flush()

        suggestions = [
            TagSuggestion(
                category="Technology",
                confidence=0.9,
                category_id=flat_categories[0].id,
            ),
        ]

        created = await tagger.apply_tags(mock_db_session, doc, suggestions)

        assert len(created) == 1
        assert created[0].document_id == doc.id
        assert created[0].category_id == flat_categories[0].id
        assert created[0].confidence == 0.9
        assert created[0].tagged_by == TaggedBy.LLM

    async def test_apply_tags_skips_missing_category_id(
        self,
        tagger: Tagger,
        mock_db_session: AsyncSession,
    ) -> None:
        """Test skipping suggestions without category_id."""
        doc = Document(
            id=str(uuid4()),
            source_path="/test/doc.pdf",
            storage_backend="local",
            file_type=FileType.PDF,
            file_hash="abc123",
            title="Test Document",
            size_bytes=1000,
        )
        mock_db_session.add(doc)
        await mock_db_session.flush()

        suggestions = [
            TagSuggestion(category="Unknown", confidence=0.9, category_id=None),
        ]

        created = await tagger.apply_tags(mock_db_session, doc, suggestions)
        assert len(created) == 0

    async def test_apply_tags_updates_existing(
        self,
        tagger: Tagger,
        mock_db_session: AsyncSession,
        flat_categories: list[Category],
    ) -> None:
        """Test updating existing tag with higher confidence."""
        doc = Document(
            id=str(uuid4()),
            source_path="/test/doc.pdf",
            storage_backend="local",
            file_type=FileType.PDF,
            file_hash="abc123",
            title="Test Document",
            size_bytes=1000,
        )
        mock_db_session.add(doc)

        cat = flat_categories[0]
        mock_db_session.add(cat)
        await mock_db_session.flush()

        # Create existing tag
        existing = DocumentTag(
            document_id=doc.id,
            category_id=cat.id,
            confidence=0.5,
            tagged_by=TaggedBy.USER,
        )
        mock_db_session.add(existing)
        await mock_db_session.flush()

        # Update with higher confidence
        suggestions = [
            TagSuggestion(category="Technology", confidence=0.9, category_id=cat.id),
        ]

        created = await tagger.apply_tags(mock_db_session, doc, suggestions)

        assert len(created) == 0  # No new tag created
        assert existing.confidence == 0.9
        assert existing.tagged_by == TaggedBy.LLM


@pytest.mark.asyncio
class TestTaggerEndToEnd:
    """End-to-end integration tests."""

    async def test_full_tagging_pipeline(
        self,
        mock_settings: GrimoireSettings,
        flat_categories: list[Category],
        sample_document_text: str,
    ) -> None:
        """Test full tagging pipeline with mocked LLM."""
        tagger = Tagger(mock_settings)

        mock_response = {
            "suggestions": [
                {
                    "category": "Technology",
                    "confidence": 0.95,
                    "reasoning": "Document is about technology",
                },
                {
                    "category": "Science",
                    "confidence": 0.85,
                    "reasoning": "Scientific content",
                },
            ]
        }

        with patch.object(
            tagger, "_call_ollama", return_value=json.dumps(mock_response)
        ):
            result = await tagger.suggest_tags(
                document_sample=sample_document_text,
                categories=flat_categories,
            )

        assert len(result.suggestions) == 2
        assert len(result.applied_tags) == 2  # Both pass threshold

        # Verify suggestions have category IDs
        for tag in result.suggestions:
            assert tag.category_id is not None

    async def test_hierarchical_categories(
        self,
        mock_settings: GrimoireSettings,
        mock_categories: list[Category],
        sample_document_text: str,
    ) -> None:
        """Test tagging with hierarchical categories."""
        tagger = Tagger(mock_settings)

        # LLM returns a hierarchical path
        mock_response = {
            "suggestions": [
                {"category": "Research/AI", "confidence": 0.9},
            ]
        }

        with patch.object(
            tagger, "_call_ollama", return_value=json.dumps(mock_response)
        ):
            result = await tagger.suggest_tags(
                document_sample=sample_document_text,
                categories=mock_categories,
            )

        assert len(result.suggestions) == 1
        # Should match by parent path
        assert result.suggestions[0].category_id == mock_categories[1].id


class TestTaggerEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_very_long_category_list(
        self,
        tagger: Tagger,
        sample_document_text: str,
    ) -> None:
        """Test handling many categories."""
        # Create 100 categories
        categories = [
            Category(
                id=str(uuid4()),
                name=f"Category{i}",
                slug=f"category{i}",
                created_at=datetime.utcnow(),
            )
            for i in range(100)
        ]

        mock_response = {"suggestions": [{"category": "Category50", "confidence": 0.9}]}

        with patch.object(
            tagger, "_call_ollama", return_value=json.dumps(mock_response)
        ):
            result = await tagger.suggest_tags(
                document_sample=sample_document_text,
                categories=categories,
            )

        assert len(result.suggestions) == 1

    def test_unicode_category_names(self, tagger: Tagger) -> None:
        """Test handling unicode category names."""
        categories = [
            Category(
                id=str(uuid4()),
                name="研究",  # Japanese characters
                slug="research",
                created_at=datetime.utcnow(),
            ),
            Category(
                id=str(uuid4()),
                name="AI & Machine Learning",
                slug="ai-ml",
                created_at=datetime.utcnow(),
            ),
        ]

        contexts = tagger._format_categories(categories)
        assert len(contexts) == 2
        # Find the Japanese category (may not be first due to sorting)
        japanese_ctx = next(
            (ctx for ctx in contexts if ctx.category.name == "研究"), None
        )
        assert japanese_ctx is not None
        assert "研究" in japanese_ctx.path

    def test_special_characters_in_path(self, tagger: Tagger) -> None:
        """Test handling special characters in category paths."""
        cat1 = Category(
            id=str(uuid4()),
            name="C++",
            slug="cpp",
            created_at=datetime.utcnow(),
        )
        cat2 = Category(
            id=str(uuid4()),
            name="C#",
            slug="csharp",
            created_at=datetime.utcnow(),
        )

        contexts = tagger._format_categories([cat1, cat2])
        assert len(contexts) == 2


class TestConcurrencyAndAsync:
    """Test async and concurrent behavior."""

    @pytest.mark.asyncio
    async def test_concurrent_suggestions(
        self,
        mock_settings: GrimoireSettings,
        flat_categories: list[Category],
    ) -> None:
        """Test concurrent tag suggestions."""
        import asyncio

        tagger1 = Tagger(mock_settings)
        tagger2 = Tagger(mock_settings)

        mock_response = {"suggestions": [{"category": "Technology", "confidence": 0.9}]}

        with patch.object(
            tagger1, "_call_ollama", return_value=json.dumps(mock_response)
        ):
            with patch.object(
                tagger2, "_call_ollama", return_value=json.dumps(mock_response)
            ):
                results = await asyncio.gather(
                    tagger1.suggest_tags("Sample text 1", flat_categories),
                    tagger2.suggest_tags("Sample text 2", flat_categories),
                )

        assert len(results) == 2
        assert all(len(r.suggestions) == 1 for r in results)


class TestPromptGeneration:
    """Test prompt generation."""

    def test_prompt_includes_threshold(
        self, tagger: Tagger, flat_categories: list[Category]
    ) -> None:
        """Test threshold is mentioned in prompt."""
        contexts = tagger._format_categories(flat_categories)
        sample = "Test document"
        threshold = 0.85

        prompt = tagger._build_prompt(sample, contexts, threshold)

        assert "0.85" in prompt
        assert "Technology" in prompt
        assert "Science" in prompt

    def test_prompt_includes_categories(
        self, tagger: Tagger, mock_categories: list[Category]
    ) -> None:
        """Test all categories appear in prompt."""
        contexts = tagger._format_categories(mock_categories)
        sample = "Test document"

        prompt = tagger._build_prompt(sample, contexts)

        assert "Research" in prompt
        assert "AI" in prompt
        assert "Machine Learning" in prompt


# =============================================================================
# Performance Tests
# =============================================================================


@pytest.mark.slow
class TestPerformance:
    """Performance and stress tests."""

    @pytest.mark.asyncio
    async def test_large_document_sample(
        self, tagger: Tagger, flat_categories: list[Category]
    ) -> None:
        """Test handling very large document samples."""
        large_sample = "word " * 10000  # Very large text

        mock_response = {"suggestions": [{"category": "Technology", "confidence": 0.9}]}

        with patch.object(
            tagger, "_call_ollama", return_value=json.dumps(mock_response)
        ):
            result = await tagger.suggest_tags(
                document_sample=large_sample,
                categories=flat_categories,
            )

        assert len(result.suggestions) == 1
