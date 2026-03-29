"""Database layer for Grimoire.

Provides SQLAlchemy models, async session management, and Alembic migrations.
"""

from grimoire.db.base import Base
from grimoire.db.models import (
    ActionType,
    CacheEntry,
    CacheType,
    Category,
    Chunk,
    ContentType,
    DiscoveredBy,
    Document,
    DocumentTag,
    FileType,
    GeneratedContent,
    ProcessingLog,
    ProcessingStatus,
    Relationship,
    RelationshipType,
    StatusType,
    StorageBackend,
    TaggedBy,
    WatchPath,
)
from grimoire.db.session import (
    close_db,
    get_db,
    get_db_context,
    get_db_manager,
    initialize_db,
)

__all__ = [
    # Base
    "Base",
    # Models
    "Document",
    "Chunk",
    "Category",
    "DocumentTag",
    "GeneratedContent",
    "Relationship",
    "WatchPath",
    "ProcessingLog",
    "CacheEntry",
    # Enums
    "StorageBackend",
    "FileType",
    "ProcessingStatus",
    "ContentType",
    "RelationshipType",
    "TaggedBy",
    "DiscoveredBy",
    "ActionType",
    "StatusType",
    "CacheType",
    # Session management
    "initialize_db",
    "close_db",
    "get_db",
    "get_db_context",
    "get_db_manager",
]
