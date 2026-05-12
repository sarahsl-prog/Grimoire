"""Tests for the Ingestion Agent.

Tests cover:
- Happy path: single file ingestion, batch ingestion
- File type detection
- Chunking strategy selection
- Dedup handling (new, skip, update)
- Error handling: parse failures, embedding failures
- Edge cases: empty directory, unsupported files
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from grimoire.agents.ingestion import (
    BatchIngestionResult,
    IngestionAgent,
    IngestionResult,
    _select_chunking_strategy,
    detect_file_type,
)
from grimoire.core.chunker.base import Chunk, ChunkConfig, ChunkingStrategy
from grimoire.core.dedup import DeduplicationAction, DedupResult
from grimoire.core.parser import DocumentMetadata, ParsedDocument
from grimoire.db.models import (
    ActionType,
    Category,
    Document,
    FileType,
    ProcessingStatus,
    StorageBackend,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_parser() -> MagicMock:
    """Create a mock DocumentParser."""
    parser = MagicMock()
    parser.parse = AsyncMock(
        return_value=ParsedDocument(
            text="Sample document text for testing purposes.",
            metadata=DocumentMetadata(
                title="Test Document",
                file_size=1024,
                file_hash="abc123",
            ),
            status="success",
        )
    )
    parser.SUPPORTED_EXTENSIONS = {
        ".pdf", ".docx", ".doc", ".pptx", ".xlsx",
        ".html", ".htm", ".png", ".jpg", ".md", ".txt",
    }
    return parser


@pytest.fixture
def mock_embedder() -> MagicMock:
    """Create a mock Embedder."""
    embedder = MagicMock()
    embedder.embed = AsyncMock(
        return_value=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    )
    return embedder


@pytest.fixture
def mock_vector_store() -> MagicMock:
    """Create a mock VectorStore."""
    store = MagicMock()
    store.add_documents = AsyncMock(return_value=None)
    store.delete = AsyncMock(return_value=None)
    store.search = AsyncMock(return_value=[])
    return store


@pytest.fixture
def mock_tagger() -> MagicMock:
    """Create a mock Tagger."""
    tagger = MagicMock()
    tag_result = MagicMock()
    tag_result.applied_tags = [MagicMock(), MagicMock()]
    tag_result.model_used = "test-model"
    tagger.tag_document = AsyncMock(return_value=tag_result)
    return tagger


@pytest.fixture
def mock_db() -> AsyncMock:
    """Create a mock database session."""
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.delete = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def sample_chunks() -> List[Chunk]:
    """Create sample chunks for testing."""
    return [
        Chunk(
            content="First chunk content.",
            token_count=5,
            index=0,
            metadata={"chunk_id": "chunk-0"},
        ),
        Chunk(
            content="Second chunk content.",
            token_count=5,
            index=1,
            metadata={"chunk_id": "chunk-1"},
        ),
    ]


@pytest.fixture
def agent(
    mock_parser: MagicMock,
    mock_embedder: MagicMock,
    mock_vector_store: MagicMock,
    mock_tagger: MagicMock,
) -> IngestionAgent:
    """Create an IngestionAgent with all mocked dependencies."""
    return IngestionAgent(
        parser=mock_parser,
        embedder=mock_embedder,
        vector_store=mock_vector_store,
        tagger=mock_tagger,
    )


# =============================================================================
# File Type Detection Tests
# =============================================================================


class TestFileTypeDetection:
    """File type detection from extensions."""

    def test_pdf_detection(self) -> None:
        assert detect_file_type("document.pdf") == FileType.PDF

    def test_docx_detection(self) -> None:
        assert detect_file_type("document.docx") == FileType.DOCX

    def test_doc_detection(self) -> None:
        assert detect_file_type("document.doc") == FileType.DOCX

    def test_pptx_detection(self) -> None:
        assert detect_file_type("slides.pptx") == FileType.PPTX

    def test_xlsx_detection(self) -> None:
        assert detect_file_type("data.xlsx") == FileType.XLSX

    def test_html_detection(self) -> None:
        assert detect_file_type("page.html") == FileType.HTML

    def test_htm_detection(self) -> None:
        assert detect_file_type("page.htm") == FileType.HTML

    def test_markdown_detection(self) -> None:
        assert detect_file_type("readme.md") == FileType.MD

    def test_text_detection(self) -> None:
        assert detect_file_type("notes.txt") == FileType.TXT

    def test_image_detection(self) -> None:
        for ext in [".png", ".jpg", ".jpeg", ".tiff", ".gif", ".bmp", ".webp"]:
            assert detect_file_type(f"image{ext}") == FileType.IMAGE

    def test_unknown_extension(self) -> None:
        assert detect_file_type("data.xyz") == FileType.OTHER

    def test_case_insensitive(self) -> None:
        assert detect_file_type("DOCUMENT.PDF") == FileType.PDF

    def test_path_object(self) -> None:
        assert detect_file_type(Path("/some/path/doc.pdf")) == FileType.PDF


# =============================================================================
# Chunking Strategy Selection Tests
# =============================================================================


class TestChunkingStrategySelection:
    """Strategy selection based on file type."""

    def test_markdown_uses_markdown_strategy(self) -> None:
        assert _select_chunking_strategy("file.md") == ChunkingStrategy.MARKDOWN

    def test_pdf_uses_recursive_strategy(self) -> None:
        assert _select_chunking_strategy("file.pdf") == ChunkingStrategy.RECURSIVE

    def test_txt_uses_recursive_strategy(self) -> None:
        assert _select_chunking_strategy("file.txt") == ChunkingStrategy.RECURSIVE

    def test_docx_uses_recursive_strategy(self) -> None:
        assert _select_chunking_strategy("file.docx") == ChunkingStrategy.RECURSIVE


# =============================================================================
# Happy Path Tests
# =============================================================================


class TestIngestionHappyPath:
    """Standard ingestion scenarios."""

    @pytest.mark.asyncio
    async def test_ingest_new_file(
        self,
        agent: IngestionAgent,
        mock_db: AsyncMock,
        mock_parser: MagicMock,
        sample_chunks: List[Chunk],
        tmp_path: Path,
    ) -> None:
        """Can ingest a new file through the full pipeline."""
        # Create a temp file
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello world")

        # Mock dedup: no existing document
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Mock chunking
        with patch.object(agent, "_chunk_document", new_callable=AsyncMock) as mock_chunk:
            mock_chunk.return_value = sample_chunks

            result = await agent.ingest_file(mock_db, test_file)

        assert result.status == "completed"
        assert result.chunks_created == 2
        assert result.vectors_stored == 2
        assert result.duration_ms >= 0
        assert result.file_path == str(test_file)

    @pytest.mark.asyncio
    async def test_ingest_skips_duplicate(
        self,
        agent: IngestionAgent,
        mock_db: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Duplicate files are skipped."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello world")

        # Mock dedup: existing document with same hash
        existing_doc = MagicMock(spec=Document)
        existing_doc.id = "existing-doc-id"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_doc
        mock_db.execute.return_value = mock_result

        # Mock deduplicator to return SKIP
        with patch.object(
            agent._deduplicator, "check_file", new_callable=AsyncMock,
        ) as mock_dedup:
            mock_dedup.return_value = DedupResult(
                action=DeduplicationAction.SKIP,
                file_hash="abc123",
                existing_document=existing_doc,
            )
            result = await agent.ingest_file(mock_db, test_file)

        assert result.status == "skipped"
        assert result.document_id == "existing-doc-id"

    @pytest.mark.asyncio
    async def test_ingest_batch_directory(
        self,
        agent: IngestionAgent,
        mock_db: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Can process a directory of files."""
        # Create test files
        (tmp_path / "doc1.txt").write_text("Document 1")
        (tmp_path / "doc2.txt").write_text("Document 2")
        (tmp_path / "ignored.xyz").write_text("Not supported")

        # Mock dedup and chunking
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        # First call returns no dup, rest return categories
        mock_db.execute.return_value = mock_result

        chunks = [
            Chunk(content="Test chunk.", token_count=3, index=0, metadata={"chunk_id": "c0"}),
        ]

        with patch.object(agent, "_chunk_document", new_callable=AsyncMock) as mock_chunk:
            mock_chunk.return_value = chunks
            agent._embedder.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])

            batch_result = await agent.ingest_directory(mock_db, tmp_path)

        assert batch_result.total == 2  # Only .txt files
        assert batch_result.succeeded == 2
        assert batch_result.failed == 0


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestIngestionErrorHandling:
    """Error handling during ingestion."""

    @pytest.mark.asyncio
    async def test_parse_failure_creates_failed_record(
        self,
        agent: IngestionAgent,
        mock_db: AsyncMock,
        mock_parser: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Parse failures create a failed document record."""
        test_file = tmp_path / "corrupt.pdf"
        test_file.write_bytes(b"not a real pdf")

        # Mock parser to return failure
        mock_parser.parse = AsyncMock(
            return_value=ParsedDocument(
                text="",
                status="failed",
                error_message="Corrupt file",
            )
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await agent.ingest_file(mock_db, test_file)

        assert result.status == "failed"
        assert result.error_message == "Corrupt file"

    @pytest.mark.asyncio
    async def test_embedding_failure_returns_error(
        self,
        agent: IngestionAgent,
        mock_db: AsyncMock,
        mock_parser: MagicMock,
        tmp_path: Path,
        sample_chunks: List[Chunk],
    ) -> None:
        """Embedding failures are handled gracefully."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello world")

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Mock chunking to succeed, embedding to fail
        with patch.object(agent, "_chunk_document", new_callable=AsyncMock) as mock_chunk:
            mock_chunk.return_value = sample_chunks
            agent._embedder.embed = AsyncMock(
                side_effect=RuntimeError("GPU out of memory")
            )

            result = await agent.ingest_file(mock_db, test_file)

        assert result.status == "failed"
        assert "GPU out of memory" in result.error_message

    @pytest.mark.asyncio
    async def test_auto_tag_failure_does_not_fail_ingestion(
        self,
        agent: IngestionAgent,
        mock_db: AsyncMock,
        mock_parser: MagicMock,
        tmp_path: Path,
        sample_chunks: List[Chunk],
    ) -> None:
        """Auto-tagging failure should not fail the whole ingestion."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello world")

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        # Mock categories query to return empty list
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        with patch.object(agent, "_chunk_document", new_callable=AsyncMock) as mock_chunk:
            mock_chunk.return_value = sample_chunks

            # Tagger raises error
            agent._tagger.tag_document = AsyncMock(
                side_effect=RuntimeError("LLM unavailable")
            )

            result = await agent.ingest_file(mock_db, test_file)

        # Ingestion should still succeed despite tagging failure
        assert result.status == "completed"
        assert result.tags_applied == 0


# =============================================================================
# Edge Cases
# =============================================================================


class TestIngestionEdgeCases:
    """Edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_empty_directory(
        self,
        agent: IngestionAgent,
        mock_db: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Empty directory returns empty batch result."""
        # Mock the categories query
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_exec_result = MagicMock()
        mock_exec_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_exec_result

        batch_result = await agent.ingest_directory(mock_db, tmp_path)
        assert batch_result.total == 0
        assert batch_result.succeeded == 0

    @pytest.mark.asyncio
    async def test_invalid_directory_raises(
        self,
        agent: IngestionAgent,
        mock_db: AsyncMock,
    ) -> None:
        """Invalid directory path raises ValueError."""
        with pytest.raises(ValueError, match="Not a directory"):
            await agent.ingest_directory(mock_db, "/nonexistent/path")

    def test_discover_files_recursive(
        self,
        agent: IngestionAgent,
        tmp_path: Path,
    ) -> None:
        """Discovers files recursively in subdirectories."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (tmp_path / "root.txt").write_text("root file")
        (subdir / "nested.txt").write_text("nested file")
        (subdir / "binary.bin").write_text("not supported")

        files = agent._discover_files(tmp_path, recursive=True)
        txt_files = [f for f in files if f.suffix == ".txt"]
        assert len(txt_files) == 2

    def test_discover_files_non_recursive(
        self,
        agent: IngestionAgent,
        tmp_path: Path,
    ) -> None:
        """Non-recursive scan only finds files in root directory."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (tmp_path / "root.txt").write_text("root file")
        (subdir / "nested.txt").write_text("nested file")

        files = agent._discover_files(tmp_path, recursive=False)
        txt_files = [f for f in files if f.suffix == ".txt"]
        assert len(txt_files) == 1

    @pytest.mark.asyncio
    async def test_agent_without_tagger(
        self,
        mock_parser: MagicMock,
        mock_embedder: MagicMock,
        mock_vector_store: MagicMock,
        mock_db: AsyncMock,
        tmp_path: Path,
        sample_chunks: List[Chunk],
    ) -> None:
        """Agent works without a tagger (auto_tag has no effect)."""
        agent = IngestionAgent(
            parser=mock_parser,
            embedder=mock_embedder,
            vector_store=mock_vector_store,
            tagger=None,
        )

        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello world")

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch.object(agent, "_chunk_document", new_callable=AsyncMock) as mock_chunk:
            mock_chunk.return_value = sample_chunks
            result = await agent.ingest_file(mock_db, test_file, auto_tag=True)

        assert result.status == "completed"
        assert result.tags_applied == 0


# =============================================================================
# IngestionResult Model Tests
# =============================================================================


class TestIngestionResultModels:
    """Data model validation."""

    def test_ingestion_result_defaults(self) -> None:
        result = IngestionResult(file_path="/test/file.pdf")
        assert result.status == "completed"
        assert result.chunks_created == 0
        assert result.vectors_stored == 0
        assert result.tags_applied == 0
        assert result.error_message is None

    def test_batch_result_defaults(self) -> None:
        result = BatchIngestionResult()
        assert result.total == 0
        assert result.succeeded == 0
        assert result.skipped == 0
        assert result.failed == 0
        assert result.results == []

    def test_batch_result_with_results(self) -> None:
        results = [
            IngestionResult(file_path="/a.pdf", status="completed"),
            IngestionResult(file_path="/b.pdf", status="skipped"),
            IngestionResult(file_path="/c.pdf", status="failed", error_message="err"),
        ]
        batch = BatchIngestionResult(
            total=3, succeeded=1, skipped=1, failed=1, results=results,
        )
        assert len(batch.results) == 3


# =============================================================================
# Phase 8 — Strategy loader / domain switch
# =============================================================================


class TestDomainSwitch:
    """``settings.security.domain`` drives chunker selection in ingestion."""

    @pytest.mark.parametrize("domain", ["general", "security"])
    def test_create_chunker_respects_domain(
        self,
        mock_parser: MagicMock,
        mock_embedder: MagicMock,
        mock_vector_store: MagicMock,
        mock_tagger: MagicMock,
        domain: str,
    ) -> None:
        from types import SimpleNamespace

        from grimoire.strategies.security.chunker import SecurityChunker

        settings = SimpleNamespace(security=SimpleNamespace(domain=domain))
        agent = IngestionAgent(
            parser=mock_parser,
            embedder=mock_embedder,
            vector_store=mock_vector_store,
            tagger=mock_tagger,
            settings=settings,
        )

        chunker = agent._create_chunker(ChunkingStrategy.RECURSIVE)
        if domain == "security":
            assert isinstance(chunker, SecurityChunker)
        else:
            assert not isinstance(chunker, SecurityChunker)

    def test_no_settings_keeps_legacy_chunker_path(
        self,
        mock_parser: MagicMock,
        mock_embedder: MagicMock,
        mock_vector_store: MagicMock,
        mock_tagger: MagicMock,
    ) -> None:
        """Backward compat: an agent constructed without ``settings`` keeps
        the pre-Phase-8 per-extension chunker logic."""
        from grimoire.strategies.security.chunker import SecurityChunker

        agent = IngestionAgent(
            parser=mock_parser,
            embedder=mock_embedder,
            vector_store=mock_vector_store,
            tagger=mock_tagger,
        )
        chunker = agent._create_chunker(ChunkingStrategy.RECURSIVE)
        assert not isinstance(chunker, SecurityChunker)
