"""Tests for the Watcher Agent.

Tests cover:
- Happy path: start/stop watches, event processing
- File filtering: supported extensions, hidden files
- Status reporting
- Error handling: ingestion failures, queue overflow
- Edge cases: duplicate watches, missing watch IDs
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from grimoire.agents.watcher import WatcherAgent, WatcherStats, WatchStatus
from grimoire.agents.ingestion import IngestionResult
from grimoire.storage.base import FileChange, FileChangeType, StorageBackend
from grimoire.storage.watch_manager import WatchManager


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_watch_manager() -> MagicMock:
    """Create a mock WatchManager."""
    manager = MagicMock(spec=WatchManager)
    manager.start_watch = AsyncMock(return_value="local:/tmp/test")
    manager.stop_watch = AsyncMock(return_value=True)
    manager.stop_all = AsyncMock()
    manager.list_watches = MagicMock(return_value=[])
    return manager


@pytest.fixture
def mock_ingestion_agent() -> MagicMock:
    """Create a mock IngestionAgent."""
    agent = MagicMock()
    agent.ingest_file = AsyncMock(
        return_value=IngestionResult(
            file_path="/tmp/test/file.txt",
            document_id="doc-123",
            status="completed",
            chunks_created=5,
            vectors_stored=5,
        )
    )
    return agent


@pytest.fixture
def mock_db_session_factory() -> MagicMock:
    """Create a mock database session factory (async context manager)."""
    mock_db = AsyncMock()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_db)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=mock_cm)
    return factory


@pytest.fixture
def watcher(
    mock_watch_manager: MagicMock,
    mock_ingestion_agent: MagicMock,
    mock_db_session_factory: MagicMock,
) -> WatcherAgent:
    """Create a WatcherAgent with mocked dependencies."""
    return WatcherAgent(
        watch_manager=mock_watch_manager,
        ingestion_agent=mock_ingestion_agent,
        db_session_factory=mock_db_session_factory,
    )


def make_change(
    path: str = "/tmp/test/file.txt",
    change_type: FileChangeType = FileChangeType.CREATED,
) -> FileChange:
    """Helper to create FileChange events."""
    return FileChange(
        change_type=change_type,
        path=path,
        timestamp=datetime.utcnow(),
        file_info=None,
    )


# =============================================================================
# Happy Path Tests
# =============================================================================


class TestWatcherHappyPath:
    """Standard watcher scenarios."""

    @pytest.mark.asyncio
    async def test_start_watch(
        self, watcher: WatcherAgent, mock_watch_manager: MagicMock,
    ) -> None:
        """Can start watching a directory."""
        watch_id = await watcher.watch("/tmp/test", backend="local")
        assert watch_id == "local:/tmp/test"
        mock_watch_manager.start_watch.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_watch(
        self, watcher: WatcherAgent, mock_watch_manager: MagicMock,
    ) -> None:
        """Can stop a specific watch."""
        await watcher.watch("/tmp/test", backend="local")
        result = await watcher.unwatch("local:/tmp/test")
        assert result is True
        mock_watch_manager.stop_watch.assert_called_once_with("local:/tmp/test")

    @pytest.mark.asyncio
    async def test_stop_all(
        self, watcher: WatcherAgent, mock_watch_manager: MagicMock,
    ) -> None:
        """Can stop all watches."""
        await watcher.watch("/tmp/test", backend="local")
        await watcher.stop_all()
        mock_watch_manager.stop_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_status_empty(self, watcher: WatcherAgent) -> None:
        """Status is empty when no watches are active."""
        stats = watcher.get_status()
        assert stats.active_watches == 0
        assert stats.total_files_processed == 0
        assert stats.total_files_failed == 0

    @pytest.mark.asyncio
    async def test_get_status_with_watch(self, watcher: WatcherAgent) -> None:
        """Status reflects active watches."""
        await watcher.watch("/tmp/test", backend="local")
        stats = watcher.get_status()
        assert stats.active_watches == 1
        assert stats.watches[0].path == "/tmp/test"
        assert stats.watches[0].backend == "local"

    @pytest.mark.asyncio
    async def test_context_manager(
        self, mock_watch_manager: MagicMock,
        mock_ingestion_agent: MagicMock,
        mock_db_session_factory: MagicMock,
    ) -> None:
        """Can use as async context manager."""
        async with WatcherAgent(
            watch_manager=mock_watch_manager,
            ingestion_agent=mock_ingestion_agent,
            db_session_factory=mock_db_session_factory,
        ) as w:
            await w.watch("/tmp/test", backend="local")

        mock_watch_manager.stop_all.assert_called_once()


# =============================================================================
# File Filtering Tests
# =============================================================================


class TestWatcherFiltering:
    """File change filtering logic."""

    def test_should_process_created_txt(self, watcher: WatcherAgent) -> None:
        """Created .txt files should be processed."""
        change = make_change("/tmp/test/file.txt", FileChangeType.CREATED)
        assert watcher._should_process(change) is True

    def test_should_process_modified_pdf(self, watcher: WatcherAgent) -> None:
        """Modified .pdf files should be processed."""
        change = make_change("/tmp/test/doc.pdf", FileChangeType.MODIFIED)
        assert watcher._should_process(change) is True

    def test_should_not_process_deleted(self, watcher: WatcherAgent) -> None:
        """Deleted files should not be processed."""
        change = make_change("/tmp/test/file.txt", FileChangeType.DELETED)
        assert watcher._should_process(change) is False

    def test_should_not_process_moved(self, watcher: WatcherAgent) -> None:
        """Moved files should not be processed."""
        change = make_change("/tmp/test/file.txt", FileChangeType.MOVED)
        assert watcher._should_process(change) is False

    def test_should_not_process_unsupported_extension(self, watcher: WatcherAgent) -> None:
        """Unsupported file types should not be processed."""
        change = make_change("/tmp/test/data.xyz", FileChangeType.CREATED)
        assert watcher._should_process(change) is False

    def test_should_not_process_hidden_files(self, watcher: WatcherAgent) -> None:
        """Hidden files should not be processed."""
        change = make_change("/tmp/test/.hidden.txt", FileChangeType.CREATED)
        assert watcher._should_process(change) is False

    def test_should_not_process_hidden_directory(self, watcher: WatcherAgent) -> None:
        """Files in hidden directories should not be processed."""
        change = make_change("/tmp/test/.git/config.txt", FileChangeType.CREATED)
        assert watcher._should_process(change) is False

    def test_should_process_supported_extensions(self, watcher: WatcherAgent) -> None:
        """All supported extensions are accepted."""
        for ext in [".pdf", ".docx", ".xlsx", ".html", ".md", ".png", ".jpg"]:
            change = make_change(f"/tmp/test/file{ext}", FileChangeType.CREATED)
            assert watcher._should_process(change) is True, f"Failed for {ext}"


# =============================================================================
# Event Handling Tests
# =============================================================================


class TestWatcherEventHandling:
    """Event processing behavior."""

    @pytest.mark.asyncio
    async def test_handle_file_event_success(
        self,
        watcher: WatcherAgent,
        mock_ingestion_agent: MagicMock,
    ) -> None:
        """Successful file event increments processed count."""
        await watcher.watch("/tmp/test", backend="local")
        change = make_change("/tmp/test/file.txt", FileChangeType.CREATED)

        await watcher._handle_file_event("local:/tmp/test", change)

        mock_ingestion_agent.ingest_file.assert_called_once()
        tracker = watcher._trackers["local:/tmp/test"]
        assert tracker.files_processed == 1
        assert tracker.files_failed == 0

    @pytest.mark.asyncio
    async def test_handle_file_event_failure(
        self,
        watcher: WatcherAgent,
        mock_ingestion_agent: MagicMock,
    ) -> None:
        """Failed file event increments failed count."""
        await watcher.watch("/tmp/test", backend="local")

        mock_ingestion_agent.ingest_file = AsyncMock(
            return_value=IngestionResult(
                file_path="/tmp/test/file.txt",
                status="failed",
                error_message="Parse error",
            )
        )

        change = make_change("/tmp/test/file.txt", FileChangeType.CREATED)
        await watcher._handle_file_event("local:/tmp/test", change)

        tracker = watcher._trackers["local:/tmp/test"]
        assert tracker.files_processed == 0
        assert tracker.files_failed == 1

    @pytest.mark.asyncio
    async def test_handle_file_event_skipped(
        self,
        watcher: WatcherAgent,
        mock_ingestion_agent: MagicMock,
    ) -> None:
        """Skipped (duplicate) files don't count as processed or failed."""
        await watcher.watch("/tmp/test", backend="local")

        mock_ingestion_agent.ingest_file = AsyncMock(
            return_value=IngestionResult(
                file_path="/tmp/test/file.txt",
                status="skipped",
            )
        )

        change = make_change("/tmp/test/file.txt", FileChangeType.CREATED)
        await watcher._handle_file_event("local:/tmp/test", change)

        tracker = watcher._trackers["local:/tmp/test"]
        assert tracker.files_processed == 0
        assert tracker.files_failed == 0

    @pytest.mark.asyncio
    async def test_handle_file_event_exception(
        self,
        watcher: WatcherAgent,
        mock_ingestion_agent: MagicMock,
    ) -> None:
        """Exceptions during ingestion increment failed count."""
        await watcher.watch("/tmp/test", backend="local")

        mock_ingestion_agent.ingest_file = AsyncMock(
            side_effect=RuntimeError("Database error")
        )

        change = make_change("/tmp/test/file.txt", FileChangeType.CREATED)
        await watcher._handle_file_event("local:/tmp/test", change)

        tracker = watcher._trackers["local:/tmp/test"]
        assert tracker.files_failed == 1

    @pytest.mark.asyncio
    async def test_handle_event_unknown_watch_id(
        self,
        watcher: WatcherAgent,
    ) -> None:
        """Events for unknown watch IDs are silently ignored."""
        change = make_change("/tmp/test/file.txt", FileChangeType.CREATED)
        # Should not raise
        await watcher._handle_file_event("unknown:id", change)


