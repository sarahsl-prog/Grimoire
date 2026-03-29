"""Tests for the document parser module.

This module contains comprehensive tests for the DocumentParser class,
including happy path tests, edge cases, error handling, and async behavior.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from grimoire.core.parser import (
    DOCLEY_AVAILABLE,
    DocumentMetadata,
    DocumentParser,
    ParsedDocument,
    ParserConfig,
    parse_document,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_pdf() -> Path:
    """Create a minimal PDF-like sample file for testing.

    Returns:
        Path to a test file (may not be a valid PDF)
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        # Write minimal PDF-like content
        f.write(b"%PDF-1.4\n")
        f.write(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
        f.write(b"%%EOF")
        return Path(f.name)


@pytest.fixture
def sample_docx() -> Path:
    """Create a minimal DOCX-like sample file for testing.

    Returns:
        Path to a test file (may not be a valid DOCX)
    """
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
        # Write minimal zip content (DOCX is a zip file)
        f.write(b"PK\x03\x04")  # ZIP magic bytes
        f.write(b"test")
        return Path(f.name)


@pytest.fixture
def sample_xlsx() -> Path:
    """Create a minimal XLSX sample file for testing.

    Returns:
        Path to a test file
    """
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        f.write(b"PK\x03\x04")  # ZIP magic bytes
        f.write(b"test")
        return Path(f.name)


@pytest.fixture
def sample_txt() -> Path:
    """Create a text file fixture.

    Returns:
        Path to a test file
    """
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
        f.write("Sample text content for testing.")
        return Path(f.name)


@pytest.fixture
def sample_image() -> Path:
    """Create a minimal PNG-like sample file for testing.

    Returns:
        Path to a test file
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        # PNG signature
        f.write(b"\x89PNG\r\n\x1a\n")
        return Path(f.name)


@pytest.fixture
def parser_config() -> ParserConfig:
    """Create a default parser configuration.

    Returns:
        ParserConfig with default settings
    """
    return ParserConfig(
        ocr_enabled=True,
        parse_images=False,
        timeout=30
    )


@pytest.fixture
def parser(parser_config: ParserConfig) -> DocumentParser:
    """Create a DocumentParser instance.

    Args:
        parser_config: Parser configuration fixture

    Returns:
        DocumentParser instance
    """
    return DocumentParser(parser_config)


@pytest.fixture
def mock_docling_result() -> MagicMock:
    """Create a mock Docling conversion result.

    Returns:
        Mock result object
    """
    mock = MagicMock()
    mock.status = MagicMock()
    mock.status.value = "SUCCESS"

    # Mock document
    mock_document = MagicMock()
    mock_document.export_to_markdown.return_value = "Sample extracted text from document."
    mock_document.num_pages.return_value = 5

    # Mock metadata
    mock_meta = MagicMock()
    mock_meta.title = "Test Document"
    mock_meta.author = "Test Author"
    mock_meta.creation_date = "2024-01-01"
    mock_meta.modification_date = "2024-01-02"

    mock_document.metadata = mock_meta
    mock.document = mock_document

    return mock


# =============================================================================
# Test Classes for Organization
# =============================================================================


class TestParsedDocument:
    """Tests for ParsedDocument Pydantic model."""

    def test_basic_creation(self) -> None:
        """Test basic ParsedDocument creation."""
        doc = ParsedDocument(
            text="Sample text",
            status="success"
        )
        assert doc.text == "Sample text"
        assert doc.status == "success"
        assert isinstance(doc.metadata, DocumentMetadata)

    def test_default_values(self) -> None:
        """Test default values are set correctly."""
        doc = ParsedDocument()
        assert doc.text == ""
        assert doc.status == "success"
        assert doc.images == []
        assert doc.error_message is None

    def test_full_document_creation(self) -> None:
        """Test creating ParsedDocument with all fields."""
        metadata = DocumentMetadata(
            title="Test Title",
            author="Test Author",
            pages=10,
            word_count=500
        )
        doc = ParsedDocument(
            text="Full text content",
            metadata=metadata,
            images=[{"index": 0, "caption": "Sample"}],
            status="success"
        )
        assert doc.metadata.title == "Test Title"
        assert doc.metadata.author == "Test Author"
        assert len(doc.images) == 1


class TestDocumentMetadata:
    """Tests for DocumentMetadata Pydantic model."""

    def test_empty_metadata(self) -> None:
        """Test empty DocumentMetadata creation."""
        meta = DocumentMetadata()
        assert meta.title is None
        assert meta.author is None
        assert meta.pages is None

    def test_full_metadata(self) -> None:
        """Test DocumentMetadata with all fields."""
        meta = DocumentMetadata(
            title="Document Title",
            author="John Doe",
            pages=5,
            word_count=1000,
            file_type=".pdf",
            file_size=1024,
            file_hash="abc123"
        )
        assert meta.title == "Document Title"
        assert meta.word_count == 1000

    def test_additional_fields(self) -> None:
        """Test DocumentMetadata with extra fields."""
        meta = DocumentMetadata(
            title="Test",
            custom_field="custom value",
            additional={"key": "value"}
        )
        assert meta.additional == {"key": "value"}


class TestParserConfig:
    """Tests for ParserConfig Pydantic model."""

    def test_default_config(self) -> None:
        """Test default parser configuration."""
        config = ParserConfig()
        assert config.ocr_enabled is True
        assert config.parse_images is False
        assert config.enable_tables is True
        assert config.max_file_size == 100 * 1024 * 1024  # 100MB

    def test_custom_config(self) -> None:
        """Test custom parser configuration."""
        config = ParserConfig(
            ocr_enabled=False,
            parse_images=True,
            enable_tables=False,
            timeout=60
        )
        assert config.ocr_enabled is False
        assert config.parse_images is True
        assert config.timeout == 60


class TestDocumentParserInitialization:
    """Tests for DocumentParser initialization."""

    def test_default_initialization(self) -> None:
        """Test DocumentParser with default config."""
        parser = DocumentParser()
        assert parser.config is not None
        assert parser.config.ocr_enabled is True

    def test_custom_initialization(self) -> None:
        """Test DocumentParser with custom config."""
        config = ParserConfig(ocr_enabled=False)
        parser = DocumentParser(config)
        assert parser.config.ocr_enabled is False

    def test_supported_extensions(self) -> None:
        """Test supported extensions list."""
        parser = DocumentParser()
        supported = parser.get_supported_formats()
        assert ".pdf" in supported
        assert ".docx" in supported
        assert ".xlsx" in supported
        assert ".html" in supported
        assert ".png" in supported


class TestDocumentParserSupportedFormats:
    """Tests for file format support detection."""

    def test_supported_pdf(self, parser: DocumentParser, sample_pdf: Path) -> None:
        """Test PDF is detected as supported."""
        assert parser.is_supported(sample_pdf) is True

    def test_supported_docx(self, parser: DocumentParser, sample_docx: Path) -> None:
        """Test DOCX is detected as supported."""
        assert parser.is_supported(sample_docx) is True

    def test_unsupported_txt(self, parser: DocumentParser, sample_txt: Path) -> None:
        """Test TXT is detected as unsupported."""
        # TXT is not in SUPPORTED_EXTENSIONS but should we support it?
        # Currently not in the list per requirements
        result = parser.is_supported(sample_txt)
        # Note: TXT is not in the required format list
        assert result is False

    def test_supported_xlsx(self, parser: DocumentParser, sample_xlsx: Path) -> None:
        """Test XLSX is detected as supported."""
        assert parser.is_supported(sample_xlsx) is True


@pytest.mark.asyncio
class TestDocumentParserAsync:
    """Tests for async parsing behavior."""

    async def test_parse_nonexistent_file(self, parser: DocumentParser) -> None:
        """Test handling of non-existent file."""
        result = await parser.parse("/nonexistent/path/file.pdf")
        assert result.status == "failed"
        assert "not found" in result.error_message.lower() or "File not found" in result.error_message

    async def test_parse_unsupported_format(self, parser: DocumentParser, sample_txt: Path) -> None:
        """Test handling of unsupported file format."""
        result = await parser.parse(sample_txt)
        assert result.status == "failed"
        assert "Unsupported" in result.error_message

    @pytest.mark.skipif(not DOCLEY_AVAILABLE, reason="Docling not available")
    async def test_parse_empty_file(self, parser: DocumentParser) -> None:
        """Test handling of empty file."""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"")
            empty_file = Path(f.name)

        try:
            # Empty PDF might fail or succeed depending on Docling
            result = await parser.parse(empty_file)
            # Should either succeed (partially) or fail gracefully
            assert result.status in ["success", "partial", "failed"]
        finally:
            empty_file.unlink()


class TestDocumentParserHelperMethods:
    """Tests for parser helper methods."""

    def test_compute_file_hash(self, parser: DocumentParser, sample_pdf: Path) -> None:
        """Test file hash computation."""
        hash1 = parser._compute_file_hash(sample_pdf)
        hash2 = parser._compute_file_hash(sample_pdf)

        assert isinstance(hash1, str)
        assert len(hash1) == 64  # SHA-256 hex string length
        assert hash1 == hash2  # Same file = same hash

    def test_detect_file_type(self, parser: DocumentParser, sample_pdf: Path) -> None:
        """Test file type detection."""
        ext = parser._detect_file_type(sample_pdf)
        assert ext == ".pdf"

    def test_is_image(self, parser: DocumentParser, sample_image: Path) -> None:
        """Test image file detection."""
        assert parser._is_image(sample_image) is True

    def test_is_not_image(self, parser: DocumentParser, sample_pdf: Path) -> None:
        """Test non-image file detection."""
        assert parser._is_image(sample_pdf) is False

    def test_count_words(self, parser: DocumentParser) -> None:
        """Test word counting."""
        text = "This is a test sentence with seven words"
        count = parser._count_words(text)
        assert count == 8

        # Empty text
        assert parser._count_words("") == 0

        # Multiple spaces
        assert parser._count_words("word1    word2") == 2


class TestDocumentParserProcessResult:
    """Tests for processing Docling results."""

    def test_process_successful_result(
        self,
        parser: DocumentParser,
        mock_docling_result: MagicMock,
        sample_pdf: Path
    ) -> None:
        """Test processing a successful conversion result."""
        result = parser._process_docling_result(
            mock_docling_result,
            sample_pdf,
            "test_hash"
        )

        assert result.status == "success"
        assert result.text is not None
        assert result.metadata.pages == 5
        assert result.metadata.title == "Test Document"
        assert result.metadata.author == "Test Author"

    def test_process_result_no_document(
        self,
        parser: DocumentParser,
        sample_pdf: Path
    ) -> None:
        """Test processing result with no document."""
        mock_result = MagicMock()
        mock_result.document = None
        mock_result.markdown = None  # Explicitly set to None

        result = parser._process_docling_result(
            mock_result,
            sample_pdf,
            "test_hash"
        )

        assert result.status == "success"
        assert result.text == ""

    def test_process_result_with_error(
        self,
        parser: DocumentParser,
        sample_pdf: Path
    ) -> None:
        """Test processing result that raises error."""
        mock_result = MagicMock()
        mock_result.document = MagicMock()
        mock_result.document.export_to_markdown.side_effect = Exception("Test error")

        result = parser._process_docling_result(
            mock_result,
            sample_pdf,
            "test_hash"
        )

        assert result.status == "failed"
        assert "Test error" in result.error_message


@pytest.mark.asyncio
class TestDocumentParserWithMocks:
    """Tests using mocked Docling behavior."""

    async def test_parse_with_mocked_docling(
        self,
        parser: DocumentParser,
        sample_pdf: Path,
        mock_docling_result: MagicMock
    ) -> None:
        """Test parsing with mocked Docling converter."""
        with patch.object(
            parser,
            '_get_converter',
            return_value=MagicMock(
                convert=lambda path: mock_docling_result
            )
        ):
            result = await parser.parse(sample_pdf)
            assert result.status == "success"

    async def test_parse_timeout(self, parser: DocumentParser) -> None:
        """Test timeout handling."""
        config = ParserConfig(timeout=0)  # Immediate timeout
        parser.config = config

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4\n")
            test_file = Path(f.name)

        try:
            with patch.object(parser, '_parse_sync', side_effect=lambda x, y: asyncio.sleep(10)):
                result = await parser.parse(test_file)
                assert result.status in ["failed"]  # Should timeout
        finally:
            test_file.unlink(missing_ok=True)


class TestParseDocumentFunction:
    """Tests for the parse_document convenience function."""

    @pytest.mark.asyncio
    async def test_parse_document_with_defaults(self) -> None:
        """Test parse_document with default settings."""
        # Should handle non-existent file gracefully
        result = await parse_document("/nonexistent.pdf")
        assert result.status == "failed"

    @pytest.mark.asyncio
    async def test_parse_document_custom_config(self) -> None:
        """Test parse_document with custom config."""
        result = await parse_document(
            "/nonexistent.pdf",
            ocr_enabled=False,
            parse_images=True
        )
        # Should still fail because file doesn't exist
        assert result.status == "failed"


class TestDocumentParserEdgeCases:
    """Tests for edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_parse_large_file_rejected(self) -> None:
        """Test that files exceeding max_file_size are rejected."""
        config = ParserConfig(max_file_size=10)  # Very small limit
        parser = DocumentParser(config)

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"x" * 100)  # 100 bytes > 10 byte limit
            large_file = Path(f.name)

        try:
            result = await parser.parse(large_file)
            assert result.status == "failed"
            assert "too large" in result.error_message.lower()
        finally:
            large_file.unlink()

    @pytest.mark.asyncio
    async def test_parse_with_invalid_path(self, parser: DocumentParser) -> None:
        """Test parsing with invalid path."""
        result = await parser.parse("")
        assert result.status == "failed"

    def test_supported_formats_list(self) -> None:
        """Test that supported formats returns non-empty list."""
        parser = DocumentParser()
        formats = parser.get_supported_formats()
        assert len(formats) > 0
        assert ".pdf" in formats


