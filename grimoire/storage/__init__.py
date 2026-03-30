"""Storage adapters for Grimoire."""

from grimoire.storage.base import (
    FileChange,
    FileChangeType,
    FileInfo,
    FileMetadata,
    StorageAdapter,
    StorageBackend,
    WatchHandle,
)
from grimoire.storage.watch_manager import (
    ActiveWatch,
    CloudStoragePoller,
    WatchConfig,
    WatchManager,
    WatchType,
)

__all__ = [
    "StorageAdapter",
    "StorageBackend",
    "FileInfo",
    "FileMetadata",
    "FileChange",
    "FileChangeType",
    "WatchHandle",
    "WatchManager",
    "WatchConfig",
    "WatchType",
    "ActiveWatch",
    "CloudStoragePoller",
]
