"""Local filesystem storage adapter using watchdog for monitoring."""

import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Union

from loguru import logger

from grimoire.storage.base import (
    FileChange,
    FileChangeType,
    FileInfo,
    FileMetadata,
    StorageAdapter,
    WatchHandle,
)


class WatchdogWatchHandle(WatchHandle):
    """WatchHandle implementation using watchdog Observer.

    This class wraps a watchdog Observer instance and provides
    the WatchHandle protocol for managing filesystem watchers.
    """

    def __init__(self, observer: object, watched_path: str) -> None:
        """Initialize watch handle.

        Args:
            observer: The watchdog Observer instance.
            watched_path: The path being watched.
        """
        self._observer: object = observer
        self._watched_path: str = watched_path
        self._running: bool = False

    def start(self) -> None:
        """Start watching."""
        if not self._running:
            self._observer.start()  # type: ignore[attr-defined]
            self._running = True
            logger.debug(f"Started watching: {self._watched_path}")

    def stop(self) -> None:
        """Stop watching and release resources."""
        if self._running:
            self._observer.stop()  # type: ignore[attr-defined]
            self._observer.join(timeout=5.0)  # type: ignore[attr-defined]
            self._running = False
            logger.debug(f"Stopped watching: {self._watched_path}")

    def is_running(self) -> bool:
        """Check if the watcher is currently active."""
        return self._running and self._observer.is_alive()  # type: ignore[attr-defined]

    def __enter__(self) -> "WatchdogWatchHandle":
        """Enter context manager (start watching)."""
        self.start()
        return self

    def __exit__(self, exc_type: Optional[type], 
                 exc_val: Optional[BaseException], 
                 exc_tb: Optional[object]) -> None:
        """Exit context manager (stop watching)."""
        self.stop()


# At runtime, _WatchdogEventHandler will inherit from FileSystemEventHandler
# This is set up at module import time to ensure dispatch method is available
from watchdog.events import FileSystemEventHandler


class _WatchdogEventHandler(FileSystemEventHandler):
    """Internal event handler that converts watchdog events to FileChange.

    Note: This class inherits from FileSystemEventHandler at runtime
    to ensure proper dispatch behavior.
    """

    def __init__(self, callback: Callable[[FileChange], None]):
        """Initialize with callback.

        Args:
            callback: Function to call with FileChange on each event.
        """
        self._callback: Callable[[FileChange], None] = callback

    def _create_file_info(self, path: str) -> Optional[FileInfo]:
        """Create FileInfo from filesystem path if file exists."""
        try:
            if os.path.isfile(path):
                stat = os.stat(path)
                mime_type: Optional[str] = _get_mime_type(path)
                return FileInfo(
                    path=path,
                    name=os.path.basename(path),
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime),
                    is_directory=False,
                    mime_type=mime_type,
                    metadata={"permissions": stat.st_mode},
                )
        except (OSError, PermissionError) as e:
            logger.warning(f"Failed to get file info for {path}: {e}")
        return None

    def _to_str(self, path: Union[str, bytes]) -> str:
        """Convert path to string, handling bytes type."""
        if isinstance(path, bytes):
            return path.decode("utf-8", errors="surrogateescape")
        return path

    def on_created(self, event: object) -> None:
        """Handle file/directory creation event."""
        if hasattr(event, 'is_directory') and hasattr(event, 'src_path'):
            src_path: str = self._to_str(event.src_path)
            is_dir: bool = bool(event.is_directory)
            change = FileChange(
                change_type=FileChangeType.CREATED,
                path=src_path,
                timestamp=datetime.now(),
                file_info=self._create_file_info(src_path) if not is_dir else None,
            )
            try:
                self._callback(change)
            except Exception as e:
                logger.error(f"Error in watch callback for created event: {e}")

    def on_modified(self, event: object) -> None:
        """Handle file modification event."""
        if hasattr(event, 'is_directory'):
            is_dir = bool(event.is_directory)
            if is_dir:
                return  # Skip directory modification events
        
        if hasattr(event, 'src_path'):
            src_path = self._to_str(event.src_path)
            change = FileChange(
                change_type=FileChangeType.MODIFIED,
                path=src_path,
                timestamp=datetime.now(),
                file_info=self._create_file_info(src_path),
            )
            try:
                self._callback(change)
            except Exception as e:
                logger.error(f"Error in watch callback for modified event: {e}")

    def on_deleted(self, event: object) -> None:
        """Handle file/directory deletion event."""
        if hasattr(event, 'src_path'):
            src_path = self._to_str(event.src_path)
            change = FileChange(
                change_type=FileChangeType.DELETED,
                path=src_path,
                timestamp=datetime.now(),
            )
            try:
                self._callback(change)
            except Exception as e:
                logger.error(f"Error in watch callback for deleted event: {e}")

    def on_moved(self, event: object) -> None:
        """Handle file/directory move event."""
        if hasattr(event, 'src_path'):
            src_path = self._to_str(event.src_path)
            dest_path: Optional[str] = None
            is_dir = False
            if hasattr(event, 'dest_path'):
                raw_dest = event.dest_path
                if isinstance(raw_dest, (str, bytes)):
                    dest_path = self._to_str(raw_dest)
            if hasattr(event, 'is_directory'):
                is_dir = bool(event.is_directory)
            
            change = FileChange(
                change_type=FileChangeType.MOVED,
                path=dest_path if dest_path else src_path,
                previous_path=src_path,
                timestamp=datetime.now(),
                file_info=self._create_file_info(dest_path) if dest_path and not is_dir else None,
            )
            try:
                self._callback(change)
            except Exception as e:
                logger.error(f"Error in watch callback for moved event: {e}")

    def on_any_event(self, event: object) -> None:
        """Catch-all handler - not used directly."""
        pass