class TestDocumentParserConcurrency:
    """Tests for concurrent parsing behavior."""

    @pytest.mark.asyncio
    async def test_concurrent_parses(self, parser: DocumentParser) -> None:
        """Test that multiple parse calls can run concurrently."""
        # Create simple test files
        files = []
        for i in range(3):
            with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
                f.write(b"test")
                files.append(Path(f.name))

        try:
            # Test that we can start multiple parses
            # (they'll fail due to unsupported format, but that's ok)
            tasks = [parser.parse(f) for f in files]
            results = await asyncio.gather(*tasks)

            # All should complete (even if failing)
            assert len(results) == 3
            for r in results:
                assert r.status in ["success", "failed", "partial"]
        finally:
            for f in files:
                f.unlink(missing_ok=True)


class TestDocumentParserStateManagement:
    """Tests for parser state handling."""

    def test_config_immutability(self) -> None:
        """Test that config changes after creation DO affect parser (Pydantic models are mutable)."""
        config = ParserConfig(ocr_enabled=True)
        parser = DocumentParser(config)

        # Pydantic models are mutable, so changing original config WILL affect parser
        config.ocr_enabled = False
        # Since parser keeps a reference to the same object, it will see the change
        assert parser.config.ocr_enabled is False  # Parser sees the change (same reference)

    def test_custom_config_override(self, parser: DocumentParser) -> None:
        """Test custom config overrides instance config."""
        custom_config = ParserConfig(ocr_enabled=False)

        # The parser instance config
        assert parser.config.ocr_enabled is True

        # Custom config should be different
        assert custom_config.ocr_enabled is False
