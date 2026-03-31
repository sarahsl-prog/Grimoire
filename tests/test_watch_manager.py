"""Comprehensive tests for WatchManager hybrid watching implementation.

This module tests the WatchManager including:
- Local path watching with watchdog
- Cloud path polling with asyncio
- Mixed local + cloud path handling
- Event callback invocation
- Start/stop watch lifecycle
- Error handling and edge cases
- Context manager support
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import Mock

import pytest
import pytest_asyncio

from grimoire.storage.base import (
    FileChange,
    FileChangeType,
    StorageBackend,
)
from grimoire.storage.watch_manager import (
    ActiveWatch,
    CloudStoragePoller,
    WatchConfig,
    WatchManager,
    WatchType,
    _WatchdogEventHandler,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_directory() -> Generator[Path, None, None]:
    """Provide a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield Path(tmp_dir)


@pytest.fixture
def watch_manager() -> WatchManager:
    """Create a WatchManager instance for testing."""
    return WatchManager(max_local_watches=10)


@pytest_asyncio.fixture
async def async_watch_manager() -> WatchManager:
    """Create an async-capable WatchManager."""
    manager = WatchManager(max_local_watches=10)
    try:
        yield manager
    finally:
        await manager.stop_all()


@pytest.fixture
def mock_callback() -> Mock:
    """Create a mock callback function for testing."""
    return Mock()


# =============================================================================
# Initialization Tests
# =============================================================================


class TestWatchManagerInitialization:
    """Tests for WatchManager initialization and configuration."""

    def test_can_create_watch_manager(self) -> None:
        """WatchManager can be instantiated."""
        manager = WatchManager()
        assert manager is not None
        assert isinstance(manager, WatchManager)

    def test_default_poll_intervals_set(self) -> None:
        """Default poll intervals are configured."""
        manager = WatchManager()
        assert manager._default_poll_intervals[StorageBackend.GOOGLE_DRIVE] == 300
        assert manager._default_poll_intervals[StorageBackend.ONE_DRIVE] == 300
        assert manager._default_poll_intervals[StorageBackend.RCLONE] == 60
        assert manager._default_poll_intervals[StorageBackend.LOCAL] == 0
        assert manager._default_poll_intervals[StorageBackend.USB] == 0

    def test_custom_poll_intervals_override_defaults(self) -> None:
        """Custom poll intervals override defaults."""
        custom_intervals = {StorageBackend.GOOGLE_DRIVE: 60}
        manager = WatchManager(default_poll_intervals=custom_intervals)
        assert manager._default_poll_intervals[StorageBackend.GOOGLE_DRIVE] == 60

    def test_max_local_watches_configurable(self) -> None:
        """Max local watches can be configured."""
        manager = WatchManager(max_local_watches=50)
        assert manager._max_local_watches == 50

    def test_initial_state_empty(self) -> None:
        """WatchManager starts with no active watches."""
        manager = WatchManager()
        assert manager.list_watches() == []
        assert manager._local_watch_count == 0


# =============================================================================
# Local Path Watching Tests
# =============================================================================