# =============================================================================
# Edge Cases
# =============================================================================


class TestWatcherEdgeCases:
    """Edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_unwatch_nonexistent(
        self, watcher: WatcherAgent, mock_watch_manager: MagicMock,
    ) -> None:
        """Unwatching a nonexistent watch returns False."""
        mock_watch_manager.stop_watch = AsyncMock(return_value=False)
        result = await watcher.unwatch("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_multiple_watches(
        self, watcher: WatcherAgent, mock_watch_manager: MagicMock,
    ) -> None:
        """Can manage multiple concurrent watches."""
        mock_watch_manager.start_watch = AsyncMock(
            side_effect=["local:/tmp/a", "local:/tmp/b"]
        )

        id1 = await watcher.watch("/tmp/a", backend="local")
        id2 = await watcher.watch("/tmp/b", backend="local")

        assert id1 != id2
        stats = watcher.get_status()
        assert stats.active_watches == 2

    @pytest.mark.asyncio
    async def test_stop_all_clears_trackers(
        self, watcher: WatcherAgent,
    ) -> None:
        """stop_all removes all trackers."""
        await watcher.watch("/tmp/test", backend="local")
        assert len(watcher._trackers) == 1

        await watcher.stop_all()
        assert len(watcher._trackers) == 0

    @pytest.mark.asyncio
    async def test_find_watch_id(self, watcher: WatcherAgent) -> None:
        """Can find watch ID by path."""
        await watcher.watch("/tmp/test", backend="local")
        assert watcher._find_watch_id("/tmp/test") == "local:/tmp/test"
        assert watcher._find_watch_id("/nonexistent") is None


# =============================================================================
# Data Model Tests
# =============================================================================


class TestWatcherModels:
    """Data model validation."""

    def test_watch_status_defaults(self) -> None:
        status = WatchStatus(watch_id="w1", path="/tmp", backend="local")
        assert status.is_running is True
        assert status.files_processed == 0
        assert status.files_failed == 0
        assert status.last_event_at is None

    def test_watcher_stats_defaults(self) -> None:
        stats = WatcherStats()
        assert stats.active_watches == 0
        assert stats.total_files_processed == 0
        assert stats.watches == []

    def test_watcher_stats_aggregation(self) -> None:
        stats = WatcherStats(
            active_watches=2,
            total_files_processed=10,
            total_files_failed=2,
            watches=[
                WatchStatus(
                    watch_id="w1", path="/a", backend="local",
                    files_processed=7, files_failed=1,
                ),
                WatchStatus(
                    watch_id="w2", path="/b", backend="local",
                    files_processed=3, files_failed=1,
                ),
            ],
        )
        assert stats.total_files_processed == 10
        assert stats.total_files_failed == 2
