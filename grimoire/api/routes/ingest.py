"""Ingest API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
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
    # Validate and sanitize file path to prevent path traversal
    import os
    from pathlib import Path
    
    resolved_path = Path(body.file_path).resolve()
    # Only allow access to /tmp, /home, or explicitly allowed paths
    allowed_roots = [Path("/tmp"), Path("/home"), Path("/home/sunds")]
    if not any(str(resolved_path).startswith(str(root)) for root in allowed_roots):
        raise HTTPException(
            status_code=403, 
            detail="Path not in allowed directories. Use paths under /home or /tmp."
        )
    
    if ".." in body.file_path or "~" in body.file_path:
        raise HTTPException(status_code=400, detail="Invalid characters in path")
    
    if not resolved_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {body.file_path}")
    
    agent = get_ingestion_agent()
    result = await agent.ingest_file(db, str(resolved_path), auto_tag=body.auto_tag)
    return IngestResultResponse(**result.model_dump())


@router.post("/directory", response_model=BatchIngestResponse)
async def ingest_directory(
    request: Request,
    body: IngestDirectoryRequest,
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> BatchIngestResponse:
    """Ingest all supported files from a directory."""
    # Validate and sanitize directory path to prevent path traversal
    from pathlib import Path
    
    resolved_path = Path(body.directory).resolve()
    allowed_roots = [Path("/tmp"), Path("/home"), Path("/home/sunds")]
    if not any(str(resolved_path).startswith(str(root)) for root in allowed_roots):
        raise HTTPException(
            status_code=403, 
            detail="Path not in allowed directories. Use paths under /home or /tmp."
        )
    
    if ".." in body.directory or "~" in body.directory:
        raise HTTPException(status_code=400, detail="Invalid characters in path")
    
    if not resolved_path.exists():
        raise HTTPException(status_code=404, detail=f"Directory not found: {body.directory}")
    
    if not resolved_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Path is not a directory: {body.directory}")
    
    agent = get_ingestion_agent()
    result = await agent.ingest_directory(
        db, str(resolved_path), recursive=body.recursive, auto_tag=body.auto_tag,
    )
    return BatchIngestResponse(**result.model_dump())