def _get_mime_type(path: str) -> Optional[str]:
    """Guess MIME type from file extension.

    Args:
        path: File path.

    Returns:
        MIME type string or None.
    """
    import mimetypes
    mime_type: Optional[str] = mimetypes.guess_type(path)[0]
    return mime_type


def _compute_file_hash(path: str, block_size: int = 65536) -> Optional[str]:
    """Compute SHA-256 hash of file contents.

    Args:
        path: Path to the file.
        block_size: Size of blocks to read.

    Returns:
        Hexadecimal hash string or None on error.
    """
    try:
        sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                block = f.read(block_size)
                if not block:
                    break
                sha256.update(block)
        return sha256.hexdigest()
    except (OSError, PermissionError) as e:
        logger.warning(f"Failed to compute hash for {path}: {e}")
        return None


class LocalStorageAdapter(StorageAdapter):
    """Storage adapter for local filesystem and USB drives.

    This adapter provides access to files on the local filesystem
    using standard Python file operations and watchdog for monitoring.

    Example:
        ```python
        adapter = LocalStorageAdapter()
        files = await adapter.list_files("/home/user/documents")
        data = await adapter.read_file("/home/user/documents/file.txt")
        ```
    """

    def __init__(self) -> None:
        """Initialize the local storage adapter."""
        self._observers: List[object] = []
        logger.debug("LocalStorageAdapter initialized")

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
        files: List[FileInfo] = []
        target_path = Path(path)

        if not target_path.exists():
            logger.error(f"Path does not exist: {path}")
            raise FileNotFoundError(f"Path does not exist: {path}")

        if not target_path.is_dir():
            logger.error(f"Path is not a directory: {path}")
            raise NotADirectoryError(f"Path is not a directory: {path}")

        try:
            if recursive:
                for item in target_path.rglob("*"):
                    if item.is_file():
                        try:
                            stat = item.stat()
                            mime_type: Optional[str] = _get_mime_type(str(item))
                            files.append(
                                FileInfo(
                                    path=str(item),
                                    name=item.name,
                                    size_bytes=stat.st_size,
                                    modified_at=datetime.fromtimestamp(stat.st_mtime),
                                    is_directory=False,
                                    mime_type=mime_type,
                                    metadata={"permissions": stat.st_mode},
                                )
                            )
                        except (OSError, PermissionError) as e:
                            logger.warning(f"Skipping inaccessible file {item}: {e}")
            else:
                for item in target_path.iterdir():
                    if item.is_file():
                        try:
                            stat = item.stat()
                            mime_type = _get_mime_type(str(item))
                            files.append(
                                FileInfo(
                                    path=str(item),
                                    name=item.name,
                                    size_bytes=stat.st_size,
                                    modified_at=datetime.fromtimestamp(stat.st_mtime),
                                    is_directory=False,
                                    mime_type=mime_type,
                                    metadata={"permissions": stat.st_mode},
                                )
                            )
                        except (OSError, PermissionError) as e:
                            logger.warning(f"Skipping inaccessible file {item}: {e}")

        except PermissionError as e:
            logger.error(f"Permission denied accessing {path}: {e}")
            raise
        except OSError as e:
            logger.error(f"Error listing directory {path}: {e}")
            raise RuntimeError(f"Error listing directory {path}: {e}") from e

        logger.debug(f"Listed {len(files)} files in {path} (recursive={recursive})")
        return files

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
        target_path = Path(path)

        if not target_path.exists():
            logger.error(f"File not found: {path}")
            raise FileNotFoundError(f"File not found: {path}")

        if not target_path.is_file():
            logger.error(f"Path is not a file: {path}")
            raise IsADirectoryError(f"Path is not a file: {path}")

        try:
            with open(target_path, "rb") as f:
                data: bytes = f.read()
            logger.debug(f"Read {len(data)} bytes from {path}")
            return data
        except PermissionError as e:
            logger.error(f"Permission denied reading {path}: {e}")
            raise
        except OSError as e:
            logger.error(f"Error reading file {path}: {e}")
            raise RuntimeError(f"Error reading file {path}: {e}") from e

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
        target_path = Path(path)

        if not target_path.exists():
            logger.error(f"Path does not exist: {path}")
            raise FileNotFoundError(f"Path does not exist: {path}")

        try:
            stat = target_path.stat()
            mime_type: Optional[str] = _get_mime_type(path) if target_path.is_file() else None
            file_hash: Optional[str] = None
            if target_path.is_file():
                file_hash = _compute_file_hash(str(target_path))

            metadata = FileMetadata(
                path=str(target_path.absolute()),
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime),
                created_at=datetime.fromtimestamp(stat.st_ctime),
                accessed_at=datetime.fromtimestamp(stat.st_atime),
                file_hash=file_hash,
                permissions=stat.st_mode,
                mime_type=mime_type,
                additional={"nlink": stat.st_nlink},
            )
            logger.debug(f"Retrieved metadata for {path}")
            return metadata

        except PermissionError as e:
            logger.error(f"Permission denied accessing metadata for {path}: {e}")
            raise
        except OSError as e:
            logger.error(f"Error getting metadata for {path}: {e}")
            raise RuntimeError(f"Error getting metadata for {path}: {e}") from e

    async def exists(self, path: str) -> bool:
        """Check if path exists.

        Args:
            path: Path to check.

        Returns:
            True if path exists, False otherwise.
        """
        exists = Path(path).exists()
        logger.debug(f"Path {path} exists: {exists}")
        return exists

    async def list_changes(self, since: datetime, path: Optional[str] = None) -> List[FileChange]:
        """List changes since a given timestamp.

        Local adapters do not support change tracking via polling.
        This method raises NotImplementedError.

        Args:
            since: Timestamp to check changes from.
            path: Optional path to limit scope.

        Returns:
            List of FileChange objects.

        Raises:
            NotImplementedError: Always raised for local adapters.
        """
        raise NotImplementedError(
            "Local adapters do not support list_changes(). "
            "Use watch() for filesystem monitoring."
        )

    async def supports_watch(self) -> bool:
        """Check if native watching is supported.

        Returns:
            True as local filesystem supports native watching via watchdog.
        """
        return True

    async def watch(
        self, path: str, callback: Callable[[FileChange], None]
    ) -> WatchHandle:
        """Watch for changes at a path.

        Uses watchdog to monitor the directory for changes.
        Events are converted to FileChange objects and passed to the callback.

        Args:
            path: Directory path to watch.
            callback: Function called on each file change event.

        Returns:
            WatchHandle for controlling the watch session.

        Raises:
            FileNotFoundError: If path does not exist.
            NotADirectoryError: If path is not a directory.
            RuntimeError: If watch setup fails.
        """
        # Import watchdog classes at runtime
        from watchdog.observers import Observer
        
        target_path = Path(path)

        if not target_path.exists():
            logger.error(f"Cannot watch non-existent path: {path}")
            raise FileNotFoundError(f"Path does not exist: {path}")

        if not target_path.is_dir():
            logger.error(f"Cannot watch file (must be directory): {path}")
            raise NotADirectoryError(f"Path is not a directory: {path}")

        try:
            observer = Observer()
            event_handler = _WatchdogEventHandler(callback)
            observer.schedule(event_handler, str(target_path), recursive=True)
            
            handle = WatchdogWatchHandle(observer, str(target_path))
            self._observers.append(observer)
            logger.info(f"Started watching directory: {path}")
            return handle

        except OSError as e:
            logger.error(f"Failed to setup watcher for {path}: {e}")
            raise RuntimeError(f"Failed to setup watcher for {path}: {e}") from e