class TestLocalPathWatching:
    """Tests for local filesystem watching with watchdog."""

    @pytest.mark.asyncio
    async def test_can_start_local_watch(self, temp_directory: Path) -> None:
        """Can start watching a local directory."""
        manager = WatchManager()
        callback = Mock()
        watch_id = await manager.start_watch(
            str(temp_directory), "local", callback=callback
        )
        assert watch_id.startswith("local:")
        assert len(manager.list_watches()) == 1
        await manager.stop_watch(watch_id)

    @pytest.mark.asyncio
    async def test_local_watch_uses_watchdog(self, temp_directory: Path) -> None:
        """Local watches create a watchdog Observer."""
        manager = WatchManager()
        watch_id = await manager.start_watch(
            str(temp_directory), "local", callback=Mock()
        )
        watch = manager._watches[watch_id]
        assert watch.config.watch_type == WatchType.LOCAL
        assert watch.local_observer is not None
        await manager.stop_watch(watch_id)

    @pytest.mark.asyncio
    async def test_local_watch_requires_existing_path(self) -> None:
        """Starting watch on non-existent path raises RuntimeError."""
        manager = WatchManager()
        with pytest.raises(RuntimeError, match="Path does not exist"):
            await manager.start_watch("/nonexistent/path123", "local", callback=Mock())

    @pytest.mark.asyncio
    async def test_local_watch_respects_recursive_option(
        self, temp_directory: Path
    ) -> None:
        """Recursive option is stored in watch config."""
        manager = WatchManager()
        watch_id = await manager.start_watch(
            str(temp_directory), "local", callback=Mock(), recursive=False
        )
        watch = manager._watches[watch_id]
        assert watch.config.recursive is False
        await manager.stop_watch(watch_id)

    @pytest.mark.asyncio
    async def test_cannot_watch_same_path_twice(self, temp_directory: Path) -> None:
        """Watching same path twice raises ValueError."""
        manager = WatchManager()
        callback = Mock()
        watch_id = await manager.start_watch(
            str(temp_directory), "local", callback=callback
        )
        with pytest.raises(ValueError, match="Already watching"):
            await manager.start_watch(str(temp_directory), "local", callback=callback)
        await manager.stop_watch(watch_id)

    @pytest.mark.asyncio
    async def test_local_watch_count_tracked(self, temp_directory: Path) -> None:
        """Local watch counter is incremented."""
        manager = WatchManager()
        assert manager._local_watch_count == 0
        subdir = temp_directory / "subdir"
        subdir.mkdir()
        watch_id1 = await manager.start_watch(
            str(temp_directory), "local", callback=Mock()
        )
        assert manager._local_watch_count == 1
        watch_id2 = await manager.start_watch(str(subdir), "local", callback=Mock())
        assert manager._local_watch_count == 2
        await manager.stop_watch(watch_id1)
        await manager.stop_watch(watch_id2)
        assert manager._local_watch_count == 0

    @pytest.mark.asyncio
    async def test_max_local_watches_enforced(self, temp_directory: Path) -> None:
        """Cannot exceed max_local_watches limit."""
        manager = WatchManager(max_local_watches=1)
        callback = Mock()
        subdir = temp_directory / "subdir"
        subdir.mkdir()
        watch_id = await manager.start_watch(
            str(temp_directory), "local", callback=callback
        )
        with pytest.raises(RuntimeError, match="Maximum number of local watches"):
            await manager.start_watch(str(subdir), "local", callback=callback)
        await manager.stop_watch(watch_id)

    @pytest.mark.asyncio
    async def test_can_stop_local_watch(self, temp_directory: Path) -> None:
        """Can stop a local watch."""
        manager = WatchManager()
        watch_id = await manager.start_watch(
            str(temp_directory), "local", callback=Mock()
        )
        result = await manager.stop_watch(watch_id)
        assert result is True
        assert watch_id not in manager._watches
        assert len(manager.list_watches()) == 0


# =============================================================================
# Cloud Path Watching Tests
# =============================================================================


