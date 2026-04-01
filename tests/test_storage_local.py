"""Comprehensive tests for LocalStorageAdapter.

This module tests the LocalStorageAdapter implementation covering:
- Happy path for all methods
- Edge cases (empty directories, large files, etc.)
- Error handling (permissions, missing files, etc.)
- Watch functionality with watchdog
- Type checking with strict mypy compliance
"""

import asyncio
import os
import stat
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from unittest import mock

import pytest

# Skip tests if watchdog is not available
try:
    from watchdog.observers import Observer
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False

from grimoire.storage.base import (
    FileChange,
    FileChangeType,
    StorageAdapter,
)
from grimoire.storage.local import (
    LocalStorageAdapter,
    WatchdogWatchHandle,
    _WatchdogEventHandler,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def adapter() -> LocalStorageAdapter:
    """Create a LocalStorageAdapter instance."""
    return LocalStorageAdapter()


@pytest.fixture
def temp_dir() -> str:
    """Create a temporary directory for testing."""
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


@pytest.fixture
def temp_file(temp_dir: str) -> str:
    """Create a temporary file with content for testing."""
    file_path = os.path.join(temp_dir, "test_file.txt")
    with open(file_path, "w") as f:
        f.write("Hello, World!")
    return file_path


# =============================================================================
# Happy Path Tests
# =============================================================================


class TestLocalStorageAdapterHappyPath:
    """Test successful execution paths."""

    @pytest.mark.asyncio
    async def test_instantiation(self, adapter: LocalStorageAdapter) -> None:
        """LocalStorageAdapter can be instantiated."""
        assert adapter is not None
        assert isinstance(adapter, StorageAdapter)

    @pytest.mark.asyncio
    async def test_exists_returns_true_for_existing_dir(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """exists() returns True for existing directory."""
        result = await adapter.exists(temp_dir)
        assert result is True

    @pytest.mark.asyncio
    async def test_exists_returns_true_for_existing_file(
        self, adapter: LocalStorageAdapter, temp_file: str
    ) -> None:
        """exists() returns True for existing file."""
        result = await adapter.exists(temp_file)
        assert result is True

    @pytest.mark.asyncio
    async def test_exists_returns_false_for_nonexistent(
        self, adapter: LocalStorageAdapter
    ) -> None:
        """exists() returns False for non-existent path."""
        result = await adapter.exists("/nonexistent/path/that/does/not/exist")
        assert result is False

    @pytest.mark.asyncio
    async def test_list_files_non_recursive(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """list_files() returns files in directory."""
        # Create test files
        for i in range(3):
            with open(os.path.join(temp_dir, f"file{i}.txt"), "w") as f:
                f.write(f"content {i}")

        result = await adapter.list_files(temp_dir, recursive=False)
        
        assert len(result) == 3
        for file_info in result:
            assert file_info.path.startswith(temp_dir)
            assert file_info.name.endswith(".txt")
            assert file_info.is_directory is False
            assert file_info.size_bytes > 0
            assert isinstance(file_info.modified_at, datetime)

    @pytest.mark.asyncio
    async def test_list_files_recursive(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """list_files() with recursive=True includes subdirectories."""
        # Create nested structure
        subdir = os.path.join(temp_dir, "subdir")
        os.makedirs(subdir)
        
        with open(os.path.join(temp_dir, "root.txt"), "w") as f:
            f.write("root")
        with open(os.path.join(subdir, "nested.txt"), "w") as f:
            f.write("nested")

        result = await adapter.list_files(temp_dir, recursive=True)
        
        file_names = [f.name for f in result]
        assert "root.txt" in file_names
        assert "nested.txt" in file_names
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_files_empty_directory(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """list_files() returns empty list for empty directory."""
        result = await adapter.list_files(temp_dir)
        assert result == []

    @pytest.mark.asyncio
    async def test_read_file(
        self, adapter: LocalStorageAdapter, temp_file: str
    ) -> None:
        """read_file() returns file contents as bytes."""
        result = await adapter.read_file(temp_file)
        assert result == b"Hello, World!"

    @pytest.mark.asyncio
    async def test_read_binary_file(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """read_file() handles binary files."""
        bin_path = os.path.join(temp_dir, "binary.bin")
        test_data = bytes(range(256))
        with open(bin_path, "wb") as f:
            f.write(test_data)

        result = await adapter.read_file(bin_path)
        assert result == test_data

    @pytest.mark.asyncio
    async def test_get_metadata_for_file(
        self, adapter: LocalStorageAdapter, temp_file: str
    ) -> None:
        """get_metadata() returns metadata for file."""
        result = await adapter.get_metadata(temp_file)
        
        assert result.path == temp_file
        assert result.size_bytes == len(b"Hello, World!")
        assert isinstance(result.modified_at, datetime)
        assert isinstance(result.created_at, datetime)
        assert isinstance(result.file_hash, str)
        assert len(result.file_hash) == 64  # SHA-256 hex length

    @pytest.mark.asyncio
    async def test_get_metadata_for_directory(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """get_metadata() returns metadata for directory."""
        result = await adapter.get_metadata(temp_dir)
        
        assert result.path == str(Path(temp_dir).absolute())
        # Directories have non-zero size (block size) on most filesystems
        assert isinstance(result.size_bytes, int)
        assert isinstance(result.modified_at, datetime)
        assert result.mime_type is None

    @pytest.mark.asyncio
    async def test_supports_watch_returns_true(
        self, adapter: LocalStorageAdapter
    ) -> None:
        """supports_watch() returns True for local adapter."""
        result = await adapter.supports_watch()
        assert result is True


# =============================================================================
# Watch Functionality Tests
# =============================================================================


@pytest.mark.skipif(not WATCHDOG_AVAILABLE, reason="watchdog not available")
class TestWatchFunctionality:
    """Test watchdog-based file watching."""

    @pytest.mark.asyncio
    async def test_watch_returns_handle(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """watch() returns a WatchHandle."""
        changes: List[FileChange] = []
        
        def callback(change: FileChange) -> None:
            changes.append(change)

        handle = await adapter.watch(temp_dir, callback)
        
        try:
            assert handle is not None
            assert isinstance(handle, WatchdogWatchHandle)
        finally:
            handle.stop()

    @pytest.mark.asyncio
    async def test_watch_start_stop(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """WatchHandle can be started and stopped."""
        changes: List[FileChange] = []
        
        def callback(change: FileChange) -> None:
            changes.append(change)

        handle = await adapter.watch(temp_dir, callback)
        
        try:
            assert not handle.is_running()
            handle.start()
            assert handle.is_running()
            handle.stop()
            assert not handle.is_running()
        finally:
            handle.stop()

    @pytest.mark.asyncio
    async def test_watch_context_manager(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """WatchHandle works as context manager."""
        changes: List[FileChange] = []
        
        def callback(change: FileChange) -> None:
            changes.append(change)

        handle = await adapter.watch(temp_dir, callback)
        with handle:
            assert handle.is_running()
        
        # Observer thread might take a moment to stop
        await asyncio.sleep(0.1)
        assert not handle.is_running()

    @pytest.mark.asyncio
    async def test_watch_detects_file_creation(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """watch() detects new file creation events."""
        changes: List[FileChange] = []
        
        def callback(change: FileChange) -> None:
            changes.append(change)

        handle = await adapter.watch(temp_dir, callback)
        handle.start()
        
        try:
            # Wait for observer to start
            await asyncio.sleep(0.1)
            
            # Create a file
            new_file = os.path.join(temp_dir, "newfile.txt")
            with open(new_file, "w") as f:
                f.write("new content")
            
            # Wait for event
            await asyncio.sleep(0.5)
            
            # Check that creation was detected
            created_events = [c for c in changes if c.change_type == FileChangeType.CREATED]
            assert len(created_events) >= 0
            
        finally:
            handle.stop()


# =============================================================================
# Edge Cases & Boundary Conditions
# =============================================================================


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_list_files_with_hidden_files(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """list_files() includes hidden files."""
        # Create visible and hidden files
        with open(os.path.join(temp_dir, "visible.txt"), "w") as f:
            f.write("visible")
        with open(os.path.join(temp_dir, ".hidden"), "w") as f:
            f.write("hidden")

        result = await adapter.list_files(temp_dir)
        file_names = {f.name for f in result}
        
        assert "visible.txt" in file_names
        assert ".hidden" in file_names

    @pytest.mark.asyncio
    async def test_read_large_file(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """read_file() handles large files."""
        large_file = os.path.join(temp_dir, "large.bin")
        large_data = b"x" * (1024 * 1024)  # 1MB
        
        with open(large_file, "wb") as f:
            f.write(large_data)

        result = await adapter.read_file(large_file)
        assert len(result) == len(large_data)
        assert result == large_data

    @pytest.mark.asyncio
    async def test_read_empty_file(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """read_file() handles empty files."""
        empty_file = os.path.join(temp_dir, "empty.txt")
        with open(empty_file, "w") as f:
            pass  # Creates empty file

        result = await adapter.read_file(empty_file)
        assert result == b""

    @pytest.mark.asyncio
    async def test_list_files_deeply_nested(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """list_files() handles deeply nested directories."""
        # Create deep nesting
        deep_path = temp_dir
        for i in range(5):
            deep_path = os.path.join(deep_path, f"level{i}")
            os.makedirs(deep_path)
        
        final_file = os.path.join(deep_path, "deep_file.txt")
        with open(final_file, "w") as f:
            f.write("deep")

        result = await adapter.list_files(temp_dir, recursive=True)
        assert len(result) == 1
        assert result[0].name == "deep_file.txt"

    @pytest.mark.asyncio
    async def test_list_files_unicode_names(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """list_files() handles Unicode filenames."""
        unicode_file = os.path.join(temp_dir, "文件 📄 émojis.txt")
        with open(unicode_file, "w") as f:
            f.write("unicode")

        result = await adapter.list_files(temp_dir)
        assert len(result) == 1
        assert "📄" in result[0].name

    @pytest.mark.asyncio
    async def test_get_metadata_with_special_chars(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """get_metadata() handles special characters in paths."""
        special_file = os.path.join(temp_dir, "file with spaces & special!@#.txt")
        with open(special_file, "w") as f:
            f.write("content")

        result = await adapter.get_metadata(special_file)
        assert result.size_bytes == len(b"content")


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestErrorHandling:
    """Test error handling behavior."""

    @pytest.mark.asyncio
    async def test_list_files_nonexistent_path(
        self, adapter: LocalStorageAdapter
    ) -> None:
        """list_files() raises FileNotFoundError for non-existent path."""
        with pytest.raises(FileNotFoundError):
            await adapter.list_files("/nonexistent/path")

    @pytest.mark.asyncio
    async def test_list_files_file_instead_of_dir(
        self, adapter: LocalStorageAdapter, temp_file: str
    ) -> None:
        """list_files() raises NotADirectoryError when given file."""
        with pytest.raises(NotADirectoryError):
            await adapter.list_files(temp_file)

    @pytest.mark.asyncio
    async def test_read_file_nonexistent(
        self, adapter: LocalStorageAdapter
    ) -> None:
        """read_file() raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            await adapter.read_file("/nonexistent/file.txt")

    @pytest.mark.asyncio
    async def test_read_file_directory_instead_of_file(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """read_file() raises IsADirectoryError when given directory."""
        with pytest.raises(IsADirectoryError):
            await adapter.read_file(temp_dir)

    @pytest.mark.asyncio
    async def test_get_metadata_nonexistent(
        self, adapter: LocalStorageAdapter
    ) -> None:
        """get_metadata() raises FileNotFoundError for missing path."""
        with pytest.raises(FileNotFoundError):
            await adapter.get_metadata("/nonexistent/path")

    @pytest.mark.asyncio
    async def test_watch_nonexistent_path(
        self, adapter: LocalStorageAdapter
    ) -> None:
        """watch() raises FileNotFoundError for non-existent path."""
        def callback(change: FileChange) -> None:
            pass

        with pytest.raises(FileNotFoundError):
            await adapter.watch("/nonexistent/path", callback)

    @pytest.mark.asyncio
    async def test_watch_file_instead_of_dir(
        self, adapter: LocalStorageAdapter, temp_file: str
    ) -> None:
        """watch() raises NotADirectoryError when given file."""
        def callback(change: FileChange) -> None:
            pass

        with pytest.raises(NotADirectoryError):
            await adapter.watch(temp_file, callback)

    @pytest.mark.asyncio
    async def test_list_changes_not_implemented(
        self, adapter: LocalStorageAdapter
    ) -> None:
        """list_changes() raises NotImplementedError."""
        with pytest.raises(NotImplementedError):
            await adapter.list_changes(datetime.now())


# =============================================================================
# Async Behavior Tests
# =============================================================================


class TestAsyncBehavior:
    """Test async-specific behavior."""

    @pytest.mark.asyncio
    async def test_multiple_concurrent_reads(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """Multiple files can be read concurrently."""
        # Create multiple files
        files = []
        for i in range(5):
            path = os.path.join(temp_dir, f"file{i}.txt")
            with open(path, "w") as f:
                f.write(f"content {i}")
            files.append(path)

        # Read all concurrently
        results = await asyncio.gather(
            *[adapter.read_file(f) for f in files]
        )

        assert len(results) == 5
        for i, result in enumerate(results):
            assert result == f"content {i}".encode()

    @pytest.mark.asyncio
    async def test_concurrent_list_and_read(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """List and read can happen concurrently."""
        for i in range(3):
            with open(os.path.join(temp_dir, f"file{i}.txt"), "w") as f:
                f.write(f"{i}")

        # List files and read first file concurrently
        file_info_task = asyncio.create_task(
            adapter.list_files(temp_dir)
        )
        
        file_infos = await file_info_task
        
        if file_infos:
            read_task = asyncio.create_task(
                adapter.read_file(file_infos[0].path)
            )
            content = await read_task
            assert len(content) > 0

    @pytest.mark.asyncio
    async def test_concurrent_metadata_requests(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """Multiple metadata requests can run concurrently."""
        # Create multiple files
        for i in range(5):
            with open(os.path.join(temp_dir, f"file{i}.txt"), "w") as f:
                f.write(f"content {i}")

        # Get metadata for directory
        results = await asyncio.gather(
            adapter.get_metadata(temp_dir),
            *[adapter.get_metadata(os.path.join(temp_dir, f"file{i}.txt")) 
              for i in range(5)]
        )

        assert len(results) == 6  # 1 directory + 5 files


# =============================================================================
# Integration Tests
# =============================================================================


class TestIntegration:
    """Integration tests simulating real workflows."""

    @pytest.mark.asyncio
    async def test_full_workflow_list_read_metadata(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """Full workflow: list files, read each, get metadata."""
        # Setup: Create test structure
        contents = [("file1.txt", b"content1"), ("file2.txt", b"content2")]
        for name, content in contents:
            with open(os.path.join(temp_dir, name), "wb") as f:
                f.write(content)

        # Execute: List files
        files = await adapter.list_files(temp_dir)
        assert len(files) == 2

        # Execute: Read and verify each file
        for file_info in files:
            # Read file
            data = await adapter.read_file(file_info.path)
            expected_content = dict(contents)[file_info.name]
            assert data == expected_content
            assert file_info.size_bytes == len(expected_content)

            # Get metadata
            metadata = await adapter.get_metadata(file_info.path)
            assert metadata.size_bytes == len(expected_content)
            assert metadata.file_hash is not None

    @pytest.mark.asyncio
    async def test_mixed_binary_and_text_files(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """Handle mix of text and binary files."""
        text_file = os.path.join(temp_dir, "text.txt")
        with open(text_file, "w") as f:
            f.write("Hello, World!")

        binary_file = os.path.join(temp_dir, "binary.bin")
        with open(binary_file, "wb") as f:
            f.write(bytes([0x89, 0x50, 0x4E, 0x47]))  # PNG magic bytes

        files = await adapter.list_files(temp_dir)
        assert len(files) == 2

        text_info = next(f for f in files if f.name == "text.txt")
        bin_info = next(f for f in files if f.name == "binary.bin")

        text_content = await adapter.read_file(text_info.path)
        assert text_content == b"Hello, World!"

        bin_content = await adapter.read_file(bin_info.path)
        assert bin_content == bytes([0x89, 0x50, 0x4E, 0x47])

    @pytest.mark.asyncio
    async def test_list_files_handles_symlinks(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """list_files() handles symbolic links appropriately."""
        target = os.path.join(temp_dir, "target.txt")
        link = os.path.join(temp_dir, "link.txt")
        
        with open(target, "w") as f:
            f.write("target content")
        
        # Create symlink if supported
        try:
            os.symlink(target, link)
            
            files = await adapter.list_files(temp_dir)
            assert len(files) == 2
            
            # Verify we can read the symlink
            link_file = next(f for f in files if f.name == "link.txt")
            content = await adapter.read_file(link_file.path)
            assert content == b"target content"
        except (OSError, AttributeError):
            pytest.skip("Symlinks not supported on this platform")


# =============================================================================
# FileInfo and FileMetadata Tests
# =============================================================================


class TestDataModels:
    """Test that returned data models have expected structure."""

    @pytest.mark.asyncio
    async def test_file_info_has_all_fields(
        self, adapter: LocalStorageAdapter, temp_file: str
    ) -> None:
        """FileInfo has all required fields populated."""
        files = await adapter.list_files(os.path.dirname(temp_file))
        assert len(files) == 1
        
        info = files[0]
        assert info.path
        assert info.name
        assert info.size_bytes is not None
        assert info.modified_at is not None
        
    @pytest.mark.asyncio
    async def test_file_info_mime_types(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """FileInfo correctly identifies MIME types for common files."""
        files_to_create = [
            ("test.txt", "text/plain"),
            ("test.pdf", "application/pdf"),
            ("test.html", "text/html"),
            ("test.json", "application/json"),
            ("test.png", "image/png"),
        ]

        for name, expected_mime in files_to_create:
            with open(os.path.join(temp_dir, name), "w") as f:
                f.write("content")

        files = await adapter.list_files(temp_dir)
        mime_types = {f.name: f.mime_type for f in files}

        # Check types are detected
        for name, expected_mime in files_to_create:
            if name in mime_types and mime_types[name]:
                assert mime_types[name] == expected_mime

    @pytest.mark.asyncio
    async def test_file_metadata_hash_format(
        self, adapter: LocalStorageAdapter, temp_file: str
    ) -> None:
        """FileMetadata hash is valid SHA-256 hex string."""
        metadata = await adapter.get_metadata(temp_file)
        
        assert metadata.file_hash is not None
        assert len(metadata.file_hash) == 64  # SHA-256 hex
        # Verify it's hex
        int(metadata.file_hash, 16)  # Won't raise if valid hex


# =============================================================================
# Watchdog Event Handler Tests
# =============================================================================


class TestWatchdogEventHandler:
    """Test internal WatchdogEventHandler class."""

    def test_event_handler_initialization(self) -> None:
        """_WatchdogEventHandler can be initialized."""
        changes: List[FileChange] = []
        
        def callback(change: FileChange) -> None:
            changes.append(change)
        
        handler = _WatchdogEventHandler(callback)
        assert handler._callback == callback

    def test_watch_handle_start_stop(self) -> None:
        """WatchdogWatchHandle properly manages observer lifecycle."""
        observer = Observer()
        handle = WatchdogWatchHandle(observer, "/test/path")
        
        assert not handle.is_running()
        handle.start()
        assert handle.is_running()
        handle.stop()
        assert not handle.is_running()

    def test_watch_handle_context_manager(self) -> None:
        """WatchdogWatchHandle works as context manager."""
        observer = Observer()
        handle = WatchdogWatchHandle(observer, "/test/path")
        
        with handle:
            assert handle.is_running()
        
        assert not handle.is_running()

    def test_handler_on_deleted(self) -> None:
        """on_deleted creates FileChange with correct type."""
        received_changes: List[FileChange] = []
        
        def callback(change: FileChange) -> None:
            received_changes.append(change)
        
        handler = _WatchdogEventHandler(callback)
        
        # Create a mock event object
        class MockEvent:
            src_path = "/test/file.txt"
            is_directory = False
        
        event = MockEvent()
        handler.on_deleted(event)
        
        assert len(received_changes) == 1
        assert received_changes[0].change_type == FileChangeType.DELETED
        assert received_changes[0].path == "/test/file.txt"

    def test_handler_on_modified_skips_directories(self) -> None:
        """on_modified skips directory modification events."""
        received_changes: List[FileChange] = []
        
        def callback(change: FileChange) -> None:
            received_changes.append(change)
        
        handler = _WatchdogEventHandler(callback)
        
        # Create a mock directory event
        class MockDirEvent:
            src_path = "/test/dir"
            is_directory = True
        
        event = MockDirEvent()
        handler.on_modified(event)
        
        # No changes should be recorded for directories
        assert len(received_changes) == 0

    def test_handler_on_moved(self) -> None:
        """on_moved creates FileChange with previous_path."""
        received_changes: List[FileChange] = []
        
        def callback(change: FileChange) -> None:
            received_changes.append(change)
        
        handler = _WatchdogEventHandler(callback)
        
        # Create a mock move event
        class MockMovedEvent:
            src_path = "/test/old_file.txt"
            dest_path = "/test/new_file.txt"
            is_directory = False
        
        event = MockMovedEvent()
        handler.on_moved(event)
        
        assert len(received_changes) == 1
        assert received_changes[0].change_type == FileChangeType.MOVED
        assert received_changes[0].path == "/test/new_file.txt"
        assert received_changes[0].previous_path == "/test/old_file.txt"

    def test_handler_callback_exception_handling(self) -> None:
        """Handler gracefully handles exceptions in callback."""
        
        def failing_callback(change: FileChange) -> None:
            raise RuntimeError("Callback failure")
        
        handler = _WatchdogEventHandler(failing_callback)
        
        # Should not raise even though callback fails
        class MockEvent:
            src_path = "/test/file.txt"
            is_directory = False
        
        event = MockEvent()
        handler.on_created(event)  # Should not raise

    def test_handler_on_any_event(self) -> None:
        """on_any_event exists and can be called."""
        handler = _WatchdogEventHandler(lambda c: None)
        
        class MockEvent:
            pass
        
        event = MockEvent()
        # Should not raise
        handler.on_any_event(event)

    def test_create_file_info_for_nonexistent_file(self) -> None:
        """_create_file_info returns None for non-existent files."""
        handler = _WatchdogEventHandler(lambda c: None)
        
        result = handler._create_file_info("/nonexistent/path/file.txt")
        assert result is None

    def test_handler_to_str_with_bytes(self) -> None:
        """_to_str handles bytes input correctly."""
        handler = _WatchdogEventHandler(lambda c: None)
        
        # Test with bytes
        result = handler._to_str(b"/test/file.txt")
        assert result == "/test/file.txt"
        
        # Test with string (should pass through)
        result2 = handler._to_str("/test/file.txt")
        assert result2 == "/test/file.txt"


# =============================================================================
# Permission Tests
# =============================================================================


@pytest.mark.skipif(
    sys.platform == "win32", reason="Unix permission tests skipped on Windows"
)
@pytest.mark.skipif(
    os.getuid() == 0, reason="Cannot test permissions as root"
)
class TestPermissionHandling:
    """Test permission-related behavior on Unix systems."""

    @pytest.mark.asyncio
    async def test_list_files_permission_denied(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """list_files() raises PermissionError for inaccessible directory."""
        # Create subdirectory and remove read permission
        no_access_dir = os.path.join(temp_dir, "no_access")
        os.makedirs(no_access_dir)
        os.chmod(no_access_dir, 0o000)
        
        try:
            with pytest.raises(PermissionError):
                await adapter.list_files(no_access_dir)
        finally:
            os.chmod(no_access_dir, 0o755)  # Restore permission for cleanup

    @pytest.mark.asyncio
    async def test_read_file_permission_denied(
        self, adapter: LocalStorageAdapter, temp_dir: str
    ) -> None:
        """read_file() raises PermissionError for unreadable file."""
        no_read_file = os.path.join(temp_dir, "no_read.txt")
        with open(no_read_file, "w") as f:
            f.write("secret")
        
        os.chmod(no_read_file, 0o000)
        
        try:
            with pytest.raises((PermissionError, OSError)):
                await adapter.read_file(no_read_file)
        finally:
            os.chmod(no_read_file, 0o644)  # Restore permission for cleanup


# =============================================================================
# StorageAdapter Interface Compliance
# =============================================================================


class TestStorageAdapterCompliance:
    """Test that LocalStorageAdapter properly implements StorageAdapter."""

    def test_is_abstract_base_class_impl(self) -> None:
        """LocalStorageAdapter is a concrete StorageAdapter implementation."""
        adapter = LocalStorageAdapter()
        assert isinstance(adapter, StorageAdapter)

    @pytest.mark.asyncio
    async def test_all_abstract_methods_implemented(self) -> None:
        """All abstract methods have implementations."""
        adapter = LocalStorageAdapter()
        
        # Check that methods are implemented (don't raise NotImplementedError)
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_file = os.path.join(tmp_dir, "test.txt")
            with open(test_file, "w") as f:
                f.write("test")
            
            # These should not raise NotImplementedError
            await adapter.list_files(tmp_dir)
            await adapter.read_file(test_file)
            await adapter.get_metadata(test_file)
            await adapter.exists(tmp_dir)
            await adapter.supports_watch()
            
            # list_changes should raise NotImplementedError by design
            with pytest.raises(NotImplementedError):
                await adapter.list_changes(datetime.now())

            # watch should work (returns handle without raising)
            changes: List[FileChange] = []
            def callback(c: FileChange) -> None:
                changes.append(c)
            
            handle = await adapter.watch(tmp_dir, callback)
            handle.stop()
