"""MCP tool definitions for Grimoire.

Every tool delegates to the existing Grimoire service layer (agents,
repositories, etc.) rather than talking to ChromaDB or Postgres directly.
This keeps the MCP surface aligned with the REST API and ensures that
chunking, embedding, caching, and tagging all happen exactly as they do
for HTTP clients.

Tier-based access control:
- READ  (rdl): search, ask, get, list, status, read-only queries
- DEV   (dvl): everything READ has + ingest, generate, create, watch start
- AGENT (agt): everything DEV has + delete, watch stop
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from loguru import logger
from mcp.server.fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field, field_validator

from grimoire.api.dependencies import (
    get_content_gen_agent,
    get_db_session,
    get_ingestion_agent,
    get_query_agent,
)
from grimoire.api.schemas import (
    CategoryCreateRequest,
    DocumentListResponse,
    GenerateRequest,
    IngestDirectoryRequest,
    IngestFileRequest,
    QueryRequest,
    SearchRequest,
    WatchStartRequest,
)
from grimoire.cli.helpers import build_watcher
from grimoire.config.settings import get_settings
from grimoire.core.embedder import EmbedderFactory
from grimoire.db.models import ApiKeyTier, ProcessingStatus
from grimoire.db.session import get_db_context

from .auth_stdio import require_tier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(data: Any) -> str:
    """Serialize a successful result to JSON."""
    return json.dumps({"status": "ok", "data": data}, indent=2, default=str)


def _err(message: str, hint: Optional[str] = None) -> str:
    """Serialize an error result with an optional actionable hint."""
    payload: Dict[str, str] = {"status": "error", "message": message}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------


class SearchInput(BaseModel):
    """Parameters for grimoire_search."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Natural language query.", min_length=1, max_length=2000)
    top_k: int = Field(default=10, ge=1, le=100, description="Number of results to return.")
    filter_dict: Optional[Dict[str, Any]] = Field(
        default=None, description="Optional metadata filters."
    )


class AskInput(BaseModel):
    """Parameters for grimoire_ask."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Question to answer.", min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=100, description="Number of source chunks.")
    filter_dict: Optional[Dict[str, Any]] = Field(default=None)
    use_cache: bool = Field(default=True)


class IngestFileInput(BaseModel):
    """Parameters for grimoire_ingest_file."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    file_path: str = Field(..., description="Absolute path to the file.", min_length=1)
    auto_tag: bool = Field(default=True)

    @field_validator("file_path")
    @classmethod
    def _resolve_path(cls, v: str) -> str:
        return os.path.expanduser(v)


class IngestDirectoryInput(BaseModel):
    """Parameters for grimoire_ingest_directory."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    directory: str = Field(..., description="Absolute path to the directory.", min_length=1)
    recursive: bool = Field(default=True)
    auto_tag: bool = Field(default=True)

    @field_validator("directory")
    @classmethod
    def _resolve_path(cls, v: str) -> str:
        return os.path.expanduser(v)


class GenerateInput(BaseModel):
    """Parameters for grimoire_generate."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    document_ids: List[str] = Field(..., min_length=1)
    content_type: str = Field(..., description="summary | flash_card | cliff_notes | outline | extract")
    style: Optional[str] = Field(default=None)
    count: int = Field(default=10, ge=1, le=100)
    query: Optional[str] = Field(default=None)


class DocumentIdInput(BaseModel):
    """Parameters for grimoire_get_document."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    document_id: str = Field(..., description="UUID of the document.")


class ListDocumentsInput(BaseModel):
    """Parameters for grimoire_list_documents."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)
    status: Optional[str] = Field(default=None)
    file_type: Optional[str] = Field(default=None)
    source_type: Optional[str] = Field(default=None)
    severity: Optional[str] = Field(default=None)
    cve_id: Optional[str] = Field(default=None)
    mitre_technique_id: Optional[str] = Field(default=None)


class CreateCategoryInput(BaseModel):
    """Parameters for grimoire_create_category."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="")
    parent_slug: Optional[str] = Field(default=None)
    color: str = Field(default="#3498db")


class WatchStartInput(BaseModel):
    """Parameters for grimoire_watch_start."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(..., min_length=1)
    backend: str = Field(default="local")
    recursive: bool = Field(default=True)