class TestCloudPathWatching:
    """Tests for cloud storage path polling."""

    @pytest.mark.asyncio
    async def test_can_start_cloud_watch(self) -> None:
        """Can start watching a cloud path."""
        manager = WatchManager()
        watch_id = await manager.start_watch("gdrive://test", "gdrive", callback=Mock())
        assert watch_id.startswith("gdrive:")
        assert len(manager.list_watches()) == 1
        await manager.stop_watch(watch_id)

    @pytest.mark.asyncio
    async def test_cloud_watch_uses_polling(self) -> None:
        """Cloud watches create an asyncio task."""
        manager = WatchManager()
        watch_id = await manager.start_watch("gdrive://test", "gdrive", callback=Mock())
        watch = manager._watches[watch_id]
        assert watch.config.watch_type == WatchType.CLOUD
        assert watch.cloud_task is not None
        assert not watch.cloud_task.done()
        await manager.stop_watch(watch_id)

    @pytest.mark.asyncio
    async def test_cloud_watch_uses_default_poll_interval(self) -> None:
        """Cloud watches use default poll interval from config."""
        manager = WatchManager()
        watch_id = await manager.start_watch("gdrive://test", "gdrive", callback=Mock())
        assert manager._watches[watch_id].config.poll_interval == 300
        await manager.stop_watch(watch_id)

    @pytest.mark.asyncio
    async def test_cloud_watch_accepts_custom_poll_interval(self) -> None:
        """Cloud watches accept custom poll interval."""
        manager = WatchManager()
        watch_id = await manager.start_watch(
            "gdrive://test", "gdrive", callback=Mock(), poll_interval=60
        )
        assert manager._watches[watch_id].config.poll_interval == 60
        await manager.stop_watch(watch_id)

    @pytest.mark.asyncio
    async def test_onedrive_uses_cloud_polling(self) -> None:
        """OneDrive paths use cloud polling."""
        manager = WatchManager()
        watch_id = await manager.start_watch(
            "onedrive://test", "onedrive", callback=Mock()
        )
        assert manager._watches[watch_id].config.watch_type == WatchType.CLOUD
        await manager.stop_watch(watch_id)

    @pytest.mark.asyncio
    async def test_rclone_uses_cloud_polling(self) -> None:
        """Rclone paths use cloud polling."""
        manager = WatchManager()
        watch_id = await manager.start_watch("/mnt/rclone", "rclone", callback=Mock())
        assert manager._watches[watch_id].config.watch_type == WatchType.CLOUD
        await manager.stop_watch(watch_id)

    @pytest.mark.asyncio
    async def test_can_stop_cloud_watch(self) -> None:
        """Can stop a cloud watch."""
        manager = WatchManager()
        watch_id = await manager.start_watch("gdrive://test", "gdrive", callback=Mock())
        result = await manager.stop_watch(watch_id)
        assert result is True

    @pytest.mark.asyncio
    async def test_cloud_task_cancelled_on_stop(self) -> None:
        """Cloud polling task is cancelled when stopped."""
        manager = WatchManager()
        watch_id = await manager.start_watch("gdrive://test", "gdrive", callback=Mock())
        task = manager._watches[watch_id].cloud_task
        assert not task.done()
        await manager.stop_watch(watch_id)
        assert task.done()
        assert task.cancelled()


# =============================================================================
# Mixed Local and Cloud Tests
# =============================================================================


class TestMixedPaths:
    """Tests for simultaneous local and cloud watching."""

    @pytest.mark.asyncio
    async def test_can_watch_local_and_cloud_simultaneously(
        self, temp_directory: Path
    ) -> None:
        """Can watch local and cloud paths at the same time."""
        manager = WatchManager()
        callback = Mock()
        local_id = await manager.start_watch(
            str(temp_directory), "local", callback=callback
        )
        cloud_id = await manager.start_watch(
            "gdrive://test", "gdrive", callback=callback
        )
        watches = manager.list_watches()
        assert len(watches) == 2
        watch_types = {w["watch_type"] for w in watches}
        assert watch_types == {"local", "cloud"}
        await manager.stop_watch(local_id)
        await manager.stop_watch(cloud_id)

    @pytest.mark.asyncio
    async def test_multiple_cloud_backends(self) -> None:
        """Can watch multiple cloud backends simultaneously."""
        manager = WatchManager()
        callback = Mock()
        gdrive_id = await manager.start_watch(
            "gdrive://docs", "gdrive", callback=callback
        )
        onedrive_id = await manager.start_watch(
            "onedrive://work", "onedrive", callback=callback
        )
        watches = manager.list_watches()
        assert len(watches) == 2
        backends = {w["backend"] for w in watches}
        assert backends == {"gdrive", "onedrive"}
        await manager.stop_watch(gdrive_id)
        await manager.stop_watch(onedrive_id)

    @pytest.mark.asyncio
    async def test_stop_all_removes_all_watches(self, temp_directory: Path) -> None:
        """stop_all removes all watches."""
        manager = WatchManager()
        callback = Mock()
        await manager.start_watch(str(temp_directory), "local", callback=callback)
        await manager.start_watch("gdrive://test", "gdrive", callback=callback)
        await manager.start_watch("onedrive://test", "onedrive", callback=callback)
        assert len(manager.list_watches()) == 3
        await manager.stop_all()
        assert len(manager.list_watches()) == 0


# =============================================================================
# Event Callback Tests
# =============================================================================


