"""SQLAlchemy ORM models for Grimoire database.

All tables from DESIGN.md Section 3:
- documents: Core document metadata
- chunks: Document chunks with embeddings
- categories: Hierarchical taxonomy
- document_tags: Many-to-many junction with confidence
- generated_content: On-demand derived content
- relationships: Document-to-document links
- watch_paths: Monitored directories
- processing_log: Audit trail
- cache_entries: Result caching
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, List, Optional
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SQLEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON as BaseJSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from grimoire.db.base import Base


# Portable JSON type that works with both PostgreSQL (JSONB) and SQLite (JSON)
class PortableJSON(TypeDecorator[dict[str, Any]]):
    """Portable JSON type that uses JSONB on PostgreSQL and JSON on other backends."""

    impl = BaseJSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(BaseJSON())


# ============================================================================
# Enums
# ============================================================================


class StorageBackend(str, Enum):
    """Storage backend types."""

    LOCAL = "local"
    USB = "usb"
    RCLONE = "rclone"
    GDRIVE = "gdrive"
    ONEDRIVE = "onedrive"


class FileType(str, Enum):
    """Supported file types."""

    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    HTML = "html"
    MD = "md"
    TXT = "txt"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    OTHER = "other"


class ProcessingStatus(str, Enum):
    """Document processing status."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"


class ContentType(str, Enum):
    """Generated content types."""

    SUMMARY = "summary"
    FLASH_CARD = "flash_card"
    CLIFF_NOTES = "cliff_notes"
    OUTLINE = "outline"
    IMAGE = "image"
    EXTRACT = "extract"


class RelationshipType(str, Enum):
    """Document relationship types."""

    RELATED = "related"
    REFERENCES = "references"
    SUMMARIZES = "summarizes"
    DERIVED_FROM = "derived_from"
    SIMILAR = "similar"


class TaggedBy(str, Enum):
    """Tag source types."""

    LLM = "llm"
    USER = "user"
    RULE = "rule"


class DiscoveredBy(str, Enum):
    """Relationship discovery source."""

    LLM = "llm"
    USER = "user"
    MANUAL = "manual"


class ActionType(str, Enum):
    """Processing log action types."""

    DISCOVERED = "discovered"
    EXTRACTED = "extracted"
    CHUNKED = "chunked"
    TAGGED = "tagged"
    FAILED = "failed"


