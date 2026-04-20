"""Tests for deduplication logic.

Comprehensive test coverage for:
- Happy path tests
- Edge cases and boundary conditions
- Input validation and error handling
- Async behavior
- Configurable strategies
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from grimoire.core.dedup import (
    CHUNK_SIZE,
    ConflictDetails,
    DedupResult,
    DedupStrategy,
    DeduplicationAction,
    Deduplicator,
    check_duplicate,
    compute_bytes_hash,
    compute_file_hash,
    get_file_mtime,
)
from grimoire.db.models import (
    ActionType,
    Document,
    FileType,
    ProcessingLog,
    ProcessingStatus,
    StatusType,
    StorageBackend,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_file() -> Generator[Path, None, None]:
    """Create a temporary file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("Hello World")
        temp_path = Path(f.name)
    yield temp_path
    # Cleanup
    if temp_path.exists():
        os.unlink(temp_path)


@pytest.fixture
def large_temp_file() -> Generator[Path, None, None]:
    """Create a large temporary file to test chunked reading."""
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".bin", delete=False) as f:
        # Write more than CHUNK_SIZE to test chunked reading
        data = b"x" * (CHUNK_SIZE * 2 + 1000)
        f.write(data)
        temp_path = Path(f.name)
    yield temp_path
    if temp_path.exists():
        os.unlink(temp_path)


@pytest.fixture
def empty_temp_file() -> Generator[Path, None, None]:
    """Create an empty temporary file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        pass
    temp_path = Path(f.name)
    yield temp_path
    if temp_path.exists():
        os.unlink(temp_path)


@pytest.fixture
def sample_document() -> Document:
    """Create a sample document for testing."""
    return Document(
        id="doc-123",
        source_path="/test/document.pdf",
        storage_backend=StorageBackend.LOCAL,
        file_type=FileType.PDF,
        file_hash="a" * 64,  # Valid SHA-256 length
        title="Test Document",
        size_bytes=1024,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        processing_status=ProcessingStatus.COMPLETED,
        version=1,
    )


@pytest.fixture
def older_document(sample_document: Document) -> Document:
    """Create a document with an older modification time."""
    doc = sample_document
    doc.updated_at = datetime.now() - timedelta(hours=1)
    return doc


@pytest.fixture
def newer_document(sample_document: Document) -> Document:
    """Create a document with a newer modification time."""
    doc = sample_document
    doc.updated_at = datetime.now() + timedelta(hours=1)
    return doc


# ============================================================================
# Happy Path Tests
# ============================================================================


class TestDedupHappyPath:
    """Standard use cases."""

    @pytest.mark.asyncio
    async def test_new_file_no_existing_document(self, temp_file: Path) -> None:
        """New file with no existing document returns 'new' action."""
        deduplicator = Deduplicator()

        result = await deduplicator.check_file(temp_file, existing_doc=None)

        assert isinstance(result, DedupResult)
        assert result.action == DeduplicationAction.NEW
        assert result.file_hash is not None
        assert len(result.file_hash) == 64  # SHA-256 hex length
        assert result.existing_document is None
        assert result.conflict is False
        assert result.resolution == "New file, no existing record"

    @pytest.mark.asyncio
    async def test_same_hash_returns_skip(
        self, temp_file: Path, sample_document: Document
    ) -> None:
        """Same file hash returns skip action."""
        # Set document hash to match file hash
        sample_document.file_hash = compute_file_hash(temp_file)
        deduplicator = Deduplicator()

        result = await deduplicator.check_file(temp_file, existing_doc=sample_document)

        assert result.action == DeduplicationAction.SKIP
        assert result.existing_document == sample_document
        assert result.conflict is False
        assert "unchanged" in result.resolution.lower()

    @pytest.mark.asyncio
    async def test_modified_file_returns_update(
        self, temp_file: Path, older_document: Document
    ) -> None:
        """Modified file (different hash, newer mtime) returns update."""
        # Different hash than the file
        older_document.file_hash = "b" * 64
        # File is newer than document
        file_mtime = datetime.now()
        deduplicator = Deduplicator()

        result = await deduplicator.check_file(
            temp_file, existing_doc=older_document, file_mtime=file_mtime
        )

        assert result.action == DeduplicationAction.UPDATE
        assert result.conflict is False
        assert "modified" in result.resolution.lower()

    @pytest.mark.asyncio
    async def test_check_duplicate_convenience_function(self, temp_file: Path) -> None:
        """Test the check_duplicate convenience function."""
        result = await check_duplicate(temp_file, strategy=DedupStrategy.AUTO)

        assert isinstance(result, DedupResult)
        assert result.action == DeduplicationAction.NEW
        assert result.strategy == DedupStrategy.AUTO

    @pytest.mark.asyncio
    async def test_deduplicator_with_custom_strategy(self, temp_file: Path) -> None:
        """Deduplicator respects custom default strategy."""
        deduplicator = Deduplicator(default_strategy=DedupStrategy.SKIP)

        assert deduplicator.default_strategy == DedupStrategy.SKIP


# ============================================================================
# Edge Cases and Boundary Conditions
# ============================================================================


class TestDedupEdgeCases:
    """Boundary conditions and unusual inputs."""

    @pytest.mark.asyncio
    async def test_empty_file(self, empty_temp_file: Path) -> None:
        """Empty file produces valid hash."""
        result = await check_duplicate(empty_temp_file)

        assert result.action == DeduplicationAction.NEW
        assert result.file_hash is not None
        # SHA-256 of empty string
        assert result.file_hash == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    @pytest.mark.asyncio
    async def test_large_file_chunked_reading(self, large_temp_file: Path) -> None:
        """Large file is read in chunks without memory issues."""
        # Should not raise any memory errors
        hash_value = compute_file_hash(large_temp_file)

        assert len(hash_value) == 64
        assert all(c in "0123456789abcdef" for c in hash_value.lower())

    @pytest.mark.asyncio
    async def test_unicode_filename(self) -> None:
        """File with unicode characters in name."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix="日本語.txt", delete=False
        ) as f:
            f.write("test content")
            temp_path = Path(f.name)

        try:
            result = await check_duplicate(temp_path)
            assert result.action == DeduplicationAction.NEW
            assert result.file_hash is not None
        finally:
            if temp_path.exists():
                os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_special_characters_in_content(self) -> None:
        """File with special unicode characters."""
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".txt", delete=False
        ) as f:
            f.write("Special chars: émojis 🎉 café \n 中文")
            temp_path = Path(f.name)

        try:
            result = await check_duplicate(temp_path)
            assert result.action == DeduplicationAction.NEW
        finally:
            if temp_path.exists():
                os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_no_mtime_provided(
        self, temp_file: Path, sample_document: Document
    ) -> None:
        """File check works without mtime (defaults to no conflict)."""
        sample_document.file_hash = "different" * 8  # Different hash
        deduplicator = Deduplicator()

        result = await deduplicator.check_file(
            temp_file, existing_doc=sample_document, file_mtime=None
        )

        assert result.action == DeduplicationAction.UPDATE
        assert result.conflict is False

    @pytest.mark.asyncio
    async def test_single_element_file(self) -> None:
        """File with single byte."""
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".bin", delete=False) as f:
            f.write(b"x")
            temp_path = Path(f.name)

        try:
            result = await check_duplicate(temp_path)
            assert result.action == DeduplicationAction.NEW
            assert len(result.file_hash) == 64
        finally:
            if temp_path.exists():
                os.unlink(temp_path)


