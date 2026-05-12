"""Document management API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.api.auth import get_api_key
from grimoire.api.dependencies import get_db_session
from grimoire.api.schemas import (
    DocumentDetailResponse,
    DocumentListResponse,
    DocumentResponse,
)
from grimoire.db.models import ApiKey, Document

router = APIRouter(prefix="/documents", tags=["documents"])


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
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> DocumentListResponse:
    """List documents with optional filtering and pagination.

    In addition to the legacy ``status`` / ``file_type`` filters, the
    indexed Phase-2 security columns are filterable: ``source_type``,
    ``severity``, ``cve_id``, ``mitre_technique_id``. All filters compose
    with ``AND`` semantics; unsupplied filters are ignored.
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

    query = select(Document).order_by(Document.created_at.desc())
    if filters:
        query = query.where(*filters)

    count_query = select(func.count(Document.id))
    if filters:
        count_query = count_query.where(*filters)
    total = (await db.execute(count_query)).scalar() or 0

    # Paginated results
    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    docs = result.scalars().all()

    return DocumentListResponse(
        documents=[
            DocumentResponse(
                id=doc.id,
                title=doc.title,
                source_path=doc.source_path,
                file_type=doc.file_type.value
                if hasattr(doc.file_type, "value")
                else str(doc.file_type),
                storage_backend=doc.storage_backend.value
                if hasattr(doc.storage_backend, "value")
                else str(doc.storage_backend),
                processing_status=doc.processing_status.value
                if hasattr(doc.processing_status, "value")
                else str(doc.processing_status),
                size_bytes=doc.size_bytes,
                created_at=doc.created_at.isoformat() if doc.created_at else None,
                updated_at=doc.updated_at.isoformat() if doc.updated_at else None,
            )
            for doc in docs
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

    return DocumentDetailResponse(
        id=doc.id,
        title=doc.title,
        source_path=doc.source_path,
        file_type=doc.file_type.value
        if hasattr(doc.file_type, "value")
        else str(doc.file_type),
        storage_backend=doc.storage_backend.value
        if hasattr(doc.storage_backend, "value")
        else str(doc.storage_backend),
        processing_status=doc.processing_status.value
        if hasattr(doc.processing_status, "value")
        else str(doc.processing_status),
        size_bytes=doc.size_bytes,
        created_at=doc.created_at.isoformat() if doc.created_at else None,
        updated_at=doc.updated_at.isoformat() if doc.updated_at else None,
        error_message=doc.error_message,
    )


@router.delete("/{document_id}", status_code=204)
async def delete_document(
    document_id: str,
    request: Request,
    api_key: ApiKey = Depends(get_api_key),
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
            from grimoire.services.vector_store import get_vector_store_service
            from grimoire.config.settings import get_settings

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
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to delete document")
