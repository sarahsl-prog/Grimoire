"""Ingest API routes."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from loguru import logger
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

# Read the body a megabyte at a time.  Streaming rather than awaiting the
# whole upload keeps a large file off the heap: one chunk is resident at a
# time regardless of the file's size.
_UPLOAD_CHUNK_BYTES = 1024 * 1024


class _UploadTooLargeError(Exception):
    """Internal signal that a streamed upload passed the configured cap."""


def _safe_unlink(path: Path) -> None:
    """Best-effort removal of a partial upload.

    Swallows failures too: a filename with an embedded null byte makes
    ``Path.unlink`` raise ``ValueError`` (not ``OSError``) just like
    ``Path.open`` does, so cleanup itself can fail with the same class of
    error it is being called to recover from. It must never escape and
    override the caller's own error handling.
    """
    with contextlib.suppress(OSError, ValueError):
        path.unlink(missing_ok=True)


def _staging_dir() -> Path:
    """Return the upload staging directory, creating it if absent.

    Patched in tests.  Uploads are kept here permanently: ``ingest_file``
    records the path it is given as ``Document.source_path``, so deleting the
    staged file would orphan the row.
    """
    from grimoire.config.settings import get_settings

    upload_dir = Path(get_settings().api.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def _max_upload_bytes() -> int:
    """Return the configured upload cap.  Patched in tests."""
    from grimoire.config.settings import get_settings

    return int(get_settings().api.max_upload_bytes)


def _supported_extensions() -> set[str]:
    """Return the parser's accepted extensions.

    Imported lazily: ``grimoire.core.parser`` imports Docling at module
    scope, which is far too heavy to pay for at route-module import time.
    """
    from grimoire.core.parser import DocumentParser

    return DocumentParser.SUPPORTED_EXTENSIONS


async def _stream_upload_to_disk(
    upload: UploadFile, destination: Path, max_bytes: int
) -> int:
    """Write an upload to ``destination`` in chunks, enforcing ``max_bytes``.

    Returns the number of bytes written.  On any failure the partial file is
    removed before the HTTPException propagates, so a rejected upload never
    leaves debris in the staging directory.
    """
    written = 0
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = await upload.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise _UploadTooLargeError
                handle.write(chunk)
    except _UploadTooLargeError:
        # Must be caught before (OSError, ValueError) below: it is a plain
        # Exception subclass with no relation to either, but if that ever
        # changes this ordering is what keeps the 413 path from being
        # swallowed by the 500 path.
        _safe_unlink(destination)
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the maximum upload size of {max_bytes} bytes",
        ) from None
    except (OSError, ValueError) as exc:
        # ValueError alongside OSError: a filename with an embedded null
        # byte (e.g. "a\x00b.txt") passes the extension check but makes
        # Path.open() raise ValueError rather than OSError, so it must be
        # caught here too or it escapes as a raw traceback to the client.
        _safe_unlink(destination)
        logger.error(f"Failed to write upload to {destination}: {exc}")
        raise HTTPException(
            status_code=500, detail="Could not store the uploaded file"
        ) from exc
    return written


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
        raise HTTPException(
            status_code=400, detail=f"Path is not a file: {body.file_path}"
        )

    agent = get_ingestion_agent()
    result = await agent.ingest_file(db, str(resolved), auto_tag=body.auto_tag)
    return IngestResultResponse(**result.model_dump())


@router.post("/upload", response_model=IngestResultResponse)
async def ingest_upload(
    request: Request,
    file: UploadFile = File(...),
    auto_tag: bool = Form(default=True),
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> IngestResultResponse:
    """Ingest a file uploaded in the request body.

    Unlike ``/ingest/file``, this endpoint never trusts a client-supplied
    path.  The server chooses the staging location and the client's filename
    contributes only a sanitized basename, so there is no traversal surface
    to validate.
    """
    original_name = Path(file.filename or "upload").name
    extension = Path(original_name).suffix.lower()
    if extension not in _supported_extensions():
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {extension or '(no extension)'}",
        )

    destination = _staging_dir() / f"{uuid4().hex}_{original_name}"
    written = await _stream_upload_to_disk(file, destination, _max_upload_bytes())
    logger.info(f"Staged upload {original_name} ({written} bytes) at {destination}")

    agent = get_ingestion_agent()
    result = await agent.ingest_file(db, str(destination), auto_tag=auto_tag)
    if result.status == "skipped":
        # A skipped result means the agent deduplicated against a
        # pre-existing Document whose source_path already points at the
        # ORIGINAL file, not this one - so nothing references the copy just
        # staged here. Unlike "completed"/"failed", which create a Document
        # row whose source_path IS this destination, a skipped upload would
        # otherwise leak the staged copy on the (non-rebuildable) uploads
        # volume forever, every time the same file is re-dragged.
        _safe_unlink(destination)
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
        raise HTTPException(
            status_code=404, detail=f"Directory not found: {body.directory}"
        )
    if not resolved.is_dir():
        raise HTTPException(
            status_code=400, detail=f"Path is not a directory: {body.directory}"
        )

    agent = get_ingestion_agent()
    result = await agent.ingest_directory(
        db,
        str(resolved),
        recursive=body.recursive,
        auto_tag=body.auto_tag,
    )
    return BatchIngestResponse(**result.model_dump())