class DeleteDocumentInput(BaseModel):
    """Parameters for grimoire_delete_document."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    document_id: str = Field(...)


class PgQueryInput(BaseModel):
    """Parameters for grimoire_pg_query."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    sql: str = Field(..., min_length=1, max_length=4000, description="SELECT query only.")
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("sql")
    @classmethod
    def _must_be_select(cls, v: str) -> str:
        stripped = v.strip().upper()
        if not stripped.startswith("SELECT"):
            raise ValueError("Only SELECT queries are permitted.")
        return v


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def grimoire_search(params: SearchInput, ctx: Context) -> str:
    """Semantic search of the Grimoire knowledge base (no answer generation)."""
    agent = get_query_agent()
    async with get_db_context() as db:
        result = await agent.search(
            db,
            params.query,
            top_k=params.top_k,
            filter_dict=params.filter_dict,
        )
    return _ok({
        "query": result.query,
        "results": [r.model_dump() for r in result.results],
        "total_results": result.total_results,
        "duration_ms": result.duration_ms,
    })


async def grimoire_ask(params: AskInput, ctx: Context) -> str:
    """Ask a question and receive a generated answer with citations."""
    agent = get_query_agent()
    async with get_db_context() as db:
        result = await agent.query(
            db,
            params.query,
            top_k=params.top_k,
            filter_dict=params.filter_dict,
            use_cache=params.use_cache,
        )
    return _ok({
        "query": result.query,
        "answer": result.answer,
        "citations": [c.model_dump() for c in result.citations],
        "model_used": result.model_used,
        "cached": result.cached,
        "duration_ms": result.duration_ms,
    })


async def grimoire_ingest_file(params: IngestFileInput, ctx: Context) -> str:
    """Ingest a single file into the knowledge base.  Requires DEV tier or higher."""
    require_tier(ApiKeyTier.DEV, ApiKeyTier.AGENT)
    agent = get_ingestion_agent()
    async with get_db_context() as db:
        result = await agent.ingest_file(db, params.file_path, auto_tag=params.auto_tag)
    return _ok(result.model_dump())


async def grimoire_ingest_directory(params: IngestDirectoryInput, ctx: Context) -> str:
    """Ingest all supported files from a directory.  Requires DEV tier or higher."""
    require_tier(ApiKeyTier.DEV, ApiKeyTier.AGENT)
    agent = get_ingestion_agent()
    async with get_db_context() as db:
        result = await agent.ingest_directory(
            db, params.directory, recursive=params.recursive, auto_tag=params.auto_tag
        )
    return _ok(result.model_dump())


async def grimoire_generate(params: GenerateInput, ctx: Context) -> str:
    """Generate content (summary, flash cards, cliff notes, outline, extract)
    from selected documents.  Requires DEV tier or higher."""
    require_tier(ApiKeyTier.DEV, ApiKeyTier.AGENT)
    agent = get_content_gen_agent()
    from grimoire.db.models import ContentType

    try:
        ct = ContentType(params.content_type)
    except ValueError:
        return _err(f"Invalid content_type: {params.content_type}")

    async with get_db_context() as db:
        if ct == ContentType.SUMMARY:
            result = await agent.generate_summary(db, params.document_ids, style=params.style or "concise")
        elif ct == ContentType.FLASH_CARD:
            result = await agent.generate_flash_cards(db, params.document_ids, count=params.count)
        elif ct == ContentType.CLIFF_NOTES:
            result = await agent.generate_cliff_notes(db, params.document_ids)
        elif ct == ContentType.OUTLINE:
            result = await agent.generate_outline(db, params.document_ids)
        elif ct == ContentType.EXTRACT:
            if not params.query:
                return _err("'query' is required for extract generation.")
            result = await agent.generate_extract(db, params.document_ids, query=params.query)
        else:
            return _err(f"Unsupported content type: {params.content_type}")

    return _ok(result.model_dump())


