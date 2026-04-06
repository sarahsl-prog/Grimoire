"""Tests for the Coordinator Agent.

Tests cover:
- Intent classification: all IntentType values, keyword matching, question starters
- Content-type extraction: all ContentType values
- Happy path routing: each intent dispatched to the correct agent
- CoordinatorContext overrides: forced intent, pre-parsed params
- Convenience methods: ingest(), query(), generate()
- Missing/unconfigured agents: descriptive errors, no exceptions
- Empty / whitespace-only input
- LLM-assisted fallback classification (mocked)
- Path extraction from free-form text (Unix, Windows, cloud schemes)
- UNKNOWN intent falls back to QUERY
- Per-intent parameter inference and validation
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grimoire.agents.content_gen import GenerationResult
from grimoire.agents.coordinator import (
    CoordinatorAgent,
    CoordinatorContext,
    CoordinatorResult,
    IntentType,
    classify_intent,
    extract_content_type,
)
from grimoire.agents.ingestion import BatchIngestionResult, IngestionResult
from grimoire.agents.query import QueryResult, SearchOnlyResult
from grimoire.db.models import ContentType


# =============================================================================
# Helpers
# =============================================================================


def _make_ingestion_result(status: str = "completed") -> IngestionResult:
    return IngestionResult(
        file_path="/tmp/doc.pdf",
        document_id="doc-abc",
        status=status,
        chunks_created=3,
        vectors_stored=3,
    )


def _make_batch_result() -> BatchIngestionResult:
    return BatchIngestionResult(
        total=2,
        succeeded=2,
        skipped=0,
        failed=0,
        results=[_make_ingestion_result(), _make_ingestion_result()],
    )


def _make_query_result(answer: str = "The answer is 42.") -> QueryResult:
    return QueryResult(
        query="test question",
        answer=answer,
        model_used="test-model",
        search_results_count=3,
    )


def _make_search_result() -> SearchOnlyResult:
    return SearchOnlyResult(
        query="search term",
        results=[{"chunk_id": "c1", "content": "result text", "score": 0.9}],
        total_results=1,
    )


def _make_generation_result(content_type: str = "summary") -> GenerationResult:
    return GenerationResult(
        content="Generated content here.",
        content_type=content_type,
        document_ids=["doc-1"],
        model_used="test-model",
    )


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_db() -> AsyncMock:
    """Mock database session."""
    return AsyncMock()


@pytest.fixture
def mock_ingestion_agent() -> MagicMock:
    """Mock IngestionAgent."""
    agent = MagicMock()
    agent.ingest_file = AsyncMock(return_value=_make_ingestion_result())
    agent.ingest_directory = AsyncMock(return_value=_make_batch_result())
    return agent


@pytest.fixture
def mock_query_agent() -> MagicMock:
    """Mock QueryAgent."""
    agent = MagicMock()
    agent.query = AsyncMock(return_value=_make_query_result())
    agent.search = AsyncMock(return_value=_make_search_result())
    return agent


@pytest.fixture
def mock_content_gen_agent() -> MagicMock:
    """Mock ContentGenerationAgent."""
    agent = MagicMock()
    agent.generate_summary = AsyncMock(return_value=_make_generation_result("summary"))
    agent.generate_flash_cards = AsyncMock(return_value=_make_generation_result("flash_card"))
    agent.generate_cliff_notes = AsyncMock(return_value=_make_generation_result("cliff_notes"))
    agent.generate_outline = AsyncMock(return_value=_make_generation_result("outline"))
    agent.generate_extract = AsyncMock(return_value=_make_generation_result("extract"))
    return agent


@pytest.fixture
def mock_watcher_agent() -> MagicMock:
    """Mock WatcherAgent."""
    agent = MagicMock()
    agent.watch = AsyncMock(return_value="watch-id-123")
    agent.unwatch = AsyncMock(return_value=True)
    return agent


@pytest.fixture
def coordinator(
    mock_ingestion_agent: MagicMock,
    mock_query_agent: MagicMock,
    mock_content_gen_agent: MagicMock,
    mock_watcher_agent: MagicMock,
) -> CoordinatorAgent:
    """Full coordinator with all agents mocked."""
    return CoordinatorAgent(
        ingestion_agent=mock_ingestion_agent,
        query_agent=mock_query_agent,
        content_gen_agent=mock_content_gen_agent,
        watcher_agent=mock_watcher_agent,
        llm_url="http://localhost:11434",
        llm_model="test-model",
    )


# =============================================================================
# Intent Classification Tests
# =============================================================================


class TestClassifyIntent:
    """classify_intent() keyword matching and heuristics."""

    def test_ingest_keyword(self) -> None:
        intent, conf = classify_intent("ingest /home/docs")
        assert intent == IntentType.INGEST
        assert conf >= 0.8

    def test_scan_keyword(self) -> None:
        intent, _ = classify_intent("scan my documents folder")
        assert intent == IntentType.INGEST

    def test_import_keyword(self) -> None:
        intent, _ = classify_intent("import files from /data")
        assert intent == IntentType.INGEST

    def test_index_keyword(self) -> None:
        intent, _ = classify_intent("index the new research papers")
        assert intent == IntentType.INGEST

    def test_watch_keyword(self) -> None:
        intent, conf = classify_intent("watch /tmp/documents")
        assert intent == IntentType.WATCH
        assert conf >= 0.8

    def test_monitor_keyword(self) -> None:
        intent, _ = classify_intent("monitor the uploads directory")
        assert intent == IntentType.WATCH

    def test_unwatch_keyword(self) -> None:
        intent, conf = classify_intent("unwatch /tmp/documents")
        assert intent == IntentType.UNWATCH
        assert conf >= 0.8

    def test_stop_watching_phrase(self) -> None:
        intent, _ = classify_intent("stop watching /data")
        assert intent == IntentType.UNWATCH

    def test_generate_keyword(self) -> None:
        intent, conf = classify_intent("generate a summary for doc 42")
        assert intent == IntentType.GENERATE
        assert conf >= 0.8

    def test_summarize_keyword(self) -> None:
        intent, _ = classify_intent("summarize document abc123")
        assert intent == IntentType.GENERATE

    def test_flashcard_keyword(self) -> None:
        intent, _ = classify_intent("create flashcard from biology notes")
        assert intent == IntentType.GENERATE

    def test_outline_keyword(self) -> None:
        intent, _ = classify_intent("outline the chapter on neural nets")
        assert intent == IntentType.GENERATE

    def test_search_for_phrase(self) -> None:
        intent, conf = classify_intent("search for machine learning papers")
        assert intent == IntentType.SEARCH
        assert conf >= 0.8

    def test_find_documents_phrase(self) -> None:
        intent, _ = classify_intent("find documents about transformers")
        assert intent == IntentType.SEARCH

    def test_question_what(self) -> None:
        intent, conf = classify_intent("what is the main argument in the paper?")
        assert intent == IntentType.QUERY
        assert conf >= 0.6

    def test_question_how(self) -> None:
        intent, _ = classify_intent("how does attention mechanism work?")
        assert intent == IntentType.QUERY

    def test_question_why(self) -> None:
        intent, _ = classify_intent("why did the model fail to converge?")
        assert intent == IntentType.QUERY

    def test_question_mark(self) -> None:
        intent, conf = classify_intent("transformer architecture details?")
        assert intent == IntentType.QUERY
        assert conf >= 0.5

    def test_explain_starter(self) -> None:
        intent, _ = classify_intent("explain the difference between BERT and GPT")
        assert intent == IntentType.QUERY

    def test_unknown_returns_low_confidence(self) -> None:
        intent, conf = classify_intent("xyzzy frobnicator")
        assert intent == IntentType.UNKNOWN
        assert conf < 0.5

    def test_empty_string(self) -> None:
        intent, conf = classify_intent("")
        assert intent == IntentType.UNKNOWN
        assert conf < 0.5

    def test_unwatch_takes_priority_over_watch(self) -> None:
        """UNWATCH keywords are checked before WATCH, so 'stop watching' wins."""
        intent, _ = classify_intent("stop watching /tmp")
        assert intent == IntentType.UNWATCH

    def test_case_insensitive(self) -> None:
        intent, _ = classify_intent("INGEST /docs/paper.pdf")
        assert intent == IntentType.INGEST

    def test_returns_tuple(self) -> None:
        result = classify_intent("what is life?")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], IntentType)
        assert isinstance(result[1], float)


# =============================================================================
# Content Type Extraction Tests
# =============================================================================


class TestExtractContentType:
    """extract_content_type() keyword matching."""

    def test_summary(self) -> None:
        assert extract_content_type("generate a summary") == ContentType.SUMMARY

    def test_summarize(self) -> None:
        assert extract_content_type("summarize this paper") == ContentType.SUMMARY

    def test_overview(self) -> None:
        assert extract_content_type("give me an overview") == ContentType.SUMMARY

    def test_flashcard(self) -> None:
        assert extract_content_type("create flashcards from this") == ContentType.FLASH_CARD

    def test_flash_card_space(self) -> None:
        assert extract_content_type("make flash card quiz") == ContentType.FLASH_CARD

    def test_cliff_notes(self) -> None:
        assert extract_content_type("cliff notes for chapter 3") == ContentType.CLIFF_NOTES

    def test_key_points(self) -> None:
        assert extract_content_type("list the key points") == ContentType.CLIFF_NOTES

    def test_outline(self) -> None:
        assert extract_content_type("write an outline") == ContentType.OUTLINE

    def test_table_of_contents(self) -> None:
        assert extract_content_type("create a table of contents") == ContentType.OUTLINE

    def test_extract(self) -> None:
        assert extract_content_type("extract the methodology section") == ContentType.EXTRACT

    def test_unknown_defaults_to_summary(self) -> None:
        assert extract_content_type("do something with doc 42") == ContentType.SUMMARY

    def test_empty_string_defaults_to_summary(self) -> None:
        assert extract_content_type("") == ContentType.SUMMARY


# =============================================================================
# Happy Path Tests
# =============================================================================


class TestCoordinatorHappyPath:
    """Core routing to each agent works correctly."""

    @pytest.mark.asyncio
    async def test_query_intent_routes_to_query_agent(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_query_agent: MagicMock,
    ) -> None:
        result = await coordinator.execute(mock_db, "what is machine learning?")

        assert result.intent == IntentType.QUERY
        assert result.agent_used == "QueryAgent"
        assert result.error is None
        mock_query_agent.query.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ingest_intent_routes_to_ingestion_agent_for_file(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_ingestion_agent: MagicMock,
        tmp_path: Any,
    ) -> None:
        sample = tmp_path / "doc.pdf"
        sample.touch()
        ctx = CoordinatorContext(intent=IntentType.INGEST, file_path=str(sample))

        result = await coordinator.execute(mock_db, "ingest doc", context=ctx)

        assert result.intent == IntentType.INGEST
        assert result.agent_used == "IngestionAgent"
        assert result.error is None
        mock_ingestion_agent.ingest_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ingest_intent_routes_to_ingestion_agent_for_directory(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_ingestion_agent: MagicMock,
        tmp_path: Any,
    ) -> None:
        ctx = CoordinatorContext(intent=IntentType.INGEST, file_path=str(tmp_path))

        result = await coordinator.execute(mock_db, "ingest dir", context=ctx)

        assert result.intent == IntentType.INGEST
        assert result.agent_used == "IngestionAgent"
        mock_ingestion_agent.ingest_directory.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_intent_routes_to_query_agent_search(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_query_agent: MagicMock,
    ) -> None:
        result = await coordinator.execute(
            mock_db, "search for papers about BERT"
        )

        assert result.intent == IntentType.SEARCH
        assert "QueryAgent" in result.agent_used
        assert result.error is None
        mock_query_agent.search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_generate_intent_routes_to_content_gen(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_content_gen_agent: MagicMock,
    ) -> None:
        ctx = CoordinatorContext(
            intent=IntentType.GENERATE,
            document_ids=["doc-1"],
            content_type=ContentType.SUMMARY,
        )
        result = await coordinator.execute(mock_db, "summarize doc", context=ctx)

        assert result.intent == IntentType.GENERATE
        assert result.agent_used == "ContentGenerationAgent"
        assert result.error is None
        mock_content_gen_agent.generate_summary.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_watch_intent_routes_to_watcher(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_watcher_agent: MagicMock,
        tmp_path: Any,
    ) -> None:
        ctx = CoordinatorContext(
            intent=IntentType.WATCH,
            file_path=str(tmp_path),
        )
        result = await coordinator.execute(mock_db, "watch dir", context=ctx)

        assert result.intent == IntentType.WATCH
        assert result.agent_used == "WatcherAgent"
        assert result.error is None
        mock_watcher_agent.watch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unwatch_intent_routes_to_watcher(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_watcher_agent: MagicMock,
    ) -> None:
        ctx = CoordinatorContext(
            intent=IntentType.UNWATCH,
            watch_id="watch-id-123",
        )
        result = await coordinator.execute(mock_db, "unwatch", context=ctx)

        assert result.intent == IntentType.UNWATCH
        assert result.agent_used == "WatcherAgent"
        assert result.error is None
        mock_watcher_agent.unwatch.assert_awaited_once_with("watch-id-123")

    @pytest.mark.asyncio
    async def test_result_contains_input_text(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
    ) -> None:
        text = "what is entropy?"
        result = await coordinator.execute(mock_db, text)
        assert result.input_text == text

    @pytest.mark.asyncio
    async def test_result_contains_duration(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
    ) -> None:
        result = await coordinator.execute(mock_db, "what is entropy?")
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_result_contains_confidence(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
    ) -> None:
        result = await coordinator.execute(mock_db, "what is life?")
        assert 0.0 <= result.confidence <= 1.0


# =============================================================================
# Context Override Tests
# =============================================================================


class TestCoordinatorContextOverrides:
    """CoordinatorContext pre-parsed params are respected."""

    @pytest.mark.asyncio
    async def test_forced_intent_bypasses_classification(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_ingestion_agent: MagicMock,
        tmp_path: Any,
    ) -> None:
        sample = tmp_path / "file.md"
        sample.touch()
        # Force INGEST even though text looks like a question
        ctx = CoordinatorContext(
            intent=IntentType.INGEST,
            file_path=str(sample),
        )
        result = await coordinator.execute(
            mock_db, "what is the meaning of life?", context=ctx
        )
        assert result.intent == IntentType.INGEST
        mock_ingestion_agent.ingest_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_context_confidence_is_1_when_intent_forced(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        tmp_path: Any,
    ) -> None:
        sample = tmp_path / "file.txt"
        sample.touch()
        ctx = CoordinatorContext(
            intent=IntentType.INGEST,
            file_path=str(sample),
        )
        result = await coordinator.execute(mock_db, "anything", context=ctx)
        assert result.confidence == 1.0

    @pytest.mark.asyncio
    async def test_context_top_k_passed_to_query(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_query_agent: MagicMock,
    ) -> None:
        ctx = CoordinatorContext(intent=IntentType.QUERY, top_k=15)
        await coordinator.execute(mock_db, "explain RAG", context=ctx)
        _, kwargs = mock_query_agent.query.call_args
        assert kwargs["top_k"] == 15

    @pytest.mark.asyncio
    async def test_context_filter_dict_passed_to_query(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_query_agent: MagicMock,
    ) -> None:
        filters = {"tag": "research"}
        ctx = CoordinatorContext(
            intent=IntentType.QUERY,
            filter_dict=filters,
        )
        await coordinator.execute(mock_db, "summarize findings", context=ctx)
        _, kwargs = mock_query_agent.query.call_args
        assert kwargs["filter_dict"] == filters

    @pytest.mark.asyncio
    async def test_context_content_type_overrides_inference(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_content_gen_agent: MagicMock,
    ) -> None:
        ctx = CoordinatorContext(
            intent=IntentType.GENERATE,
            document_ids=["doc-1"],
            content_type=ContentType.FLASH_CARD,  # explicit override
        )
        # Text says "summary" but context says flash_card
        await coordinator.execute(mock_db, "summarize doc 1", context=ctx)
        mock_content_gen_agent.generate_flash_cards.assert_awaited_once()
        mock_content_gen_agent.generate_summary.assert_not_called()

    @pytest.mark.asyncio
    async def test_context_recursive_false_passed_to_ingest(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_ingestion_agent: MagicMock,
        tmp_path: Any,
    ) -> None:
        ctx = CoordinatorContext(
            intent=IntentType.INGEST,
            file_path=str(tmp_path),
            recursive=False,
        )
        await coordinator.execute(mock_db, "ingest dir", context=ctx)
        _, kwargs = mock_ingestion_agent.ingest_directory.call_args
        assert kwargs["recursive"] is False


# =============================================================================
# Generate Content Type Routing Tests
# =============================================================================


class TestGenerateContentTypeRouting:
    """All GeneratedContent types route to the right method."""

    @pytest.mark.asyncio
    async def test_summary_routes_to_generate_summary(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_content_gen_agent: MagicMock,
    ) -> None:
        ctx = CoordinatorContext(
            intent=IntentType.GENERATE,
            document_ids=["doc-1"],
            content_type=ContentType.SUMMARY,
        )
        await coordinator.execute(mock_db, "generate summary", context=ctx)
        mock_content_gen_agent.generate_summary.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_flashcard_routes_to_generate_flash_cards(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_content_gen_agent: MagicMock,
    ) -> None:
        ctx = CoordinatorContext(
            intent=IntentType.GENERATE,
            document_ids=["doc-1"],
            content_type=ContentType.FLASH_CARD,
            flashcard_count=15,
        )
        await coordinator.execute(mock_db, "generate flashcards", context=ctx)
        mock_content_gen_agent.generate_flash_cards.assert_awaited_once_with(
            mock_db, ["doc-1"], count=15
        )

    @pytest.mark.asyncio
    async def test_cliff_notes_routes_correctly(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_content_gen_agent: MagicMock,
    ) -> None:
        ctx = CoordinatorContext(
            intent=IntentType.GENERATE,
            document_ids=["doc-1"],
            content_type=ContentType.CLIFF_NOTES,
        )
        await coordinator.execute(mock_db, "cliff notes", context=ctx)
        mock_content_gen_agent.generate_cliff_notes.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_outline_routes_correctly(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_content_gen_agent: MagicMock,
    ) -> None:
        ctx = CoordinatorContext(
            intent=IntentType.GENERATE,
            document_ids=["doc-1"],
            content_type=ContentType.OUTLINE,
        )
        await coordinator.execute(mock_db, "outline", context=ctx)
        mock_content_gen_agent.generate_outline.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_extract_routes_correctly(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_content_gen_agent: MagicMock,
    ) -> None:
        ctx = CoordinatorContext(
            intent=IntentType.GENERATE,
            document_ids=["doc-1"],
            content_type=ContentType.EXTRACT,
        )
        await coordinator.execute(mock_db, "extract methodology", context=ctx)
        mock_content_gen_agent.generate_extract.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_generate_infers_content_type_from_text(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_content_gen_agent: MagicMock,
    ) -> None:
        """When content_type not forced in context, text is used to infer it."""
        ctx = CoordinatorContext(
            intent=IntentType.GENERATE,
            document_ids=["doc-1"],
            # No content_type forced
        )
        await coordinator.execute(mock_db, "create flashcards for this doc", context=ctx)
        mock_content_gen_agent.generate_flash_cards.assert_awaited_once()


# =============================================================================
# Edge Cases
# =============================================================================


class TestCoordinatorEdgeCases:
    """Boundary conditions and unusual inputs."""

    @pytest.mark.asyncio
    async def test_empty_input_returns_error(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
    ) -> None:
        result = await coordinator.execute(mock_db, "")
        assert result.error is not None
        assert result.intent == IntentType.UNKNOWN

    @pytest.mark.asyncio
    async def test_whitespace_only_input_returns_error(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
    ) -> None:
        result = await coordinator.execute(mock_db, "   \t\n  ")
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_unknown_intent_falls_back_to_query(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_query_agent: MagicMock,
    ) -> None:
        result = await coordinator.execute(mock_db, "xyzzy frobnicator nonsense")
        assert result.intent == IntentType.QUERY
        mock_query_agent.query.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_very_long_input_is_handled(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
    ) -> None:
        long_input = "what is " + "A" * 5000 + "?"
        result = await coordinator.execute(mock_db, long_input)
        assert result.error is None
        assert result.intent == IntentType.QUERY

    @pytest.mark.asyncio
    async def test_unicode_input_is_handled(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
    ) -> None:
        result = await coordinator.execute(mock_db, "¿Qué es el aprendizaje automático?")
        assert result.error is None

    @pytest.mark.asyncio
    async def test_result_is_coordinator_result_type(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
    ) -> None:
        result = await coordinator.execute(mock_db, "what is AI?")
        assert isinstance(result, CoordinatorResult)


# =============================================================================
# Missing Agent Error Handling
# =============================================================================


class TestMissingAgents:
    """Descriptive errors when a required agent is not configured."""

    @pytest.mark.asyncio
    async def test_missing_ingestion_agent_returns_error(
        self,
        mock_db: AsyncMock,
        tmp_path: Any,
    ) -> None:
        agent = CoordinatorAgent(ingestion_agent=None)
        sample = tmp_path / "file.pdf"
        sample.touch()
        ctx = CoordinatorContext(
            intent=IntentType.INGEST,
            file_path=str(sample),
        )
        result = await agent.execute(mock_db, "ingest file", context=ctx)
        assert result.error is not None
        assert "IngestionAgent" in result.error

    @pytest.mark.asyncio
    async def test_missing_query_agent_returns_error(
        self,
        mock_db: AsyncMock,
    ) -> None:
        agent = CoordinatorAgent(query_agent=None)
        ctx = CoordinatorContext(intent=IntentType.QUERY)
        result = await agent.execute(mock_db, "what is AI?", context=ctx)
        assert result.error is not None
        assert "QueryAgent" in result.error

    @pytest.mark.asyncio
    async def test_missing_content_gen_agent_returns_error(
        self,
        mock_db: AsyncMock,
    ) -> None:
        agent = CoordinatorAgent(content_gen_agent=None)
        ctx = CoordinatorContext(
            intent=IntentType.GENERATE,
            document_ids=["doc-1"],
        )
        result = await agent.execute(mock_db, "summarize", context=ctx)
        assert result.error is not None
        assert "ContentGenerationAgent" in result.error

    @pytest.mark.asyncio
    async def test_missing_watcher_agent_returns_error(
        self,
        mock_db: AsyncMock,
        tmp_path: Any,
    ) -> None:
        agent = CoordinatorAgent(watcher_agent=None)
        ctx = CoordinatorContext(
            intent=IntentType.WATCH,
            file_path=str(tmp_path),
        )
        result = await agent.execute(mock_db, "watch dir", context=ctx)
        assert result.error is not None
        assert "WatcherAgent" in result.error

    @pytest.mark.asyncio
    async def test_no_agents_configured_still_returns_result(
        self,
        mock_db: AsyncMock,
    ) -> None:
        agent = CoordinatorAgent()
        result = await agent.execute(mock_db, "what is entropy?")
        # Falls through to query → error because no query agent
        assert isinstance(result, CoordinatorResult)
        assert result.error is not None


# =============================================================================
# Input Validation Tests
# =============================================================================


class TestCoordinatorInputValidation:
    """Invalid / missing parameters produce clear errors, not crashes."""

    @pytest.mark.asyncio
    async def test_ingest_without_path_returns_error(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
    ) -> None:
        ctx = CoordinatorContext(intent=IntentType.INGEST)
        # No file_path in context or inferable from text
        result = await coordinator.execute(mock_db, "ingest something", context=ctx)
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_generate_without_doc_ids_returns_error(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
    ) -> None:
        ctx = CoordinatorContext(
            intent=IntentType.GENERATE,
            document_ids=[],  # Empty
        )
        result = await coordinator.execute(mock_db, "summarize", context=ctx)
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_unwatch_without_watch_id_returns_error(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
    ) -> None:
        ctx = CoordinatorContext(
            intent=IntentType.UNWATCH,
            watch_id=None,  # Not provided
        )
        result = await coordinator.execute(mock_db, "unwatch", context=ctx)
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_watch_without_path_returns_error(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
    ) -> None:
        ctx = CoordinatorContext(intent=IntentType.WATCH)
        result = await coordinator.execute(mock_db, "watch something", context=ctx)
        assert result.error is not None


# =============================================================================
# Path Extraction Tests
# =============================================================================


class TestPathExtraction:
    """_extract_path() finds paths from free-form text."""

    def test_unix_absolute_path(self) -> None:
        path = CoordinatorAgent._extract_path("ingest /home/user/documents")
        assert path == "/home/user/documents"

    def test_unix_path_with_subdir(self) -> None:
        path = CoordinatorAgent._extract_path("scan /data/research/papers")
        assert path == "/data/research/papers"

    def test_windows_absolute_path(self) -> None:
        path = CoordinatorAgent._extract_path(r"ingest C:\Users\data\docs")
        assert path == r"C:\Users\data\docs"

    def test_gdrive_scheme(self) -> None:
        path = CoordinatorAgent._extract_path("watch gdrive://Documents/Research")
        assert path == "gdrive://Documents/Research"

    def test_onedrive_scheme(self) -> None:
        path = CoordinatorAgent._extract_path("ingest onedrive://Notes")
        assert path == "onedrive://Notes"

    def test_rclone_scheme(self) -> None:
        path = CoordinatorAgent._extract_path("scan rclone://remote/path")
        assert path == "rclone://remote/path"

    def test_no_path_returns_none(self) -> None:
        path = CoordinatorAgent._extract_path("what is machine learning?")
        assert path is None

    def test_cloud_scheme_takes_priority_over_unix(self) -> None:
        path = CoordinatorAgent._extract_path("watch gdrive://docs /tmp/fallback")
        assert path == "gdrive://docs"


# =============================================================================
# Convenience Method Tests
# =============================================================================


class TestConvenienceMethods:
    """coordinator.ingest(), .query(), .generate() wrappers."""

    @pytest.mark.asyncio
    async def test_ingest_method_with_file(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_ingestion_agent: MagicMock,
        tmp_path: Any,
    ) -> None:
        sample = tmp_path / "doc.txt"
        sample.touch()
        result = await coordinator.ingest(mock_db, str(sample))
        assert result.intent == IntentType.INGEST
        assert result.error is None
        mock_ingestion_agent.ingest_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ingest_method_with_directory(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_ingestion_agent: MagicMock,
        tmp_path: Any,
    ) -> None:
        result = await coordinator.ingest(mock_db, str(tmp_path), recursive=True)
        assert result.intent == IntentType.INGEST
        mock_ingestion_agent.ingest_directory.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_query_method(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_query_agent: MagicMock,
    ) -> None:
        result = await coordinator.query(mock_db, "what is entropy?")
        assert result.intent == IntentType.QUERY
        assert result.error is None
        mock_query_agent.query.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_query_method_passes_top_k(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_query_agent: MagicMock,
    ) -> None:
        await coordinator.query(mock_db, "question", top_k=20)
        _, kwargs = mock_query_agent.query.call_args
        assert kwargs["top_k"] == 20

    @pytest.mark.asyncio
    async def test_query_method_passes_filter(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_query_agent: MagicMock,
    ) -> None:
        filters = {"category": "biology"}
        await coordinator.query(mock_db, "question", filter_dict=filters)
        _, kwargs = mock_query_agent.query.call_args
        assert kwargs["filter_dict"] == filters

    @pytest.mark.asyncio
    async def test_generate_method(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_content_gen_agent: MagicMock,
    ) -> None:
        result = await coordinator.generate(
            mock_db, ["doc-1"], ContentType.SUMMARY
        )
        assert result.intent == IntentType.GENERATE
        assert result.error is None
        mock_content_gen_agent.generate_summary.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_generate_method_flash_cards(
        self,
        coordinator: CoordinatorAgent,
        mock_db: AsyncMock,
        mock_content_gen_agent: MagicMock,
    ) -> None:
        await coordinator.generate(
            mock_db, ["doc-1"], ContentType.FLASH_CARD, count=20
        )
        mock_content_gen_agent.generate_flash_cards.assert_awaited_once_with(
            mock_db, ["doc-1"], count=20
        )


# =============================================================================
# LLM Fallback Classification Tests
# =============================================================================


class TestLLMFallback:
    """LLM-assisted classification when keyword confidence is low."""

    @pytest.mark.asyncio
    async def test_llm_fallback_called_on_low_confidence(
        self,
        mock_db: AsyncMock,
        mock_query_agent: MagicMock,
    ) -> None:
        coordinator = CoordinatorAgent(
            query_agent=mock_query_agent,
            llm_url="http://localhost:11434",
            llm_model="test-model",
            use_llm_fallback=True,
            llm_fallback_threshold=0.99,  # Force fallback for all inputs
        )

        with patch.object(
            coordinator, "_llm_classify", new_callable=AsyncMock
        ) as mock_classify:
            mock_classify.return_value = IntentType.QUERY
            result = await coordinator.execute(mock_db, "xyzzy frobnicator")

        mock_classify.assert_awaited_once()
        assert result.intent == IntentType.QUERY

    @pytest.mark.asyncio
    async def test_llm_fallback_not_called_on_high_confidence(
        self,
        mock_db: AsyncMock,
        mock_query_agent: MagicMock,
    ) -> None:
        coordinator = CoordinatorAgent(
            query_agent=mock_query_agent,
            use_llm_fallback=True,
            llm_fallback_threshold=0.5,  # Only call LLM below 0.5
        )

        with patch.object(
            coordinator, "_llm_classify", new_callable=AsyncMock
        ) as mock_classify:
            # "what is X?" has confidence ~0.7 → above threshold
            await coordinator.execute(mock_db, "what is machine learning?")

        mock_classify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_llm_fallback_network_error_is_handled(
        self,
        mock_db: AsyncMock,
        mock_query_agent: MagicMock,
    ) -> None:
        coordinator = CoordinatorAgent(
            query_agent=mock_query_agent,
            use_llm_fallback=True,
            llm_fallback_threshold=0.99,
        )

        with patch.object(
            coordinator, "_llm_classify", new_callable=AsyncMock,
            side_effect=Exception("network error"),
        ):
            # Should not raise; falls through to keyword result
            result = await coordinator.execute(mock_db, "xyzzy frobnicator")

        assert isinstance(result, CoordinatorResult)

    @pytest.mark.asyncio
    async def test_llm_fallback_disabled_by_default(
        self,
        mock_db: AsyncMock,
        mock_query_agent: MagicMock,
    ) -> None:
        coordinator = CoordinatorAgent(
            query_agent=mock_query_agent,
            use_llm_fallback=False,
        )

        with patch.object(
            coordinator, "_llm_classify", new_callable=AsyncMock
        ) as mock_classify:
            await coordinator.execute(mock_db, "xyzzy frobnicator")

        mock_classify.assert_not_awaited()


# =============================================================================
# CoordinatorResult Model Tests
# =============================================================================


class TestCoordinatorResultModel:
    """CoordinatorResult Pydantic model fields and defaults."""

    def test_required_fields(self) -> None:
        result = CoordinatorResult(
            intent=IntentType.QUERY,
            agent_used="QueryAgent",
            input_text="test",
        )
        assert result.intent == IntentType.QUERY
        assert result.agent_used == "QueryAgent"
        assert result.input_text == "test"

    def test_default_values(self) -> None:
        result = CoordinatorResult(
            intent=IntentType.QUERY,
            agent_used="QueryAgent",
            input_text="test",
        )
        assert result.result is None
        assert result.error is None
        assert result.confidence == 1.0
        assert result.duration_ms == 0

    def test_error_field(self) -> None:
        result = CoordinatorResult(
            intent=IntentType.UNKNOWN,
            agent_used="none",
            input_text="",
            error="Something went wrong",
        )
        assert result.error == "Something went wrong"


# =============================================================================
# CoordinatorContext Model Tests
# =============================================================================


class TestCoordinatorContextModel:
    """CoordinatorContext defaults and field types."""

    def test_default_values(self) -> None:
        ctx = CoordinatorContext()
        assert ctx.intent is None
        assert ctx.file_path is None
        assert ctx.recursive is True
        assert ctx.document_ids == []
        assert ctx.content_type is None
        assert ctx.generation_style == "concise"
        assert ctx.flashcard_count == 10
        assert ctx.top_k == 5
        assert ctx.filter_dict is None
        assert ctx.watch_id is None
        assert ctx.watch_backend == "local"
        assert ctx.use_cache is True

    def test_all_fields_settable(self) -> None:
        ctx = CoordinatorContext(
            intent=IntentType.GENERATE,
            file_path="/tmp/test",
            recursive=False,
            document_ids=["a", "b"],
            content_type=ContentType.FLASH_CARD,
            generation_style="detailed",
            flashcard_count=20,
            top_k=10,
            filter_dict={"tag": "bio"},
            watch_id="w-123",
            watch_backend="gdrive",
            use_cache=False,
        )
        assert ctx.intent == IntentType.GENERATE
        assert ctx.file_path == "/tmp/test"
        assert ctx.recursive is False
        assert ctx.document_ids == ["a", "b"]
        assert ctx.flashcard_count == 20
        assert ctx.use_cache is False