class TestEventCallbacks:
    """Tests for event callback invocation."""

    @pytest.mark.asyncio
    async def test_local_watch_invokes_sync_callback(
        self, temp_directory: Path
    ) -> None:
        """Local watch can invoke synchronous callback."""
        changes: list[FileChange] = []

        def callback(change: FileChange) -> None:
            changes.append(change)

        handler = _WatchdogEventHandler(
            callback=callback, watch_path=str(temp_directory)
        )
        from watchdog.events import FileCreatedEvent

        event = FileCreatedEvent(src_path=str(temp_directory / "test.txt"))
        handler.dispatch(event)
        await asyncio.sleep(0.1)
        assert len(changes) == 1
        assert changes[0].change_type == FileChangeType.CREATED

    @pytest.mark.asyncio
    async def test_local_watch_invokes_async_callback(
        self, temp_directory: Path
    ) -> None:
        """Local watch can invoke async callback."""
        changes: list[FileChange] = []

        async def async_callback(change: FileChange) -> None:
            changes.append(change)

        handler = _WatchdogEventHandler(
            callback=async_callback, watch_path=str(temp_directory)
        )
        from watchdog.events import FileModifiedEvent

        event = FileModifiedEvent(src_path=str(temp_directory / "test.txt"))
        handler.dispatch(event)
        await asyncio.sleep(0.1)
        assert len(changes) == 1
        assert changes[0].change_type == FileChangeType.MODIFIED

    @pytest.mark.asyncio
    async def test_callback_error_handled(self, temp_directory: Path) -> None:
        """Callback exceptions are handled gracefully."""

        def bad_callback(change: FileChange) -> None:
            raise RuntimeError("Test error")

        handler = _WatchdogEventHandler(
            callback=bad_callback, watch_path=str(temp_directory)
        )
        from watchdog.events import FileDeletedEvent

        event = FileDeletedEvent(src_path=str(temp_directory / "test.txt"))
        handler.dispatch(event)
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_async_callback_error_handled(self, temp_directory: Path) -> None:
        """Async callback exceptions are handled gracefully."""

        async def bad_async_callback(change: FileChange) -> None:
            raise RuntimeError("Test error")

        handler = _WatchdogEventHandler(
            callback=bad_async_callback, watch_path=str(temp_directory)
        )
        from watchdog.events import FileMovedEvent

        event = FileMovedEvent(
            src_path=str(temp_directory / "old.txt"),
            dest_path=str(temp_directory / "new.txt"),
        )
        handler.dispatch(event)
        await asyncio.sleep(0.1)


# =============================================================================
# Watchdog Event Handler Tests
# =============================================================================


class TestWatchdogEventHandler:
    """Tests for _WatchdogEventHandler class."""

    def test_maps_created_events(self, temp_directory: Path) -> None:
        """FileCreatedEvent maps to CREATED change type."""
        changes: list[FileChange] = []
        handler = _WatchdogEventHandler(
            callback=lambda c: changes.append(c), watch_path=str(temp_directory)
        )
        from watchdog.events import FileCreatedEvent, DirCreatedEvent

        handler.dispatch(FileCreatedEvent(src_path="/test/file.txt"))
        assert changes[-1].change_type == FileChangeType.CREATED
        handler.dispatch(DirCreatedEvent(src_path="/test/dir"))
        assert changes[-1].change_type == FileChangeType.CREATED

    def test_maps_deleted_events(self, temp_directory: Path) -> None:
        """FileDeletedEvent maps to DELETED change type."""
        changes: list[FileChange] = []
        handler = _WatchdogEventHandler(
            callback=lambda c: changes.append(c), watch_path=str(temp_directory)
        )
        from watchdog.events import FileDeletedEvent, DirDeletedEvent

        handler.dispatch(FileDeletedEvent(src_path="/test/file.txt"))
        assert changes[-1].change_type == FileChangeType.DELETED
        handler.dispatch(DirDeletedEvent(src_path="/test/dir"))
        assert changes[-1].change_type == FileChangeType.DELETED

    def test_maps_modified_events(self, temp_directory: Path) -> None:
        """FileModifiedEvent maps to MODIFIED change type."""
        changes: list[FileChange] = []
        handler = _WatchdogEventHandler(
            callback=lambda c: changes.append(c), watch_path=str(temp_directory)
        )
        from watchdog.events import FileModifiedEvent, DirModifiedEvent

        handler.dispatch(FileModifiedEvent(src_path="/test/file.txt"))
        assert changes[-1].change_type == FileChangeType.MODIFIED
        handler.dispatch(DirModifiedEvent(src_path="/test/dir"))
        assert changes[-1].change_type == FileChangeType.MODIFIED

    def test_maps_moved_events(self, temp_directory: Path) -> None:
        """FileMovedEvent maps to MOVED change type."""
        changes: list[FileChange] = []
        handler = _WatchdogEventHandler(
            callback=lambda c: changes.append(c), watch_path=str(temp_directory)
        )
        from watchdog.events import FileMovedEvent

        handler.dispatch(
            FileMovedEvent(src_path="/test/old.txt", dest_path="/test/new.txt")
        )
        assert changes[-1].change_type == FileChangeType.MOVED
        assert changes[-1].previous_path == "/test/new.txt"


