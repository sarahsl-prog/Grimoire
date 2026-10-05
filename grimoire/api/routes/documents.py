"""Document management API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import ScalarSelect, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

from grimoire.api.auth import get_api_key, require_min_tier
from grimoire.api.dependencies import get_db_session
from grimoire.api.schemas import (
    CategoryResponse,
    DocumentDetailResponse,
    DocumentListResponse,
    DocumentResponse,
)
from grimoire.db.models import (
    ApiKey,
    ApiKeyTier,
    Category,
    Chunk,
    Document,
    DocumentTag,
    TaggedBy,
)

router = APIRouter(prefix="/documents", tags=["documents"])

# Free-text search is a plain substring match, not a query language; the cap
# keeps a pathological pattern from reaching the database.
_MAX_SEARCH_LENGTH = 200


def _escape_like(text: str) -> str:
    """Make ``text`` match literally inside a ``LIKE`` pattern.

    ``%`` and ``_`` are wildcards and ``\\`` is the escape character itself,
    so a search for ``100%`` must not match every title. The backslash is
    escaped first so the escapes added for the other two are not doubled.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _chunk_count_column() -> ScalarSelect[int]:
    """Per-document chunk count, as a correlated scalar subquery.

    Selected alongside ``Document`` so the counts ride the same query and the
    same ORDER BY / LIMIT / OFFSET as the page: no N+1, and no way for a count
    to belong to a different document than the row it sits on.
    """
    return (
        select(func.count(Chunk.id))
        .where(Chunk.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )


def _tag_count_column() -> ScalarSelect[int]:
    """Per-document tag count (see ``_chunk_count_column``)."""
    return (
        select(func.count())
        .select_from(DocumentTag)
        .where(DocumentTag.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    request: Request,
    offset: int = 0,
    limit: int = 50,
    status: str | None = None,
    file_type: str | None = None,
    source_type: str | None = None,
    severity: str | None = None,
    cve_id: str | None = None,
    mitre_technique_id: str | None = None,
    q: str | None = Query(
        default=None,
        max_length=_MAX_SEARCH_LENGTH,
        description="Case-insensitive substring match on title or source path.",
    ),
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> DocumentListResponse:
    """List documents with optional filtering and pagination.

    In addition to the legacy ``status`` / ``file_type`` filters, the
    indexed Phase-2 security columns are filterable: ``source_type``,
    ``severity``, ``cve_id``, ``mitre_technique_id``. ``q`` is a free-text,
    case-insensitive substring match on the title or the source path. All
    filters compose with ``AND`` semantics; unsupplied (or blank) filters are
    ignored.
    """
    # Build the shared WHERE clauses once and apply to both the
    # paginated select and the count query — keeps the two in sync without
    # an untyped nested helper.
    filters = []
    if status:
        filters.append(Document.processing_status == status)
    if file_type:
        filters.append(Document.file_type == file_type)
    if source_type:
        filters.append(Document.source_type == source_type)
    if severity:
        filters.append(Document.severity == severity)
    if cve_id:
        filters.append(Document.cve_id == cve_id)
    if mitre_technique_id:
        filters.append(Document.mitre_technique_id == mitre_technique_id)
    if q and q.strip():
        pattern = f"%{_escape_like(q.strip())}%"
        filters.append(
            or_(
                Document.title.ilike(pattern, escape="\\"),
                Document.source_path.ilike(pattern, escape="\\"),
            )
        )

    # Document's chunks / tags / generated_content relationships are
    # lazy="selectin", which would pull every listed document's chunk text into
    # memory.  The page only needs the counts selected above, so switch every
    # relationship back to lazy for this query (and never touch them here: a lazy
    # load inside an async request raises).
    query = (
        select(Document, _chunk_count_column(), _tag_count_column())
        .options(lazyload("*"))
        .order_by(Document.created_at.desc())
    )
    if filters:
        query = query.where(*filters)

    count_query = select(func.count(Document.id))
    if filters:
        count_query = count_query.where(*filters)
    total = (await db.execute(count_query)).scalar() or 0

    # Paginated results
    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    rows = result.all()

    return DocumentListResponse(
        documents=[
            DocumentResponse(
                id=doc.id,
                title=doc.title,
                source_path=doc.source_path,
                file_type=(
                    doc.file_type.value
                    if hasattr(doc.file_type, "value")
                    else str(doc.file_type)
                ),
                storage_backend=(
                    doc.storage_backend.value
                    if hasattr(doc.storage_backend, "value")
                    else str(doc.storage_backend)
                ),
                processing_status=(
                    doc.processing_status.value
                    if hasattr(doc.processing_status, "value")
                    else str(doc.processing_status)
                ),
                size_bytes=doc.size_bytes,
                created_at=doc.created_at.isoformat() if doc.created_at else None,
                updated_at=doc.updated_at.isoformat() if doc.updated_at else None,
                chunk_count=chunk_count,
                tag_count=tag_count,
            )
            for doc, chunk_count, tag_count in rows
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/{document_id}", response_model=DocumentDetailResponse)
async def get_document(
    document_id: str,
    request: Request,
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> DocumentDetailResponse:
    """Get detailed information about a document."""
    doc = await db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

    # Names come from an explicit query: ``doc.tags`` holds DocumentTag rows
    # whose ``category`` is lazy, and a lazy load inside an async request
    # raises.  Sorted so the response is stable.
    tag_categories = list(
        (
            await db.execute(
                select(Category)
                .options(lazyload("*"))
                .join(DocumentTag, DocumentTag.category_id == Category.id)
                .where(DocumentTag.document_id == doc.id)
                .order_by(Category.name, Category.id)
            )
        )
        .scalars()
        .all()
    )
    tag_names = [cat.name for cat in tag_categories]
    chunk_count = (
        await db.execute(
            select(func.count(Chunk.id)).where(Chunk.document_id == doc.id)
        )
    ).scalar() or 0

    return DocumentDetailResponse(
        id=doc.id,
        title=doc.title,
        source_path=doc.source_path,
        file_type=(
            doc.file_type.value
            if hasattr(doc.file_type, "value")
            else str(doc.file_type)
        ),
        storage_backend=(
            doc.storage_backend.value
            if hasattr(doc.storage_backend, "value")
            else str(doc.storage_backend)
        ),
        processing_status=(
            doc.processing_status.value
            if hasattr(doc.processing_status, "value")
            else str(doc.processing_status)
        ),
        size_bytes=doc.size_bytes,
        created_at=doc.created_at.isoformat() if doc.created_at else None,
        updated_at=doc.updated_at.isoformat() if doc.updated_at else None,
        chunk_count=chunk_count,
        tag_count=len(tag_names),
        tags=tag_names,
        categories=[
            CategoryResponse(
                id=cat.id,
                name=cat.name,
                slug=cat.slug,
                description=cat.description or "",
                parent_id=cat.parent_id,
                color=cat.color or "#3498db",
            )
            for cat in tag_categories
        ],
        error_message=doc.error_message,
    )


async def _tag_target(
    db: AsyncSession, document_id: str, category_id: str
) -> DocumentTag | None:
    """The existing tag link, after checking both ends exist.

    Raises:
        HTTPException: 404 naming whichever of the document or category is
            unknown, so a typo is not mistaken for "already untagged".
    """
    if await db.get(Document, document_id) is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    if await db.get(Category, category_id) is None:
        raise HTTPException(status_code=404, detail=f"Category {category_id} not found")
    return await db.get(DocumentTag, (document_id, category_id))


@router.put("/{document_id}/tags/{category_id}", status_code=204)
async def tag_document(
    document_id: str,
    category_id: str,
    request: Request,
    api_key: ApiKey = Depends(require_min_tier(ApiKeyTier.DEV)),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Tag a document with a category (idempotent).

    A tag that is already there, whoever set it, is left exactly as it is.
    """
    if await _tag_target(db, document_id, category_id) is not None:
        return
    db.add(
        DocumentTag(
            document_id=document_id,
            category_id=category_id,
            confidence=1.0,
            tagged_by=TaggedBy.USER,
        )
    )
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        from loguru import logger

        logger.error(f"Failed to tag document {document_id}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to tag document") from exc


@router.delete("/{document_id}/tags/{category_id}", status_code=204)
async def untag_document(
    document_id: str,
    category_id: str,
    request: Request,
    api_key: ApiKey = Depends(require_min_tier(ApiKeyTier.DEV)),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Remove a category from a document (idempotent)."""
    link = await _tag_target(db, document_id, category_id)
    if link is None:
        return
    await db.delete(link)
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        from loguru import logger

        logger.error(f"Failed to untag document {document_id}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to untag document") from exc


@router.delete("/{document_id}", status_code=204)
async def delete_document(
    document_id: str,
    request: Request,
    api_key: ApiKey = Depends(require_min_tier(ApiKeyTier.AGENT)),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Delete a document and its associated data."""
    doc = await db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

    # Clean up vector store entries before deleting document
    # This prevents orphaned vectors in ChromaDB/Qdrant
    # Gracefully handle case where vector store service doesn't exist yet
    try:
        try:
            from grimoire.config.settings import get_settings
            from grimoire.services.vector_store import get_vector_store_service

            settings = get_settings()
            vector_store = get_vector_store_service(settings)

            # Delete vectors for all chunks
            vector_ids = [chunk.vector_id for chunk in doc.chunks if chunk.vector_id]
            if vector_ids:
                await vector_store.delete_vectors(vector_ids)
        except ImportError:
            # Vector store service not implemented yet - just log and continue
            from loguru import logger

            logger.debug(
                f"Vector store service not available, skipping vector cleanup for {document_id}"
            )
    except Exception as e:
        from loguru import logger

        logger.warning(f"Failed to delete vectors for document {document_id}: {e}")
        # Continue with document deletion even if vector cleanup fails

    await db.delete(doc)
    try:
        await db.commit()
    except Exception as commit_error:
        await db.rollback()
        from loguru import logger

        # Keep the cause in the logs; the client only sees the generic detail.
        logger.error(f"Failed to delete document {document_id}: {commit_error}")
        raise HTTPException(
            status_code=500, detail="Failed to delete document"
        ) from commit_error