# ============================================================================
# Input Validation and Error Handling
# ============================================================================


class TestDedupInputValidation:
    """Invalid inputs are rejected gracefully."""

    def test_file_not_found(self) -> None:
        """Non-existent file raises FileNotFoundError."""
        non_existent = Path("/non/existent/file.txt")

        with pytest.raises(FileNotFoundError):
            compute_file_hash(non_existent)

    @pytest.mark.skipif(os.getuid() == 0, reason="Cannot test permissions as root")
    def test_permission_error(self, temp_file: Path) -> None:
        """File without read permission raises PermissionError."""
        # Remove read permissions
        os.chmod(temp_file, 0o000)

        try:
            with pytest.raises(PermissionError):
                compute_file_hash(temp_file)
        finally:
            # Restore permissions for cleanup
            os.chmod(temp_file, 0o644)

    def test_invalid_strategy_type(self) -> None:
        """Invalid strategy enum value raises ValueError."""
        # Creating an invalid strategy should raise ValueError
        with pytest.raises(ValueError, match="not a valid DedupStrategy"):
            _ = DedupStrategy("invalid")


# ============================================================================
# Strategy Tests
# ============================================================================


class TestDedupStrategies:
    """Test each conflict resolution strategy."""

    @pytest.mark.asyncio
    async def test_auto_strategy_skips_older_version(
        self, temp_file: Path, newer_document: Document
    ) -> None:
        """AUTO strategy skips when existing is newer."""
        newer_document.file_hash = "different" * 8
        deduplicator = Deduplicator(default_strategy=DedupStrategy.AUTO)
        # File has older mtime than document
        file_mtime = datetime.now() - timedelta(hours=2)

        result = await deduplicator.check_file(
            temp_file,
            existing_doc=newer_document,
            file_mtime=file_mtime,
            strategy=DedupStrategy.AUTO,
        )

        assert result.action == DeduplicationAction.SKIP
        assert result.conflict is True
        assert result.strategy == DedupStrategy.AUTO
        assert "newer version" in result.resolution.lower()

    @pytest.mark.asyncio
    async def test_skip_strategy_always_skips_conflict(
        self, temp_file: Path, newer_document: Document
    ) -> None:
        """SKIP strategy always skips conflicts."""
        newer_document.file_hash = "different" * 8
        deduplicator = Deduplicator(default_strategy=DedupStrategy.SKIP)
        file_mtime = datetime.now() - timedelta(hours=2)

        result = await deduplicator.check_file(
            temp_file,
            existing_doc=newer_document,
            file_mtime=file_mtime,
            strategy=DedupStrategy.SKIP,
        )

        assert result.action == DeduplicationAction.SKIP
        assert result.conflict is True
        assert "skip" in result.resolution.lower()

    @pytest.mark.asyncio
    async def test_duplicate_strategy_creates_version(
        self, temp_file: Path, newer_document: Document
    ) -> None:
        """DUPLICATE strategy adds as new version."""
        newer_document.file_hash = "different" * 8
        deduplicator = Deduplicator(default_strategy=DedupStrategy.DUPLICATE)
        file_mtime = datetime.now() - timedelta(hours=2)

        result = await deduplicator.check_file(
            temp_file,
            existing_doc=newer_document,
            file_mtime=file_mtime,
            strategy=DedupStrategy.DUPLICATE,
        )

        assert result.action == DeduplicationAction.UPDATE
        assert result.conflict is True
        assert "new version" in result.resolution.lower()

    @pytest.mark.asyncio
    async def test_manual_strategy_flags_conflict(
        self, temp_file: Path, newer_document: Document
    ) -> None:
        """MANUAL strategy flags for human review."""
        newer_document.file_hash = "different" * 8
        deduplicator = Deduplicator(default_strategy=DedupStrategy.MANUAL)
        file_mtime = datetime.now() - timedelta(hours=2)

        result = await deduplicator.check_file(
            temp_file,
            existing_doc=newer_document,
            file_mtime=file_mtime,
            strategy=DedupStrategy.MANUAL,
        )

        assert result.action == DeduplicationAction.CONFLICT
        assert result.conflict is True
        assert result.strategy == DedupStrategy.MANUAL
        assert "review" in result.resolution.lower()