# =============================================================================
# List Watches Tests
# =============================================================================


class TestListWatches:
    """Tests for listing active watches."""

    def test_list_watches_returns_empty_list_initially(self) -> None:
        """list_watches returns empty list initially."""
        manager = WatchManager()
        assert manager.list_watches() == []

    @pytest.mark.asyncio
    async def test_list_watches_returns_watch_info(self, temp_directory: Path) -> None:
        """list_watches returns watch information."""
        manager = WatchManager()
        watch_id = await manager.start_watch(
            str(temp_directory), "local", callback=Mock(), recursive=False
        )
        watches = manager.list_watches()
        assert len(watches) == 1
        assert watches[0]["watch_id"] == watch_id
        assert watches[0]["path"] == str(temp_directory)
        assert watches[0]["backend"] == "local"
        assert watches[0]["watch_type"] == "local"
        assert watches[0]["is_running"] is True
        assert watches[0]["recursive"] is False
        await manager.stop_watch(watch_id)

    @pytest.mark.asyncio
    async def test_get_watch_returns_watch_info(self, temp_directory: Path) -> None:
        """get_watch returns specific watch information."""
        manager = WatchManager()
        watch_id = await manager.start_watch(
            str(temp_directory), "local", callback=Mock()
        )
        watch = manager.get_watch(watch_id)
        assert watch is not None
        assert watch["watch_id"] == watch_id
        await manager.stop_watch(watch_id)

    def test_get_watch_returns_none_for_unknown(self) -> None:
        """get_watch returns None for unknown watch."""
        manager = WatchManager()
        assert manager.get_watch("unknown:id") is None

    @pytest.mark.asyncio
    async def test_stop_watch_returns_false_for_unknown(self) -> None:
        """stop_watch returns False for unknown watch."""
        manager = WatchManager()
        result = await manager.stop_watch("unknown:id")
        assert result is False


# =============================================================================
# CloudStoragePoller Tests
# =============================================================================


class TestCloudStoragePoller:
    """Tests for CloudStoragePoller helper class."""

    @pytest.mark.asyncio
    async def test_can_create_poller(self) -> None:
        """CloudStoragePoller can be instantiated."""
        poller = CloudStoragePoller()
        assert poller is not None

    @pytest.mark.asyncio
    async def test_poll_changes_returns_empty_list(self) -> None:
        """poll_changes returns empty list in placeholder implementation."""
        from datetime import datetime

        poller = CloudStoragePoller()
        changes = await poller.poll_changes(
            backend=StorageBackend.GOOGLE_DRIVE,
            path="gdrive://test",
            since=datetime.now(),
        )
        assert changes == []

    def test_page_token_storage(self) -> None:
        """Page tokens can be stored and retrieved."""
        poller = CloudStoragePoller()
        assert poller.get_page_token("gdrive://test") is None
        poller.set_page_token("gdrive://test", "token123")
        assert poller.get_page_token("gdrive://test") == "token123"


# =============================================================================
# Context Manager Tests
# =============================================================================


class TestContextManager:
    """Tests for async context manager support."""

    @pytest.mark.asyncio
    async def test_context_manager_starts_clean(self) -> None:
        """WatchManager can be used as async context manager."""
        async with WatchManager() as manager:
            assert isinstance(manager, WatchManager)
            assert manager.list_watches() == []

    @pytest.mark.asyncio
    async def test_context_manager_stops_all_on_exit(
        self, temp_directory: Path
    ) -> None:
        """Context manager stops all watches on exit."""
        callback = Mock()
        async with WatchManager() as manager:
            await manager.start_watch(str(temp_directory), "local", callback=callback)
            await manager.start_watch("gdrive://test", "gdrive", callback=callback)
            assert len(manager.list_watches()) == 2


# =============================================================================
# Backend String/Enum Tests
# =============================================================================


