"""Pydantic schemas for API request/response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# =============================================================================
# Ingest Schemas
# =============================================================================


class IngestFileRequest(BaseModel):
    """Request to ingest a single file."""

    file_path: str = Field(..., description="Path to the file to ingest.")
    auto_tag: bool = Field(default=True, description="Auto-tag with LLM.")
    storage_backend: str | None = Field(default=None, description="Storage backend override.")


class IngestDirectoryRequest(BaseModel):
    """Request to ingest a directory."""

    directory: str = Field(..., description="Path to the directory.")
    recursive: bool = Field(default=True, description="Recurse into subdirectories.")
    auto_tag: bool = Field(default=True, description="Auto-tag with LLM.")
    storage_backend: str | None = Field(default=None, description="Storage backend override.")


class IngestResultResponse(BaseModel):
    """Result of ingesting a single file."""

    file_path: str
    document_id: str | None = None
    status: str = "completed"
    chunks_created: int = 0
    vectors_stored: int = 0
    tags_applied: int = 0
    error_message: str | None = None
    duration_ms: int = 0


class BatchIngestResponse(BaseModel):
    """Result of ingesting a directory."""

    total: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[IngestResultResponse] = Field(default_factory=list)
    duration_ms: int = 0


# =============================================================================
# Query Schemas
# =============================================================================


class QueryRequest(BaseModel):
    """Request to ask a question (RAG pipeline)."""

    query: str = Field(..., description="The question to answer.")
    top_k: int = Field(default=5, ge=1, le=100, description="Number of source chunks.")
    filter_dict: dict[str, Any] | None = Field(default=None, description="Metadata filters.")
    use_cache: bool = Field(default=True, description="Use cached results.")


class CitationResponse(BaseModel):
    """Citation in a query result."""

    document_id: str
    document_title: str | None = None
    chunk_id: str
    chunk_index: int | None = None
    content_snippet: str = ""
    relevance_score: float = 0.0


class QueryResponse(BaseModel):
    """Response to a query."""

    query: str
    answer: str = ""
    citations: list[CitationResponse] = Field(default_factory=list)
    model_used: str = ""
    search_results_count: int = 0
    cached: bool = False
    duration_ms: int = 0


class SearchRequest(BaseModel):
    """Request to search without answer generation."""

    query: str = Field(..., description="Search query.")
    top_k: int = Field(default=10, ge=1, le=100, description="Number of results.")
    filter_dict: dict[str, Any] | None = Field(default=None, description="Metadata filters.")


class SearchResultItem(BaseModel):
    """Single search result."""

    chunk_id: str = ""
    document_id: str = ""
    document_title: str | None = None
    content: str = ""
    score: float = 0.0


class SearchResponse(BaseModel):
    """Response to a search."""

    query: str
    results: list[SearchResultItem] = Field(default_factory=list)
    total_results: int = 0
    duration_ms: int = 0


# =============================================================================
# Generation Schemas
# =============================================================================


class GenerateRequest(BaseModel):
    """Request to generate content."""

    document_ids: list[str] = Field(..., min_length=1, description="Document IDs.")
    content_type: str = Field(..., description="Type: summary, flash_card, cliff_notes, outline, extract.")
    style: str | None = Field(default=None, description="Style for summaries.")
    count: int = Field(default=10, ge=1, le=100, description="Count for flashcards.")
    query: str | None = Field(default=None, description="Query for extracts.")


class GenerateResponse(BaseModel):
    """Response with generated content."""

    content: str = ""
    content_type: str = ""
    document_ids: list[str] = Field(default_factory=list)
    model_used: str = ""
    cached: bool = False
    generation_id: str | None = None
    duration_ms: int = 0


# =============================================================================
# Document Schemas
# =============================================================================


class DocumentResponse(BaseModel):
    """Document summary."""

    id: str
    title: str | None = None
    source_path: str
    file_type: str
    storage_backend: str
    processing_status: str
    size_bytes: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    tag_count: int = 0
    chunk_count: int = 0


class DocumentListResponse(BaseModel):
    """Paginated list of documents."""

    documents: list[DocumentResponse] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 50


class DocumentDetailResponse(DocumentResponse):
    """Full document detail with tags and chunks."""

    tags: list[str] = Field(default_factory=list)
    error_message: str | None = None


# =============================================================================
# Category Schemas
# =============================================================================


class CategoryCreateRequest(BaseModel):
    """Request to create a category."""

    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="")
    parent_slug: str | None = Field(default=None, description="Parent category slug.")
    color: str = Field(default="#3498db")


class CategoryResponse(BaseModel):
    """Category data."""

    id: str
    name: str
    slug: str
    description: str = ""
    parent_id: str | None = None
    color: str = "#3498db"


class CategoryListResponse(BaseModel):
    """List of categories."""

    categories: list[CategoryResponse] = Field(default_factory=list)
    total: int = 0


# =============================================================================
# Watch Schemas
# =============================================================================


class WatchStartRequest(BaseModel):
    """Request to start watching a path."""

    path: str = Field(..., description="Path to watch.")
    backend: str = Field(default="local", description="Storage backend.")
    recursive: bool = Field(default=True)
    poll_interval: int | None = Field(default=None, description="Poll interval for cloud backends.")


class WatchResponse(BaseModel):
    """Watch status."""

    watch_id: str
    path: str
    backend: str
    is_running: bool = True


class WatcherStatsResponse(BaseModel):
    """Watcher aggregate stats."""

    active_watches: int = 0
    total_files_processed: int = 0
    total_files_failed: int = 0
    watches: list[WatchResponse] = Field(default_factory=list)


# =============================================================================
# Status Schemas
# =============================================================================


class StatusResponse(BaseModel):
    """System status."""

    documents: int = 0
    categories: int = 0
    chunks: int = 0
    generated_content: int = 0
    status_breakdown: dict[str, int] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """Standard error response."""

    detail: str


# =============================================================================
# API Key Schemas
# =============================================================================


class ApiKeyInfoResponse(BaseModel):
    """Info about the currently authenticated API key."""

    id: str
    name: str
    tier: str
    prefix: str
    expires_at: str | None = None
    created_at: str


class ApiKeyCreateResponse(BaseModel):
    """Response when creating an API key (includes the raw key once)."""

    id: str
    name: str
    tier: str
    prefix: str
    key: str
    expires_at: str | None = None