async def grimoire_get_document(params: DocumentIdInput, ctx: Context) -> str:
    """Retrieve detailed information about a document by ID."""
    async with get_db_context() as db:
        from sqlalchemy import select
        from grimoire.db.models import Document

        stmt = select(Document).where(Document.id == params.document_id)
        result = await db.execute(stmt)
        doc = result.scalar_one_or_none()
        if doc is None:
            return _err(f"Document '{params.document_id}' not found.")

        data = {
            "id": doc.id,
            "title": doc.title,
            "source_path": doc.source_path,
            "file_type": doc.file_type.value if hasattr(doc.file_type, "value") else str(doc.file_type),
            "storage_backend": doc.storage_backend.value if hasattr(doc.storage_backend, "value") else str(doc.storage_backend),
            "processing_status": doc.processing_status.value if hasattr(doc.processing_status, "value") else str(doc.processing_status),
            "size_bytes": doc.size_bytes,
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
            "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
            "tags": [t.category.name for t in doc.tags if t.category],
            "chunks": len(doc.chunks),
        }
    return _ok(data)


async def grimoire_list_documents(params: ListDocumentsInput, ctx: Context) -> str:
    """List documents with optional filtering and pagination."""
    from sqlalchemy import func, select
    from grimoire.db.models import Document

    async with get_db_context() as db:
        filters = []
        if params.status:
            filters.append(Document.processing_status == params.status)
        if params.file_type:
            filters.append(Document.file_type == params.file_type)
        if params.source_type:
            filters.append(Document.source_type == params.source_type)
        if params.severity:
            filters.append(Document.severity == params.severity)
        if params.cve_id:
            filters.append(Document.cve_id == params.cve_id)
        if params.mitre_technique_id:
            filters.append(Document.mitre_technique_id == params.mitre_technique_id)

        query = select(Document).order_by(Document.created_at.desc())
        if filters:
            query = query.where(*filters)

        count_query = select(func.count(Document.id))
        if filters:
            count_query = count_query.where(*filters)
        total = (await db.execute(count_query)).scalar() or 0

        query = query.offset(params.offset).limit(params.limit)
        result = await db.execute(query)
        docs = result.scalars().all()

        data = {
            "documents": [
                {
                    "id": d.id,
                    "title": d.title,
                    "source_path": d.source_path,
                    "file_type": d.file_type.value if hasattr(d.file_type, "value") else str(d.file_type),
                    "processing_status": d.processing_status.value if hasattr(d.processing_status, "value") else str(d.processing_status),
                    "size_bytes": d.size_bytes,
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                    "updated_at": d.updated_at.isoformat() if d.updated_at else None,
                }
                for d in docs
            ],
            "total": total,
            "offset": params.offset,
            "limit": params.limit,
        }
    return _ok(data)


async def grimoire_list_categories(ctx: Context) -> str:
    """List all categories."""
    from sqlalchemy import select
    from grimoire.db.models import Category

    async with get_db_context() as db:
        result = await db.execute(select(Category).order_by(Category.name))
        cats = result.scalars().all()
        data = [
            {
                "id": c.id,
                "name": c.name,
                "slug": c.slug,
                "description": c.description or "",
                "parent_id": c.parent_id,
                "color": c.color or "#3498db",
            }
            for c in cats
        ]
    return _ok(data)


async def grimoire_create_category(params: CreateCategoryInput, ctx: Context) -> str:
    """Create a new category.  Requires DEV tier or higher."""
    require_tier(ApiKeyTier.DEV, ApiKeyTier.AGENT)
    from uuid import uuid4
    from slugify import slugify
    from sqlalchemy import select
    from grimoire.db.models import Category

    async with get_db_context() as db:
        slug = slugify(params.name)
        existing = (await db.execute(select(Category).where(Category.slug == slug))).scalars().first()
        if existing:
            counter = 1
            new_slug = f"{slug}-{counter}"
            while (await db.execute(select(Category).where(Category.slug == new_slug))).scalars().first():
                counter += 1
                new_slug = f"{slug}-{counter}"
            slug = new_slug

        parent_id = None
        if params.parent_slug:
            parent = (await db.execute(select(Category).where(Category.slug == params.parent_slug))).scalars().first()
            if not parent:
                return _err(f"Parent category '{params.parent_slug}' not found.")
            parent_id = parent.id

        cat = Category(
            id=str(uuid4()),
            name=params.name,
            slug=slug,
            description=params.description,
            parent_id=parent_id,
            color=params.color,
        )
        db.add(cat)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            return _err("Failed to create category.")
        await db.refresh(cat)

        data = {
            "id": cat.id,
            "name": cat.name,
            "slug": cat.slug,
            "description": cat.description or "",
            "parent_id": cat.parent_id,
            "color": cat.color or "#3498db",
        }
    return _ok(data)