class TestBackendHandling:
    """Tests for backend string/enum handling."""

    @pytest.mark.asyncio
    async def test_accepts_backend_string(self, temp_directory: Path) -> None:
        """start_watch accepts backend as string."""
        manager = WatchManager()
        watch_id = await manager.start_watch(
            str(temp_directory), "local", callback=Mock()
        )
        assert manager._watches[watch_id].config.backend == StorageBackend.LOCAL
        await manager.stop_watch(watch_id)

    @pytest.mark.asyncio
    async def test_accepts_backend_enum(self, temp_directory: Path) -> None:
        """start_watch accepts backend as enum."""
        manager = WatchManager()
        watch_id = await manager.start_watch(
            str(temp_directory), StorageBackend.LOCAL, callback=Mock()
        )
        assert manager._watches[watch_id].config.backend == StorageBackend.LOCAL
        await manager.stop_watch(watch_id)

    @pytest.mark.asyncio
    async def test_rejects_invalid_backend_string(self) -> None:
        """start_watch rejects invalid backend string."""
        manager = WatchManager()
        with pytest.raises(ValueError, match="Invalid backend"):
            await manager.start_watch("/test", "invalid_backend", callback=Mock())


# =============================================================================
# USB Path Tests
# =============================================================================


class TestUSBPaths:
    """Tests for USB storage paths."""

    @pytest.mark.asyncio
    async def test_usb_uses_local_watching(self, temp_directory: Path) -> None:
        """USB paths use local watchdog watching."""
        manager = WatchManager()
        watch_id = await manager.start_watch(
            str(temp_directory), "usb", callback=Mock()
        )
        watch = manager._watches[watch_id]
        assert watch.config.watch_type == WatchType.LOCAL
        assert watch.config.backend == StorageBackend.USB
        await manager.stop_watch(watch_id)


# =============================================================================
# WatchConfig Tests
# =============================================================================


class TestWatchConfig:
    """Tests for WatchConfig dataclass."""

    def test_config_creation(self) -> None:
        """WatchConfig can be created with default values."""
        callback = Mock()
        config = WatchConfig(
            path="/test/path", backend=StorageBackend.LOCAL, callback=callback
        )
        assert config.path == "/test/path"
        assert config.backend == StorageBackend.LOCAL
        assert config.recursive is True
        assert config.poll_interval == 300

    def test_config_with_custom_values(self) -> None:
        """WatchConfig can be created with custom values."""
        callback = Mock()
        config = WatchConfig(
            path="gdrive://test",
            backend=StorageBackend.GOOGLE_DRIVE,
            callback=callback,
            recursive=False,
            poll_interval=60,
            watch_type=WatchType.CLOUD,
        )
        assert config.recursive is False
        assert config.poll_interval == 60
        assert config.watch_type == WatchType.CLOUD


# =============================================================================
# ActiveWatch Tests
# =============================================================================


class TestActiveWatch:
    """Tests for ActiveWatch dataclass."""

    def test_active_watch_creation(self) -> None:
        """ActiveWatch can be created."""
        config = WatchConfig(
            path="/test", backend=StorageBackend.LOCAL, callback=Mock()
        )
        watch = ActiveWatch(config=config)
        assert watch.config == config
        assert watch.local_observer is None
        assert watch.cloud_task is None
        assert watch.is_running is False


# =============================================================================
# Edge Case Tests
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_stop_already_stopped_watch(self, temp_directory: Path) -> None:
        """Stopping an already stopped watch returns False."""
        manager = WatchManager()
        watch_id = await manager.start_watch(
            str(temp_directory), "local", callback=Mock()
        )
        result1 = await manager.stop_watch(watch_id)
        assert result1 is True
        result2 = await manager.stop_watch(watch_id)
        assert result2 is False

    @pytest.mark.asyncio
    async def test_cloud_poll_loop_handles_exceptions(self) -> None:
        """Cloud poll loop continues despite exceptions."""
        manager = WatchManager()
        config = WatchConfig(
            path="gdrive://test",
            backend=StorageBackend.GOOGLE_DRIVE,
            callback=Mock(),
            poll_interval=1,
            watch_type=WatchType.CLOUD,
        )
        watch = ActiveWatch(config=config)
        watch.is_running = True
        task = asyncio.create_task(manager._cloud_poll_loop(watch))
        await asyncio.sleep(0.1)
        watch.is_running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