class StatusType(str, Enum):
    """Processing log status types."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class CacheType(str, Enum):
    """Cache entry types."""

    QUERY = "query"
    SEARCH = "search"
    GENERATED = "generated"


# ============================================================================
# Models
# ============================================================================


class Document(Base):
    """Core document metadata.

    Tracks document ingestion, processing status, and metadata.
    """

    __tablename__ = "documents"

    # Primary key
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    # File metadata
    source_path: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
        index=True,
        comment="URI format path to source file",
    )
    storage_backend: Mapped[StorageBackend] = mapped_column(
        SQLEnum(StorageBackend, name="storage_backend_enum"),
        nullable=False,
    )
    file_type: Mapped[FileType] = mapped_column(
        SQLEnum(FileType, name="file_type_enum"),
        nullable=False,
    )
    file_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        unique=True,
        comment="SHA-256 hash of file content",
    )
    title: Mapped[Optional[str]] = mapped_column(
        String(512),
        nullable=True,
    )
    size_bytes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Processing status
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        SQLEnum(ProcessingStatus, name="processing_status_enum"),
        default=ProcessingStatus.PENDING,
        nullable=False,
        index=True,
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
        comment="Conflict detection version",
    )

    # Relationships
    chunks: Mapped[List["Chunk"]] = relationship(
        "Chunk",
        back_populates="document",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    tags: Mapped[List["DocumentTag"]] = relationship(
        "DocumentTag",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    generated_content: Mapped[List["GeneratedContent"]] = relationship(
        "GeneratedContent",
        back_populates="document",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    source_relationships: Mapped[List["Relationship"]] = relationship(
        "Relationship",
        foreign_keys="Relationship.source_document_id",
        back_populates="source_document",
        cascade="all, delete-orphan",
    )
    target_relationships: Mapped[List["Relationship"]] = relationship(
        "Relationship",
        foreign_keys="Relationship.target_document_id",
        back_populates="target_document",
        cascade="all, delete-orphan",
    )
    processing_logs: Mapped[List["ProcessingLog"]] = relationship(
        "ProcessingLog",
        back_populates="document",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_documents_status_created", "processing_status", "created_at"),
    )


class Chunk(Base):
    """Document chunks with embeddings.

    Stores text chunks with their embeddings and maintains continuity
    links for context restoration during retrieval.
    """

    __tablename__ = "chunks"

    # Primary key
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    # Foreign key to document
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Chunk metadata
    chunk_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Position within document (0-indexed)",
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    token_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    vector_id: Mapped[Optional[str]] = mapped_column(
        String(256),
        nullable=True,
        comment="ChromaDB/Qdrant vector reference",
    )
    embedding_model: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        comment="Which model generated the embedding",
    )

    # Continuity tracking (self-referential)
    prev_chunk_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("chunks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    next_chunk_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("chunks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )

    # Relationships
    document: Mapped["Document"] = relationship("Document", back_populates="chunks")
    prev_chunk: Mapped[Optional["Chunk"]] = relationship(
        "Chunk",
        remote_side="Chunk.id",
        foreign_keys=[prev_chunk_id],
        post_update=True,
    )
    next_chunk: Mapped[Optional["Chunk"]] = relationship(
        "Chunk",
        remote_side="Chunk.id",
        foreign_keys=[next_chunk_id],
        post_update=True,
    )

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_doc_idx"),
        Index("ix_chunks_doc_idx", "document_id", "chunk_index"),
    )


class Category(Base):
    """Hierarchical taxonomy categories.

    Supports self-referential parent-child relationships for
    unlimited depth hierarchies.
    """

    __tablename__ = "categories"

    # Primary key
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    # Category metadata
    name: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        unique=True,
        comment="URL-safe identifier",
    )

    # Self-referential parent (null = root category)
    parent_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    color: Mapped[Optional[str]] = mapped_column(
        String(7),
        nullable=True,
        comment="Hex color code (e.g., #3498db)",
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )

    # Relationships
    parent: Mapped[Optional["Category"]] = relationship(
        "Category",
        remote_side="Category.id",
        back_populates="children",
    )
    children: Mapped[List["Category"]] = relationship(
        "Category",
        back_populates="parent",
        cascade="all, delete-orphan",
    )
    document_tags: Mapped[List["DocumentTag"]] = relationship(
        "DocumentTag",
        back_populates="category",
        cascade="all, delete-orphan",
    )


class DocumentTag(Base):
    """Many-to-many junction for document categories.

    Includes confidence score and tagging source for tracking
    how tags were assigned (LLM, user, or rule-based).
    """

    __tablename__ = "document_tags"

    # Composite primary key
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    category_id: Mapped[str] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Tag metadata
    confidence: Mapped[float] = mapped_column(
        Float,
        default=1.0,
        nullable=False,
        comment="Confidence score 0.0-1.0",
    )
    tagged_by: Mapped[TaggedBy] = mapped_column(
        SQLEnum(TaggedBy, name="tagged_by_enum"),
        default=TaggedBy.LLM,
        nullable=False,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )

    # Relationships
    document: Mapped["Document"] = relationship("Document", back_populates="tags")
    category: Mapped["Category"] = relationship(
        "Category", back_populates="document_tags"
    )


class GeneratedContent(Base):
    """On-demand derived content.

    Stores AI-generated summaries, flashcards, and other content
    created on-demand with caching support.
    """

    __tablename__ = "generated_content"

    # Primary key
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    # Foreign key to document
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Content metadata
    content_type: Mapped[ContentType] = mapped_column(
        SQLEnum(ContentType, name="content_type_enum"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    model_used: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Model identifier (e.g., llama3:8b)",
    )
    generation_params: Mapped[Optional[dict[str, Any]]] = mapped_column(
        PortableJSON,
        nullable=True,
        comment="JSON of temperature, tokens, etc.",
    )

    # Cache tracking
    cache_hit: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )

    # Relationships
    document: Mapped["Document"] = relationship(
        "Document",
        back_populates="generated_content",
    )

    __table_args__ = (
        Index("ix_generated_content_type", "document_id", "content_type"),
    )


class Relationship(Base):
    """Document-to-document relationships.

    Represents semantic links between documents discovered by
    the system or manually created by users.
    """

    __tablename__ = "relationships"

    # Primary key
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    # Bidirectional document references
    source_document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationship metadata
    relationship_type: Mapped[RelationshipType] = mapped_column(
        SQLEnum(RelationshipType, name="relationship_type_enum"),
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(
        Float,
        default=1.0,
        nullable=False,
    )
    discovered_by: Mapped[DiscoveredBy] = mapped_column(
        SQLEnum(DiscoveredBy, name="discovered_by_enum"),
        nullable=False,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )

    # Relationships
    source_document: Mapped["Document"] = relationship(
        "Document",
        foreign_keys=[source_document_id],
        back_populates="source_relationships",
    )
    target_document: Mapped["Document"] = relationship(
        "Document",
        foreign_keys=[target_document_id],
        back_populates="target_relationships",
    )

    __table_args__ = (
        UniqueConstraint(
            "source_document_id",
            "target_document_id",
            "relationship_type",
            name="uq_relationship",
        ),
        Index(
            "ix_relationships_source_type",
            "source_document_id",
            "relationship_type",
        ),
    )


class WatchPath(Base):
    """Monitored directories for file watching.

    Tracks paths being watched with their backend type and
    polling configuration.
    """

    __tablename__ = "watch_paths"

    # Primary key
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    # Watch configuration
    path: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
        comment="URI format path",
    )
    storage_backend: Mapped[StorageBackend] = mapped_column(
        SQLEnum(StorageBackend, name="storage_backend_enum"),
        nullable=False,
    )
    recursive: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    # Polling (for cloud backends)
    last_scanned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    poll_interval_seconds: Mapped[int] = mapped_column(
        Integer,
        default=300,
        nullable=False,
        comment="Cloud polling interval (0 = disable)",
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "path", "storage_backend", name="uq_watch_paths_path_storage_backend"
        ),
    )


class ProcessingLog(Base):
    """Audit trail for document processing.

    Tracks all actions performed on documents including
    discovery, extraction, chunking, tagging, and failures.
    """

    __tablename__ = "processing_log"

    # Primary key
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    # Foreign key to document
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Log entry
    action: Mapped[ActionType] = mapped_column(
        SQLEnum(ActionType, name="action_type_enum"),
        nullable=False,
    )
    status: Mapped[StatusType] = mapped_column(
        SQLEnum(StatusType, name="status_type_enum"),
        nullable=False,
    )
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(
        PortableJSON,
        nullable=True,
        comment="JSON with action-specific details",
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Processing duration in milliseconds",
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
        index=True,
    )

    # Relationships
    document: Mapped["Document"] = relationship(
        "Document",
        back_populates="processing_logs",
    )

    __table_args__ = (Index("ix_processing_log_doc_action", "document_id", "action"),)


class CacheEntry(Base):
    """Result caching for queries and generated content.

    Provides time-based cache expiration with hit tracking
    for cache analytics.
    """

    __tablename__ = "cache_entries"

    # Primary key
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    # Cache key
    cache_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        unique=True,
        index=True,
        comment="Hash of query params",
    )
    cache_type: Mapped[CacheType] = mapped_column(
        SQLEnum(CacheType, name="cache_type_enum"),
        nullable=False,
        index=True,
    )

    # Cached data
    data: Mapped[dict[str, Any]] = mapped_column(
        PortableJSON,
        nullable=False,
    )

    # Expiration and stats
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    hit_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )
