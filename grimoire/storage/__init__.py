"""Storage adapters for Grimoire."""

import contextlib

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

with contextlib.suppress(ImportError):
    from grimoire.storage.gdrive import (
        AuthenticationError,
        GoogleDriveAdapter,
        GoogleDriveError,
        RateLimitError,
        TokenRefreshError,
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
    "GoogleDriveAdapter",
    "GoogleDriveError",
    "AuthenticationError",
    "RateLimitError",
    "TokenRefreshError",
]
