"""Abstract base class and models for storage adapters."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol


class StorageBackend(Enum):
    """Enumeration of supported storage backends."""

    LOCAL = "local"
    USB = "usb"
    RCLONE = "rclone"
    GOOGLE_DRIVE = "gdrive"
    ONE_DRIVE = "onedrive"


@dataclass
class FileInfo:
    """Information about a file in storage.

    Attributes:
        path: Full path to the file.
        name: Filename without path.
        size_bytes: File size in bytes.
        modified_at: Last modification timestamp.
        is_directory: True if this is a directory.
        mime_type: MIME type if detectable.
        metadata: Additional backend-specific metadata.
    """

    path: str
    name: str
    size_bytes: int = 0
    modified_at: datetime = field(default_factory=datetime.now)
    is_directory: bool = False
    mime_type: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FileMetadata:
    """Detailed metadata for a file.

    Attributes:
        path: Full path to the file.
        size_bytes: File size in bytes.
        created_at: Creation timestamp.
        modified_at: Last modification timestamp.
        accessed_at: Last access timestamp (if available).
        file_hash: Content hash if computed (SHA-256).
        permissions: File permissions as integer.
        owner: Owner identifier (if available).
        mime_type: MIME type.
        additional: Backend-specific metadata.
    """

    path: str
    size_bytes: int
    modified_at: datetime
    created_at: Optional[datetime] = None
    accessed_at: Optional[datetime] = None
    file_hash: Optional[str] = None
    permissions: Optional[int] = None
    owner: Optional[str] = None
    mime_type: Optional[str] = None
    additional: Dict[str, Any] = field(default_factory=dict)


class FileChangeType(Enum):
    """Type of file change event."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    MOVED = "moved"


@dataclass
class FileChange:
    """Represents a change detected in storage.

    Attributes:
        change_type: Type of change (created, modified, deleted, moved).
        path: Path to the affected file.
        previous_path: Previous path (for moved files).
        timestamp: When the change was detected.
        file_info: Current file information (if available).
    """

    change_type: FileChangeType
    path: str
    timestamp: datetime = field(default_factory=datetime.now)
    previous_path: Optional[str] = None
    file_info: Optional[FileInfo] = None


class WatchHandle(Protocol):
    """Protocol for watch handles returned by watch() method.

    Implementations should provide a way to stop watching,
    either through explicit stop/restart methods or as a context manager.
    """

    def start(self) -> None:
        """Start or resume watching."""
        ...

    def stop(self) -> None:
        """Stop watching and release resources."""
        ...

    def is_running(self) -> bool:
        """Check if the watcher is currently active."""
        ...

    def __enter__(self) -> "WatchHandle":
        """Enter context manager (start watching)."""
        ...

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit context manager (stop watching)."""
        ...


class StorageAdapter(ABC):
    """Abstract base class for storage backend adapters.

    This interface supports local filesystem, USB drives, Google Drive,
    OneDrive, and other cloud storage backends.

    Example:
        ```python
        class LocalStorageAdapter(StorageAdapter):
            async def list_files(self, path: str, recursive: bool = False) -> List[FileInfo]:
                # Implementation
                pass
        ```
    """

    @abstractmethod
    async def list_files(self, path: str, recursive: bool = False) -> List[FileInfo]:
        """List files in a directory.

        Args:
            path: Directory path to list.
            recursive: If True, include subdirectories.

        Returns:
            List of FileInfo objects.

        Raises:
            FileNotFoundError: If path does not exist.
            PermissionError: If access is denied.
            RuntimeError: If listing operation fails.
        """
        raise NotImplementedError("Subclasses must implement list_files()")

    @abstractmethod
    async def read_file(self, path: str) -> bytes:
        """Read file contents as bytes.

        Args:
            path: Path to the file.

        Returns:
            Raw file contents as bytes.

        Raises:
            FileNotFoundError: If file does not exist.
            PermissionError: If access is denied.
            RuntimeError: If read operation fails.
        """
        raise NotImplementedError("Subclasses must implement read_file()")

    @abstractmethod
    async def get_metadata(self, path: str) -> FileMetadata:
        """Get detailed file metadata.

        Args:
            path: Path to the file or directory.

        Returns:
            FileMetadata with detailed information.

        Raises:
            FileNotFoundError: If path does not exist.
            PermissionError: If access is denied.
            RuntimeError: If metadata retrieval fails.
        """
        raise NotImplementedError("Subclasses must implement get_metadata()")

    @abstractmethod
    async def exists(self, path: str) -> bool:
        """Check if path exists.

        Args:
            path: Path to check.

        Returns:
            True if path exists, False otherwise.
        """
        raise NotImplementedError("Subclasses must implement exists()")

    @abstractmethod
    async def list_changes(
        self, since: datetime, path: Optional[str] = None
    ) -> List[FileChange]:
        """List changes since a given timestamp.

        Cloud adapters should implement this using native change tracking APIs
        (like Google Drive's changes.list or OneDrive's delta).
        Local adapters should raise NotImplementedError.

        Args:
            since: Timestamp to check changes from.
            path: Optional path to limit scope.

        Returns:
            List of FileChange objects.

        Raises:
            NotImplementedError: If the adapter does not support change tracking.
            RuntimeError: If change retrieval fails.
        """
        raise NotImplementedError(
            "Cloud adapters must implement list_changes(); Local adapters should raise"
        )

    @abstractmethod
    async def supports_watch(self) -> bool:
        """Check if native watching is supported.

        Returns:
            True if watch() can be used, False otherwise.
        """
        raise NotImplementedError("Subclasses must implement supports_watch()")

    @abstractmethod
    async def watch(
        self, path: str, callback: Callable[[FileChange], None]
    ) -> WatchHandle:
        """Watch for changes at a path.

        Only local adapters should implement this. Cloud adapters
        should use polling via list_changes() instead.

        Args:
            path: Directory path to watch.
            callback: Function called on each file change event.

        Returns:
            WatchHandle for controlling the watch session.

        Raises:
            NotImplementedError: If watching is not supported (cloud adapters).
            RuntimeError: If watch setup fails.
        """
        raise NotImplementedError(
            "Local adapters must implement watch(); Cloud adapters should raise"
        )
