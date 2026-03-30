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
    "FileChange",
    "FileChangeType",
    "FileInfo",
    "FileMetadata",
    "StorageAdapter",
    "StorageBackend",
    "WatchHandle",
    "GoogleDriveAdapter",
    "GoogleDriveError",
    "AuthenticationError",
    "RateLimitError",
    "TokenRefreshError",
]
