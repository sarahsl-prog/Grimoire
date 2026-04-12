"""Ingest API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.api.dependencies import get_db_session, get_ingestion_agent
from grimoire.api.schemas import (
    BatchIngestResponse,
    IngestDirectoryRequest,
    IngestFileRequest,
    IngestResultResponse,
)

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("/file", response_model=IngestResultResponse)
async def ingest_file(
    request: IngestFileRequest,
    db: AsyncSession = Depends(get_db_session),
) -> IngestResultResponse:
    """Ingest a single file into the knowledge base."""
    agent = get_ingestion_agent()
    result = await agent.ingest_file(db, request.file_path, auto_tag=request.auto_tag)
    return IngestResultResponse(**result.model_dump())


@router.post("/directory", response_model=BatchIngestResponse)
async def ingest_directory(
    request: IngestDirectoryRequest,
    db: AsyncSession = Depends(get_db_session),
) -> BatchIngestResponse:
    """Ingest all supported files from a directory."""
    agent = get_ingestion_agent()
    result = await agent.ingest_directory(
        db, request.directory, recursive=request.recursive, auto_tag=request.auto_tag,
    )
    return BatchIngestResponse(**result.model_dump())
