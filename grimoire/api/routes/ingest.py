"""Ingest API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.api.auth import get_api_key
from grimoire.api.dependencies import get_db_session, get_ingestion_agent
from grimoire.api.schemas import (
    BatchIngestResponse,
    IngestDirectoryRequest,
    IngestFileRequest,
    IngestResultResponse,
)
from grimoire.db.models import ApiKey

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("/file", response_model=IngestResultResponse)
async def ingest_file(
    request: Request,
    body: IngestFileRequest,
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> IngestResultResponse:
    """Ingest a single file into the knowledge base."""
    agent = get_ingestion_agent()
    result = await agent.ingest_file(db, body.file_path, auto_tag=body.auto_tag)
    return IngestResultResponse(**result.model_dump())


@router.post("/directory", response_model=BatchIngestResponse)
async def ingest_directory(
    request: Request,
    body: IngestDirectoryRequest,
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> BatchIngestResponse:
    """Ingest all supported files from a directory."""
    agent = get_ingestion_agent()
    result = await agent.ingest_directory(
        db, body.directory, recursive=body.recursive, auto_tag=body.auto_tag,
    )
    return BatchIngestResponse(**result.model_dump())