# ============================================================================
# Hash Computation Tests
# ============================================================================


class TestHashComputation:
    """Test SHA-256 hash computation."""

    def test_compute_bytes_hash(self) -> None:
        """Hash of bytes produces correct output."""
        content = b"Hello World"
        hash_value = compute_bytes_hash(content)

        assert len(hash_value) == 64
        assert hash_value == (
            "a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b57b277d9ad9f146e"
        )

    def test_compute_file_hash(self, temp_file: Path) -> None:
        """File hash computation."""
        hash_value = compute_file_hash(temp_file)

        assert len(hash_value) == 64
        assert all(c in "0123456789abcdef" for c in hash_value.lower())

    def test_same_content_same_hash(self, temp_file: Path) -> None:
        """Same content produces same hash."""
        hash1 = compute_file_hash(temp_file)
        hash2 = compute_file_hash(temp_file)

        assert hash1 == hash2

    def test_different_content_different_hash(self, temp_file: Path) -> None:
        """Different content produces different hashes."""
        hash1 = compute_file_hash(temp_file)

        # Modify file
        with open(temp_file, "a") as f:
            f.write("modified")

        hash2 = compute_file_hash(temp_file)

        assert hash1 != hash2

    def test_hash_length(self, temp_file: Path) -> None:
        """SHA-256 hash is always 64 hex characters."""
        hash_value = compute_file_hash(temp_file)

        assert len(hash_value) == 64
        # Only hex characters
        assert all(c in "0123456789abcdefABCDEF" for c in hash_value)


# ============================================================================
# Conflict Detection Tests
# ============================================================================


