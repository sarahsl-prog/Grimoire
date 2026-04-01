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

try:
    from grimoire.storage.gdrive import (
        AuthenticationError,
        GoogleDriveAdapter,
        GoogleDriveError,
        RateLimitError,
        TokenRefreshError,
    )
except ImportError:
    pass

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
