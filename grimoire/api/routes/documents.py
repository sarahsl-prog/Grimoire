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
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> DocumentListResponse:
    """List documents with optional filtering and pagination."""
    query = select(Document).order_by(Document.created_at.desc())

    if status:
        query = query.where(Document.processing_status == status)
    if file_type:
        query = query.where(Document.file_type == file_type)

    # Total count
    count_query = select(func.count(Document.id))
    if status:
        count_query = count_query.where(Document.processing_status == status)
    if file_type:
        count_query = count_query.where(Document.file_type == file_type)
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
                file_type=doc.file_type.value if hasattr(doc.file_type, "value") else str(doc.file_type),
                storage_backend=doc.storage_backend.value if hasattr(doc.storage_backend, "value") else str(doc.storage_backend),
                processing_status=doc.processing_status.value if hasattr(doc.processing_status, "value") else str(doc.processing_status),
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
        file_type=doc.file_type.value if hasattr(doc.file_type, "value") else str(doc.file_type),
        storage_backend=doc.storage_backend.value if hasattr(doc.storage_backend, "value") else str(doc.storage_backend),
        processing_status=doc.processing_status.value if hasattr(doc.processing_status, "value") else str(doc.processing_status),
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

    await db.delete(doc)
    await db.commit()