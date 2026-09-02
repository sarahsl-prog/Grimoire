"""MCP tool definitions for Grimoire.

Every tool delegates to the existing Grimoire service layer (agents,
repositories, etc.) rather than talking to ChromaDB or Postgres directly.
This keeps the MCP surface aligned with the REST API and ensures that
chunking, embedding, caching, and tagging all happen exactly as they do
for HTTP clients.

Tier-based access control:
- READ  (rdl): search, ask, get, list, status, read-only queries
- DEV   (dvl): everything READ has + ingest, generate, create, watch start
- AGENT (agt): everything DEV has + delete
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from loguru import logger
from mcp.server.mcpserver import Context
from pydantic import BaseModel, ConfigDict, Field, field_validator

from grimoire.api.dependencies import (
    get_content_gen_agent,
    get_ingestion_agent,
    get_query_agent,
)
from grimoire.cli.helpers import build_watcher
from grimoire.config.settings import get_settings
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


_mcp_watcher: Any = None


def _get_mcp_watcher() -> Any:
    global _mcp_watcher
    if _mcp_watcher is None:
        _mcp_watcher = build_watcher()
    return _mcp_watcher


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


class CveSearchInput(BaseModel):
    """Parameters for grimoire_search_cve."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    cve_id: Optional[str] = Field(
        default=None, description="Exact CVE identifier, e.g. 'CVE-2021-44228'."
    )
    query: Optional[str] = Field(
        default=None, max_length=2000, description="Semantic search over CVE descriptions."
    )
    severity: Optional[str] = Field(
        default=None,
        description="Filter by severity bucket: critical | high | medium | low | info | unknown.",
    )
    min_cvss: Optional[float] = Field(
        default=None, ge=0.0, le=10.0, description="Minimum CVSS base score."
    )
    year: Optional[int] = Field(
        default=None, ge=1999, le=2100, description="Filter by CVE year."
    )
    top_k: int = Field(default=10, ge=1, le=100, description="Maximum results to return.")

    @field_validator("cve_id")
    @classmethod
    def _validate_cve_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        from grimoire.strategies.security.metadata import _RE_CVE_ID

        if not _RE_CVE_ID.match(v):
            raise ValueError("cve_id must match 'CVE-YYYY-N+', e.g. 'CVE-2021-44228'.")
        return v

    @field_validator("severity")
    @classmethod
    def _validate_severity(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.lower()
        from grimoire.strategies.security.metadata import Severity

        allowed = {s.value for s in Severity}
        if v not in allowed:
            raise ValueError(f"severity must be one of {sorted(allowed)}.")
        return v


class PlaybookSearchInput(BaseModel):
    """Parameters for grimoire_search_playbook.

    Searches the Sigma detection-rule and IR-playbook corpora with optional
    MITRE ATT&CK technique, IR phase, platform, log-source, and severity
    facets.  Use ``source_types`` to restrict to specific corpora
    (default: both ``["playbook", "sigma_rule"]``).

    When ``query`` is absent and only structured facets are supplied,
    the search runs entirely in PostgreSQL (vector-bypass, ``"mode": "facet_only"``)
    and returns structured metadata without semantic scoring.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Semantic search over rule titles and detection logic. "
        "Omit to use the vector-bypass facet-only path.",
    )
    mitre_technique_id: Optional[str] = Field(
        default=None, description="ATT&CK technique id, e.g. 'T1059' or 'T1059.001'."
    )
    severity: Optional[str] = Field(
        default=None,
        description="Filter by severity bucket: critical | high | medium | low | info.",
    )
    platform: Optional[str] = Field(
        default=None, max_length=64, description="Platform facet, e.g. 'windows', 'linux', 'aws'."
    )
    log_source: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Log source facet, e.g. 'process_creation', 'sysmon'.",
    )
    phase: Optional[str] = Field(
        default=None,
        max_length=32,
        description="Playbook IR phase facet, e.g. 'identify', 'contain', 'recover'.",
    )
    source_types: List[str] = Field(
        default=["playbook", "sigma_rule"],
        description="List of source types to search. "
        "E.g. ['playbook', 'sigma_rule'] or ['sigma_rule'] only. "
        "Defaults to both corpora.",
    )
    top_k: int = Field(default=10, ge=1, le=100, description="Maximum results to return.")

    @field_validator("phase")
    @classmethod
    def _validate_phase(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip().lower()

    @field_validator("mitre_technique_id")
    @classmethod
    def _validate_mitre_technique_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        from grimoire.strategies.security.metadata import _RE_MITRE_TECHNIQUE_ID

        if not _RE_MITRE_TECHNIQUE_ID.match(v):
            raise ValueError(
                "mitre_technique_id must match 'T<4d>(.<3d>)?', e.g. 'T1059.001'."
            )
        return v

    @field_validator("severity")
    @classmethod
    def _validate_severity(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.lower()
        from grimoire.strategies.security.metadata import Severity

        allowed = {s.value for s in Severity}
        if v not in allowed:
            raise ValueError(f"severity must be one of {sorted(allowed)}.")
        return v


class PgQueryInput(BaseModel):
    """Parameters for grimoire_pg_query."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    sql: str = Field(..., min_length=1, max_length=4000, description="SELECT query only.")
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("sql")
    @classmethod
    def _must_be_select(cls, v: str) -> str:
        import re
        stripped = v.strip().upper()
        if not (stripped.startswith("SELECT") or stripped.startswith("WITH")):
            raise ValueError("Only SELECT queries (including WITH ... SELECT CTEs) are permitted.")
        if re.search(r"\bSELECT\b.*\bINTO\b", stripped, re.DOTALL):
            raise ValueError("SELECT INTO is not permitted.")
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


def _jsonb_array_contains(column: Any, key: str, value: str) -> Any:
    """Match a scalar value inside a JSONB array field using JSONB operators.

    Uses PostgreSQL's JSONB ``@>`` containment operator when available,
    falling back to a portable ILIKE pattern over the serialised JSON for
    SQLite / other dialects.  The GIN index on ``security_metadata`` makes
    ``@>`` O(log n) even on millions of rows; the ILIKE fallback is O(n)
    but acceptable for development / small datasets.

    ``key`` is the JSONB object key to extract (e.g. ``"platforms"``,
    ``"playbook_phase"``, ``"log_sources"``).  The value is matched as a
    **scalar string** — list fields are stored as JSON arrays in the blob
    and this helper checks membership within that array.
    """
    from sqlalchemy import cast, String

    try:
        return column[key].astext.cast(String).in_([value])
    except Exception:  # noqa: S110
        pass
    return cast(column, String).ilike(f'%"{value}"%')


async def _matching_document_ids(
    db: Any,
    source_types: List[str],
    conditions: List[Any],
) -> List[str]:
    """Return document ids of the given ``source_types`` matching all conditions."""
    from sqlalchemy import select

    from grimoire.db.models import Document

    if len(source_types) == 1:
        stmt = select(Document.id).where(Document.source_type == source_types[0])
    else:
        stmt = select(Document.id).where(Document.source_type.in_(source_types))
    for cond in conditions:
        stmt = stmt.where(cond)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _facet_only_search(
    *,
    source_types: List[str],
    conditions: List[Any],
    top_k: int,
) -> str:
    """Return pre-filtered document IDs without invoking the vector layer.

    Used when the caller supplies only structured facets (no semantic query).
    The SQL pre-filter narrows the candidate set, then document IDs are
    returned ordered by content_date desc (most recent first) capped at top_k.
    """
    from sqlalchemy import select

    from grimoire.db.models import Document

    async with get_db_context() as db:
        doc_ids = await _matching_document_ids(db, source_types, conditions)

    if not doc_ids:
        return _ok({
            "mode": "facet_only",
            "source_types": source_types,
            "sql_prefiltered_documents": 0,
            "results": [],
            "total_results": 0,
        })

    async with get_db_context() as db:
        stmt = (
            select(Document)
            .where(Document.id.in_(doc_ids))
            .order_by(Document.content_date.desc())
            .limit(top_k)
        )
        result = await db.execute(stmt)
        docs = result.scalars().all()

    return _ok({
        "mode": "facet_only",
        "source_types": source_types,
        "sql_prefiltered_documents": len(doc_ids),
        "results": [
            {
                "document_id": d.id,
                "title": d.title,
                "source_type": d.source_type,
                "severity": d.severity.value if hasattr(d.severity, "value") else str(d.severity) if d.severity else None,
                "mitre_technique_id": d.mitre_technique_id,
                "content_date": d.content_date.isoformat() if d.content_date else None,
                "source_path": d.source_path,
            }
            for d in docs
        ],
        "total_results": len(docs),
    })


async def _facet_search(
    *,
    source_types: List[str],
    conditions: List[Any],
    query_text: str,
    top_k: int,
) -> str:
    """Run a semantic search restricted to documents matching SQL facets.

    Facets (severity, CVSS, MITRE technique, platform, log source, CVE year)
    are applied via SQL on ``documents`` first — several live in the
    ``security_metadata`` JSONB blob that ChromaDB cannot substring-match —
    then the vector search is restricted to the surviving document ids.
    """
    async with get_db_context() as db:
        doc_ids = await _matching_document_ids(db, source_types, conditions)

    if not doc_ids:
        return _ok({
            "mode": "semantic",
            "source_types": source_types,
            "query": query_text,
            "results": [],
            "total_results": 0,
        })

    if len(source_types) == 1:
        filters: Dict[str, Any] = {
            "source_type": source_types[0], "document_id": {"$in": doc_ids}
        }
    else:
        filters = {
            "source_type": {"$in": source_types}, "document_id": {"$in": doc_ids}
        }
    agent = get_query_agent()
    async with get_db_context() as db:
        result = await agent.search(
            db,
            query_text,
            top_k=top_k,
            filter_dict=filters,
        )
    return _ok({
        "mode": "semantic",
        "source_types": source_types,
        "query": result.query,
        "sql_prefiltered_documents": len(doc_ids),
        "results": [r.model_dump() for r in result.results],
        "total_results": result.total_results,
        "duration_ms": result.duration_ms,
    })


async def grimoire_search_cve(params: CveSearchInput, ctx: Context) -> str:
    """Search the ingested NVD CVE corpus.

    Provide either an exact ``cve_id`` for a direct document lookup, or a
    free-text ``query`` combined with severity / CVSS / year facets to run a
    semantic search restricted to CVE content.
    """
    if not params.cve_id and not params.query:
        return _err(
            "Provide either 'cve_id' for an exact lookup or 'query' for semantic search.",
            hint="e.g. cve_id='CVE-2021-44228' or query='remote code execution'",
        )

    # Exact-ID fast path: direct SQL over the indexed cve_id column.
    if params.cve_id:
        from sqlalchemy import select

        from grimoire.db.models import Chunk, Document

        async with get_db_context() as db:
            doc_stmt = select(Document).where(Document.cve_id == params.cve_id)
            doc_result = await db.execute(doc_stmt)
            doc = doc_result.scalars().first() if doc_result is not None else None
            if doc is None:
                return _err(f"No document found for {params.cve_id}.")

            chunk_stmt = (
                select(Chunk)
                .where(Chunk.document_id == doc.id)
                .order_by(Chunk.chunk_index)
            )
            chunk_result = await db.execute(chunk_stmt)
            chunks = chunk_result.scalars().all()

        severity = doc.severity.value if hasattr(doc.severity, "value") else doc.severity
        return _ok({
            "mode": "exact",
            "cve_id": params.cve_id,
            "document": {
                "id": doc.id,
                "title": doc.title,
                "severity": severity,
                "mitre_technique_id": doc.mitre_technique_id,
                "source_path": doc.source_path,
                "security_metadata": doc.security_metadata,
                "content_date": doc.content_date.isoformat() if doc.content_date else None,
            },
            "chunks": [
                {"chunk_index": c.chunk_index, "content": c.content}
                for c in chunks
            ],
        })

    # Semantic path: facet pre-filter in SQL, then vector search on matches.
    from sqlalchemy import Float, cast, func

    from grimoire.db.models import Document

    conditions: List[Any] = []
    if params.severity:
        conditions.append(Document.severity == params.severity)
    if params.year:
        conditions.append(Document.cve_id.startswith(f"CVE-{params.year}-"))
    if params.min_cvss is not None:
        # cvss_score lives inside the security_metadata blob; cast to float for
        # a numeric comparison. NULL and unparseable values fail the > comparison.
        conditions.append(
            cast(
                func.json_extract(Document.security_metadata, "$.cvss_score"), Float
            ) >= params.min_cvss
        )

    return await _facet_search(
        source_types=["nvd_cve"],
        conditions=conditions,
        query_text=params.query or "",
        top_k=params.top_k,
    )


async def grimoire_search_playbook(params: PlaybookSearchInput, ctx: Context) -> str:
    """Search the Sigma detection-rule and IR-playbook corpora.

    Provide a free-text ``query`` for semantic search, and/or structured
    facets (mitre_technique_id, severity, phase, platform, log_source).
    Use ``source_types`` to restrict to specific corpora.

    When ``query`` is absent, the search runs entirely in PostgreSQL
    (vector-bypass, ``"mode": "facet_only"``) and returns pre-filtered
    document IDs without invoking the vector layer.
    """
    if not params.query and not params.mitre_technique_id and not params.severity \
            and not params.phase and not params.platform and not params.log_source:
        return _err(
            "At least one of 'query', 'mitre_technique_id', 'severity', 'phase', "
            "'platform', or 'log_source' is required.",
        )

    from grimoire.db.models import Document

    conditions: List[Any] = []
    if params.mitre_technique_id:
        conditions.append(Document.mitre_technique_id == params.mitre_technique_id)
    if params.severity:
        conditions.append(Document.severity == params.severity)
    if params.phase:
        conditions.append(
            _jsonb_array_contains(Document.security_metadata, "playbook_phase", params.phase)
        )
    if params.platform:
        conditions.append(
            _jsonb_array_contains(Document.security_metadata, "platforms", params.platform.lower())
        )
    if params.log_source:
        conditions.append(
            _jsonb_array_contains(Document.security_metadata, "log_sources", params.log_source.lower())
        )

    if not params.query:
        return await _facet_only_search(
            source_types=params.source_types,
            conditions=conditions,
            top_k=params.top_k,
        )

    return await _facet_search(
        source_types=params.source_types,
        conditions=conditions,
        query_text=params.query,
        top_k=params.top_k,
    )


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
            await db.refresh(cat)
        except Exception:
            await db.rollback()
            return _err("Failed to create category.")

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
    watcher = _get_mcp_watcher()
    watch_id = await watcher.watch(
        params.path,
        backend=params.backend,
        recursive=params.recursive,
    )
    return _ok({"watch_id": watch_id, "path": params.path, "backend": params.backend, "is_running": True})


class WatchStopInput(BaseModel):
    """Parameters for grimoire_watch_stop."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    watch_id: str = Field(..., min_length=1)


async def grimoire_watch_stop(params: WatchStopInput, ctx: Context) -> str:
    """Stop an active watch.  Requires DEV tier or higher."""
    require_tier(ApiKeyTier.DEV, ApiKeyTier.AGENT)
    watcher = _get_mcp_watcher()
    stopped = await watcher.unwatch(params.watch_id)
    if not stopped:
        return _err(f"Watch '{params.watch_id}' not found or already stopped.")
    return _ok({"stopped": params.watch_id})


async def grimoire_watch_status(ctx: Context) -> str:
    """Get watcher statistics."""
    watcher = _get_mcp_watcher()
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

        # Collect vector IDs while the document is still attached to the session.
        # Accessing lazy relationships after delete/commit raises DetachedInstanceError.
        vector_ids = [chunk.vector_id for chunk in doc.chunks if chunk.vector_id]

        await db.delete(doc)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            return _err("Failed to delete document.")

    # Vector cleanup after durable commit — best-effort.
    # Failure leaves orphaned vectors (search noise) but document is fully deleted.
    if vector_ids:
        try:
            from grimoire.services.vector_store import get_vector_store_service
            settings = get_settings()
            vector_store = get_vector_store_service(settings)
            await vector_store.delete_vectors(vector_ids)
        except ImportError:
            logger.debug(f"Vector store service not available, skipping cleanup for {params.document_id}")
        except Exception as e:
            logger.warning(f"Vectors not cleaned up for {params.document_id}: {e}. May require manual ChromaDB cleanup.")

    return _ok({"deleted": params.document_id})


async def grimoire_pg_query(params: PgQueryInput, ctx: Context) -> str:
    """Run a read-only SELECT query against the Postgres database.

    The user-supplied SQL is executed as a subquery with a separate, validated
    row limit applied by SQLAlchemy bound parameters.  The Pydantic validator
    already rejects non-SELECT/WITH statements and SELECT INTO.
    """
    require_tier(ApiKeyTier.DEV, ApiKeyTier.AGENT)
    from grimoire.db.session import get_db_manager
    from sqlalchemy import text

    inner = params.sql.rstrip(";")
    # Use a bound parameter for the limit so that user SQL is never
    # interpolated into the executable statement.
    sql = text(
        "SELECT * FROM (:inner_query) AS _grimoire_q LIMIT :row_limit"
    ).bindparams(inner_query=inner, row_limit=params.limit)

    manager = get_db_manager()
    async with manager.session() as db:
        try:
            rows = await db.execute(sql)
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

        # Per-processing-status breakdown
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
