"""Ingest API routes."""

from __future__ import annotations

import os
from pathlib import Path

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

# Resolve allowed roots once at module load — harmless because they are
# absolute system paths.  Symlinks inside them are still followed at runtime.
_ALLOWED_ROOTS = [Path("/tmp").resolve(), Path("/home/sunds").resolve()]
_MAX_PATH_LEN = 2048


def _is_path_allowed(raw_path: str) -> Path:
    """Sanitize a user-provided path and verify it stays under allowed roots.

    Steps:
      1. Reject null bytes and overly long strings.
      2. Resolve symlinks via realpath() and resolve().
      3. Ensure the canonical path is under an allowed root.

    Raises HTTPException(400) for invalid input and HTTPException(403) for
    paths that escape the chroot-style boundary.
    """
    if "\x00" in raw_path:
        raise HTTPException(status_code=400, detail="Null bytes not allowed in path")

    if len(raw_path) > _MAX_PATH_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Path exceeds maximum length of {_MAX_PATH_LEN} characters",
        )

    # realpath follows symlinks; resolve() makes it absolute and collapses "..".
    resolved = Path(raw_path).resolve()
    try:
        real = Path(os.path.realpath(resolved))
    except OSError:
        real = resolved

    if not any(real.is_relative_to(root) for root in _ALLOWED_ROOTS):
        raise HTTPException(
            status_code=403,
            detail="Path not in allowed directories. Use paths under /home/sunds or /tmp.",
        )

    return real


@router.post("/file", response_model=IngestResultResponse)
async def ingest_file(
    request: Request,
    body: IngestFileRequest,
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> IngestResultResponse:
    """Ingest a single file into the knowledge base."""
    resolved = _is_path_allowed(body.file_path)

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {body.file_path}")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail=f"Path is not a file: {body.file_path}")

    agent = get_ingestion_agent()
    result = await agent.ingest_file(db, str(resolved), auto_tag=body.auto_tag)
    return IngestResultResponse(**result.model_dump())


@router.post("/directory", response_model=BatchIngestResponse)
async def ingest_directory(
    request: Request,
    body: IngestDirectoryRequest,
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> BatchIngestResponse:
    """Ingest all supported files from a directory."""
    resolved = _is_path_allowed(body.directory)

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"Directory not found: {body.directory}")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"Path is not a directory: {body.directory}")

    agent = get_ingestion_agent()
    result = await agent.ingest_directory(
        db, str(resolved), recursive=body.recursive, auto_tag=body.auto_tag,
    )
    return BatchIngestResponse(**result.model_dump())