async def grimoire_watch_start(params: WatchStartInput, ctx: Context) -> str:
    """Start watching a path for changes.  Requires DEV tier or higher."""
    require_tier(ApiKeyTier.DEV, ApiKeyTier.AGENT)
    watcher = build_watcher()
    watch_id = await watcher.watch(
        params.path,
        backend=params.backend,
        recursive=params.recursive,
    )
    return _ok({"watch_id": watch_id, "path": params.path, "backend": params.backend, "is_running": True})


async def grimoire_watch_status(ctx: Context) -> str:
    """Get watcher statistics."""
    watcher = build_watcher()
    stats = watcher.get_status()
    return _ok({
        "active_watches": stats.active_watches,
        "total_files_processed": stats.total_files_processed,
        "total_files_failed": stats.total_files_failed,
        "watches": [
            {"watch_id": w.watch_id, "path": w.path, "backend": w.backend, "is_running": w.is_running}
            for w in stats.watches
        ],
    })


async def grimoire_delete_document(params: DeleteDocumentInput, ctx: Context) -> str:
    """Delete a document and its vectors.  Requires AGENT tier."""
    require_tier(ApiKeyTier.AGENT)
    from sqlalchemy import select
    from grimoire.db.models import Document

    async with get_db_context() as db:
        stmt = select(Document).where(Document.id == params.document_id)
        result = await db.execute(stmt)
        doc = result.scalar_one_or_none()
        if doc is None:
            return _err(f"Document '{params.document_id}' not found.")

        # Vector cleanup
        try:
            try:
                from grimoire.services.vector_store import get_vector_store_service
                settings = get_settings()
                vector_store = get_vector_store_service(settings)
                vector_ids = [chunk.vector_id for chunk in doc.chunks if chunk.vector_id]
                if vector_ids:
                    await vector_store.delete_vectors(vector_ids)
            except ImportError:
                logger.debug(f"Vector store service not available, skipping cleanup for {params.document_id}")
        except Exception as e:
            logger.warning(f"Failed to delete vectors for {params.document_id}: {e}")

        await db.delete(doc)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            return _err("Failed to delete document.")

    return _ok({"deleted": params.document_id})


async def grimoire_pg_query(params: PgQueryInput, ctx: Context) -> str:
    """Run a read-only SELECT query against the Postgres database."""
    from grimoire.db.session import get_db_manager
    from sqlalchemy import text

    sql = params.sql.rstrip(";")
    if "LIMIT" not in sql.upper():
        sql = f"{sql} LIMIT {params.limit}"

    manager = get_db_manager()
    async with manager.session() as db:
        try:
            rows = await db.execute(text(sql))
            data = [dict(r._mapping) for r in rows]
        except Exception as e:
            return _err(f"Query failed: {e}")

    return _ok({"row_count": len(data), "rows": data})


async def grimoire_status(ctx: Context) -> str:
    """Get system status (document counts, category counts, etc.)."""
    from sqlalchemy import func, select
    from grimoire.db.models import Category, Document, GeneratedContent

    async with get_db_context() as db:
        docs_total = (await db.execute(select(func.count(Document.id)))).scalar() or 0
        cats_total = (await db.execute(select(func.count(Category.id)))).scalar() or 0
        gen_total = (await db.execute(select(func.count(GeneratedContent.id)))).scalar() or 0

        # Chunk count via relationship
        chunks_total = 0
        status_breakdown: Dict[str, int] = {}
        for status in ProcessingStatus:
            cnt = (await db.execute(
                select(func.count(Document.id)).where(Document.processing_status == status)
            )).scalar() or 0
            if cnt:
                status_breakdown[status.value] = cnt

    return _ok({
        "documents": docs_total,
        "categories": cats_total,
        "generated_content": gen_total,
        "status_breakdown": status_breakdown,
    })