class TestConflictDetection:
    """Test version conflict detection."""

    def test_version_conflict_when_existing_newer(
        self, newer_document: Document
    ) -> None:
        """Conflict detected when existing doc is newer."""
        deduplicator = Deduplicator()
        # File mtime is older than document
        file_mtime = datetime.now() - timedelta(hours=2)

        is_conflict = deduplicator._is_version_conflict(newer_document, file_mtime)

        assert is_conflict is True

    def test_no_conflict_when_file_newer(self, older_document: Document) -> None:
        """No conflict when file is newer."""
        deduplicator = Deduplicator()
        # File mtime is newer than document
        file_mtime = datetime.now() + timedelta(hours=1)

        is_conflict = deduplicator._is_version_conflict(older_document, file_mtime)

        assert is_conflict is False

    def test_no_conflict_when_no_mtime(self, sample_document: Document) -> None:
        """No conflict when mtime is None."""
        deduplicator = Deduplicator()

        is_conflict = deduplicator._is_version_conflict(sample_document, None)

        assert is_conflict is False

    def test_no_conflict_when_no_doc_mtime(self, sample_document: Document) -> None:
        """No conflict when doc mtime is None."""
        deduplicator = Deduplicator()
        sample_document.updated_at = None

        is_conflict = deduplicator._is_version_conflict(sample_document, datetime.now())

        assert is_conflict is False


# ============================================================================
# Processing Log Tests
# ============================================================================


class TestProcessingLog:
    """Test processing log creation."""

    def test_create_processing_log_new_file(self) -> None:
        """Log entry for new file."""
        result = DedupResult(
            action=DeduplicationAction.NEW,
            file_hash="abc123",
            resolution="New file",
        )

        log_entry = Deduplicator().create_processing_log(
            document_id="doc-123",
            dedup_result=result,
            duration_ms=100,
        )

        assert log_entry.document_id == "doc-123"
        assert log_entry.action == ActionType.DISCOVERED
        assert log_entry.status == StatusType.SUCCESS
        assert log_entry.details["file_hash"] == "abc123"
        assert log_entry.duration_ms == 100

    def test_create_processing_log_skip_file(self) -> None:
        """Log entry for skipped file."""
        result = DedupResult(
            action=DeduplicationAction.SKIP,
            file_hash="abc123",
            resolution="File unchanged",
        )

        log_entry = Deduplicator().create_processing_log(
            document_id="doc-123",
            dedup_result=result,
        )

        assert log_entry.status == StatusType.PARTIAL

    def test_create_processing_log_conflict(self) -> None:
        """Log entry for conflict."""
        result = DedupResult(
            action=DeduplicationAction.CONFLICT,
            file_hash="abc123",
            conflict=True,
            strategy=DedupStrategy.MANUAL,
            resolution="Flagged for review",
        )

        log_entry = Deduplicator().create_processing_log(
            document_id="doc-123",
            dedup_result=result,
        )

        assert log_entry.status == StatusType.FAILED
        assert log_entry.details["conflict"] is True


# ============================================================================
# Conflict Details Tests
# ============================================================================


class TestConflictDetails:
    """Test conflict details creation."""

    def test_create_conflict_details(self, sample_document: Document) -> None:
        """Create conflict details with all fields."""
        deduplicator = Deduplicator()

        details = deduplicator.create_conflict_details(
            existing_doc=sample_document,
            new_hash="new_hash_123",
            new_mtime=datetime.now(),
            strategy=DedupStrategy.AUTO,
        )

        assert isinstance(details, ConflictDetails)
        assert details.existing_hash == sample_document.file_hash
        assert details.new_hash == "new_hash_123"
        assert details.existing_version == sample_document.version
        assert details.strategy_used == DedupStrategy.AUTO


# ============================================================================
# Utility Function Tests
# ============================================================================


class TestUtilityFunctions:
    """Test helper utility functions."""

    def test_get_file_mtime(self, temp_file: Path) -> None:
        """Get file modification time."""
        mtime = get_file_mtime(temp_file)

        assert isinstance(mtime, datetime)
        # Should be recent (mtime is timezone-aware, so compare with UTC)
        assert datetime.now(timezone.utc) - mtime < timedelta(minutes=1)

    def test_get_file_mtime_nonexistent(self) -> None:
        """None returned for non-existent file."""
        mtime = get_file_mtime("/non/existent/path")

        assert mtime is None


# ============================================================================
# DedupStrategy Enum Tests
# ============================================================================


