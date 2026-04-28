"""Tests for WikiAgent — wiki compilation engine."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.agents.wiki import (
    CompileResult,
    ContradictionAction,
    ContradictionResult,
    EntityExtraction,
    WikiAgent,
)
from grimoire.db.models import (
    CompileStatus,
    Document,
    WikiCompileJob,
    WikiCrossReference,
    WikiPage,
    WikiPageSection,
    WikiPageStatus,
    WikiRefType,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def source_priorities() -> dict[str, int]:
    return {"architecture-dec": 10, "meeting-notes": 3, "research-paper": 7}


@pytest.fixture
def agent(source_priorities: dict[str, int]) -> WikiAgent:
    return WikiAgent(
        llm_url="http://localhost:11434",
        llm_model="test-model",
        source_priorities=source_priorities,
    )


@pytest_asyncio.fixture
async def mock_db() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    yield session


def make_mock_document(
    doc_id: str = "doc-1",
    title: str = "Test Doc",
    source_path: str = "test.md",
) -> MagicMock:
    doc = MagicMock(spec=Document)
    doc.id = doc_id
    doc.title = title
    doc.source_path = source_path
    doc.created_at = datetime.now(timezone.utc)
    return doc


# ============================================================================
# Test Entity Extraction
# ============================================================================


class TestIdentifyEntities:
    """Test _identify_entities LLM call and parsing."""

    @pytest.mark.asyncio
    async def test_identifies_entities_from_chunks(
        self, agent: WikiAgent
    ) -> None:
        """LLM returns valid entity list."""
        mock_response = (
            '[{"name": "Auth Pipeline", "entity_type": "process", '
            '"summary": "Handles auth", "confidence": 0.9}]'
        )
        with patch.object(agent, "_call_llm", new_callable=AsyncMock, return_value=mock_response):
            entities = await agent._identify_entities(["some chunk text"])
        assert len(entities) == 1
        assert entities[0].name == "Auth Pipeline"
        assert entities[0].entity_type == "process"
        assert entities[0].confidence == 0.9

    @pytest.mark.asyncio
    async def test_empty_entity_list(
        self, agent: WikiAgent
    ) -> None:
        """LLM returns no entities."""
        with patch.object(agent, "_call_llm", new_callable=AsyncMock, return_value="[]"):
            entities = await agent._identify_entities(["some text"])
        assert len(entities) == 0

    @pytest.mark.asyncio
    async def test_malformed_llm_response(
        self, agent: WikiAgent
    ) -> None:
        """LLM returns garbage — should return empty list, not crash."""
        with patch.object(agent, "_call_llm", new_callable=AsyncMock, return_value="not json"):
            entities = await agent._identify_entities(["text"])
        assert entities == []


# ============================================================================
# Test Contradiction Detection
# ============================================================================


class TestDetectContradictions:
    """Test _detect_contradictions LLM call."""

    @pytest.mark.asyncio
    async def test_factual_conflict(self, agent: WikiAgent) -> None:
        """LLM identifies a factual contradiction."""
        mock_response = (
            '{"conflict_type": "factual", "description": "Port mismatch", '
            '"existing_claim": "port 5432", "new_claim": "port 5434", '
            '"severity": "high"}'
        )
        existing = WikiPageSection(
            heading="Config",
            content="Uses port 5432",
            source_priority=5,
        )
        with patch.object(agent, "_call_llm", new_callable=AsyncMock, return_value=mock_response):
            result = await agent._detect_contradictions(existing, "Uses port 5434")
        assert result is not None
        assert result.conflict_type == "factual"

    @pytest.mark.asyncio
    async def test_no_conflict(self, agent: WikiAgent) -> None:
        """LLM finds no contradiction."""
        with patch.object(agent, "_call_llm", new_callable=AsyncMock, return_value="none"):
            result = await agent._detect_contradictions(
                WikiPageSection(heading="Overview", content="Same info", source_priority=5),
                "Same info from another source",
            )
        assert result is None


# ============================================================================
# Test Contradiction Policy
# ============================================================================


class TestContradictionPolicy:
    """Test _apply_contradiction_policy routing."""

    def test_factual_uses_newer_wins(self, agent: WikiAgent) -> None:
        """Factual conflict routes to newer-wins."""
        contradiction = ContradictionResult(
            conflict_type="factual",
            description="Port mismatch",
            existing_claim="5432",
            new_claim="5434",
            severity="high",
        )
        action = agent._apply_contradiction_policy(contradiction)
        assert action == ContradictionAction.NEWER_WINS

    def test_temporal_uses_newer_wins(self, agent: WikiAgent) -> None:
        """Temporal drift routes to newer-wins."""
        contradiction = ContradictionResult(
            conflict_type="temporal",
            description="API version changed",
            existing_claim="v2",
            new_claim="v3",
            severity="medium",
        )
        action = agent._apply_contradiction_policy(contradiction)
        assert action == ContradictionAction.NEWER_WINS

    def test_scope_uses_source_priority(self, agent: WikiAgent) -> None:
        """Scope mismatch routes to source-priority."""
        contradiction = ContradictionResult(
            conflict_type="scope",
            description="Language coverage",
            existing_claim="Python only",
            new_claim="Python and Go",
            severity="medium",
        )
        action = agent._apply_contradiction_policy(contradiction)
        assert action == ContradictionAction.SOURCE_PRIORITY

    def test_terminology_uses_source_priority(self, agent: WikiAgent) -> None:
        """Terminology shift routes to source-priority."""
        contradiction = ContradictionResult(
            conflict_type="terminology",
            description="Name change",
            existing_claim="intake pipeline",
            new_claim="ingestion pipeline",
            severity="low",
        )
        action = agent._apply_contradiction_policy(contradiction)
        assert action == ContradictionAction.SOURCE_PRIORITY


# ============================================================================
# Test Slug Generation
# ============================================================================


class TestSlugGeneration:
    """Test _slugify method."""

    def test_simple_title(self, agent: WikiAgent) -> None:
        assert agent._slugify("Authentication Pipeline") == "authentication-pipeline"

    def test_special_chars(self, agent: WikiAgent) -> None:
        assert agent._slugify("API / REST Endpoints") == "api-rest-endpoints"

    def test_multiple_spaces(self, agent: WikiAgent) -> None:
        assert agent._slugify("ML  &  AI") == "ml-ai"


# ============================================================================
# Test Source Priority Resolution
# ============================================================================


class TestSourcePriority:
    """Test _resolve_source_priority method."""

    def test_known_source(self, agent: WikiAgent) -> None:
        assert agent._resolve_source_priority("architecture-dec") == 10

    def test_unknown_source(self, agent: WikiAgent) -> None:
        assert agent._resolve_source_priority("unknown") == 0

    def test_path_based_matching(self, agent: WikiAgent) -> None:
        """Source path containing priority key should match."""
        assert agent._resolve_source_priority("docs/architecture-dec/auth.md") == 10


# ============================================================================
# Test compile_document
# ============================================================================


class TestCompileDocument:
    """Test compile_document end-to-end flow."""

    @pytest.mark.asyncio
    async def test_creates_new_page(
        self, agent: WikiAgent, mock_db: AsyncMock
    ) -> None:
        """Compiling a document with new entities creates wiki pages."""
        mock_db.get = AsyncMock(return_value=make_mock_document())
        mock_db.flush = AsyncMock()
        mock_db.add = MagicMock()

        # Mock the database query for _get_or_create_job
        mock_job = MagicMock()
        mock_job.status = CompileStatus.PENDING
        mock_job.document_id = "doc-1"
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = mock_job
        mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=mock_scalars)))

        # Mock _fetch_chunks to return content
        mock_chunk = MagicMock()
        mock_chunk.content = "Auth pipeline uses JWT tokens"
        with patch.object(
            agent, "_fetch_chunks", new_callable=AsyncMock,
            return_value=[mock_chunk],
        ), patch.object(
            agent, "_identify_entities", new_callable=AsyncMock,
            return_value=[EntityExtraction(
                name="Auth Pipeline", entity_type="process",
                summary="Handles auth", confidence=0.9,
            )],
        ), patch.object(
            agent, "_match_existing_page", new_callable=AsyncMock,
            return_value=None,
        ), patch.object(
            agent, "_generate_page", new_callable=AsyncMock,
            return_value=(MagicMock(id="page-1"), 1),
        ), patch.object(
            agent, "_assemble_page_content", new_callable=AsyncMock,
        ), patch.object(
            agent, "_discover_cross_references", new_callable=AsyncMock,
            return_value=0,
        ):
            result = await agent.compile_document(mock_db, "doc-1")

        assert result.pages_created == 1
        assert result.error is None

    @pytest.mark.asyncio
    async def test_no_chunks_returns_empty(
        self, agent: WikiAgent, mock_db: AsyncMock
    ) -> None:
        """Document with no chunks produces empty result."""
        # Mock _get_or_create_job
        mock_job = MagicMock()
        mock_job.status = CompileStatus.PENDING
        mock_job.document_id = "doc-1"
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = mock_job
        mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=mock_scalars)))
        mock_db.flush = AsyncMock()

        with patch.object(
            agent, "_fetch_chunks", new_callable=AsyncMock, return_value=[],
        ):
            result = await agent.compile_document(mock_db, "doc-1")

        assert result.pages_created == 0
        assert result.pages_updated == 0

    @pytest.mark.asyncio
    async def test_no_entities_returns_empty(
        self, agent: WikiAgent, mock_db: AsyncMock
    ) -> None:
        """Document with chunks but no entities produces empty result."""
        mock_db.get = AsyncMock(return_value=make_mock_document())
        mock_db.flush = AsyncMock()

        # Mock _get_or_create_job
        mock_job = MagicMock()
        mock_job.status = CompileStatus.PENDING
        mock_job.document_id = "doc-1"
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = mock_job
        mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=mock_scalars)))

        mock_chunk = MagicMock()
        mock_chunk.content = "some text"

        with patch.object(
            agent, "_fetch_chunks", new_callable=AsyncMock,
            return_value=[mock_chunk],
        ), patch.object(
            agent, "_identify_entities", new_callable=AsyncMock, return_value=[],
        ):
            result = await agent.compile_document(mock_db, "doc-1")

        assert result.pages_created == 0
        assert result.pages_updated == 0

    @pytest.mark.asyncio
    async def test_already_compiled_skips(
        self, agent: WikiAgent, mock_db: AsyncMock
    ) -> None:
        """Document already compiled returns empty result without processing."""
        mock_job = MagicMock()
        mock_job.status = CompileStatus.COMPLETED
        mock_job.document_id = "doc-1"
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = mock_job
        mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=mock_scalars)))

        result = await agent.compile_document(mock_db, "doc-1")

        assert result.pages_created == 0
        assert result.pages_updated == 0

    @pytest.mark.asyncio
    async def test_llm_failure_marks_failed(
        self, agent: WikiAgent, mock_db: AsyncMock
    ) -> None:
        """LLM failure during compilation marks job as FAILED."""
        mock_db.get = AsyncMock(return_value=make_mock_document())
        mock_db.flush = AsyncMock()

        # Mock _get_or_create_job
        mock_job = MagicMock()
        mock_job.status = CompileStatus.PENDING
        mock_job.document_id = "doc-1"
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = mock_job
        mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=mock_scalars)))

        mock_chunk = MagicMock()
        mock_chunk.content = "some text"

        # Make _identify_entities raise an exception
        with patch.object(
            agent, "_fetch_chunks", new_callable=AsyncMock,
            return_value=[mock_chunk],
        ), patch.object(
            agent, "_identify_entities", new_callable=AsyncMock,
            side_effect=RuntimeError("LLM unavailable"),
        ):
            result = await agent.compile_document(mock_db, "doc-1")

        assert result.error is not None
        assert "LLM unavailable" in result.error


class TestCompilePending:
    """Test compile_pending batch processing."""

    @pytest.mark.asyncio
    async def test_compile_pending_processes_jobs(
        self, agent: WikiAgent, mock_db: AsyncMock
    ) -> None:
        """compile_pending processes all pending jobs."""
        with patch.object(
            agent, "compile_document", new_callable=AsyncMock,
            return_value=CompileResult(document_id="doc-1"),
        ):
            # Mock the query for pending jobs
            mock_job = MagicMock()
            mock_job.document_id = "doc-1"
            mock_scalars = MagicMock()
            mock_scalars.all.return_value = [mock_job]
            mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=mock_scalars)))

            results = await agent.compile_pending(mock_db)

        assert len(results) == 1
        assert results[0].document_id == "doc-1"