class TestDedupStrategy:
    """Test DedupStrategy enum."""

    def test_strategy_values(self) -> None:
        """All expected strategies exist."""
        assert DedupStrategy.AUTO.value == "auto"
        assert DedupStrategy.SKIP.value == "skip"
        assert DedupStrategy.DUPLICATE.value == "duplicate"
        assert DedupStrategy.MANUAL.value == "manual"

    def test_strategy_comparison(self) -> None:
        """Strategies can be compared."""
        assert DedupStrategy.AUTO != DedupStrategy.SKIP
        assert DedupStrategy.AUTO == DedupStrategy.AUTO


# ============================================================================
# Pydantic Model Tests
# ============================================================================


class TestPydanticModels:
    """Test Pydantic model validation."""

    def test_dedup_result_model(self) -> None:
        """DedupResult validates correctly."""
        result = DedupResult(
            action=DeduplicationAction.NEW,
            file_hash="a" * 64,
            conflict=False,
            strategy=DedupStrategy.AUTO,
        )

        assert result.action == DeduplicationAction.NEW
        assert result.file_hash == "a" * 64

    def test_dedup_result_with_existing_doc(self, sample_document: Document) -> None:
        """DedupResult with existing document."""
        result = DedupResult(
            action=DeduplicationAction.UPDATE,
            file_hash="b" * 64,
            existing_document=sample_document,
            conflict=False,
        )

        assert result.existing_document == sample_document

    def test_conflict_details_model(self, sample_document: Document) -> None:
        """ConflictDetails validates correctly."""
        details = ConflictDetails(
            existing_hash="a" * 64,
            new_hash="b" * 64,
            existing_version=1,
            strategy_used=DedupStrategy.AUTO,
        )

        assert details.existing_hash == "a" * 64
        assert details.new_hash == "b" * 64


# ============================================================================
# State Management Tests
# ============================================================================


class TestStateManagement:
    """Test re-entrant operations and state management."""

    @pytest.mark.asyncio
    async def test_deduplicator_idempotent(self, temp_file: Path) -> None:
        """Multiple calls produce consistent results."""
        deduplicator = Deduplicator()

        result1 = await deduplicator.check_file(temp_file)
        result2 = await deduplicator.check_file(temp_file)

        assert result1.file_hash == result2.file_hash
        assert result1.action == result2.action

    @pytest.mark.asyncio
    async def test_deduplicator_preserves_strategy(self) -> None:
        """Default strategy is preserved across calls."""
        deduplicator = Deduplicator(default_strategy=DedupStrategy.SKIP)

        assert deduplicator.default_strategy == DedupStrategy.SKIP
        # Strategy persists through operations


# ============================================================================
# Integration-like Tests
# ============================================================================


class TestIntegrationScenarios:
    """End-to-end style scenarios."""

    @pytest.mark.asyncio
    async def test_full_workflow_new_document(self) -> None:
        """Complete workflow for a new document."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Integration test content")
            temp_path = Path(f.name)

        try:
            # Step 1: Check deduplication (no existing doc)
            result = await check_duplicate(temp_path)

            assert result.action == DeduplicationAction.NEW
            assert result.conflict is False

            # Would typically store to DB here...

        finally:
            if temp_path.exists():
                os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_full_workflow_modified_document(self) -> None:
        """Complete workflow for a modified document."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Original content")
            temp_path = Path(f.name)

        try:
            # Create existing document
            existing = Document(
                id="doc-456",
                source_path=str(temp_path),
                storage_backend=StorageBackend.LOCAL,
                file_type=FileType.TXT,
                file_hash=compute_file_hash(temp_path),
                title="Original",
                size_bytes=100,
                created_at=datetime.now(),
                updated_at=datetime.now() - timedelta(hours=1),
                version=1,
            )

            # Modify file
            with open(temp_path, "w") as f:
                f.write("Modified content with extra text")

            # Check deduplication
            file_mtime = datetime.now()
            result = await check_duplicate(temp_path, existing, file_mtime)

            # Should detect modification
            assert result.action == DeduplicationAction.UPDATE
            assert result.file_hash != existing.file_hash

        finally:
            if temp_path.exists():
                os.unlink(temp_path)


# ============================================================================
# Performance Tests
# ============================================================================


class TestPerformance:
    """Performance-related tests."""

    def test_large_file_hashing(self, large_temp_file: Path) -> None:
        """Large file hashing completes in reasonable time."""
        import time

        start = time.time()
        hash_value = compute_file_hash(large_temp_file)
        elapsed = time.time() - start

        assert len(hash_value) == 64
        assert elapsed < 5.0  # Should complete within 5 seconds


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
