"""FastAPI application factory for the Grimoire REST API."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from grimoire.api.routes import categories, documents, generate, ingest, query, watch

# FastAPI resolves an `UploadFile = File(...)` parameter by parsing the
# ENTIRE multipart body before the route handler's first line runs, so
# ingest.py's own streaming byte counter only bounds what gets STAGED to
# disk, not what gets RECEIVED - an oversized body is already spooled by
# the time that guard sees it. This margin absorbs the multipart framing
# around a file's raw bytes (boundary markers, per-part headers, trailing
# CRLFs) that isn't part of the file itself, so a request whose file is
# exactly at the configured cap isn't rejected for its envelope.
_MULTIPART_FRAMING_MARGIN_BYTES = 8 * 1024

_UPLOAD_PATH_SUFFIX = "/ingest/upload"


def _upload_cap_with_margin() -> int:
    """Configured upload cap plus the multipart-framing margin.

    Patched in tests, mirroring ingest.py's ``_max_upload_bytes``.
    """
    from grimoire.config.settings import get_settings

    return int(get_settings().api.max_upload_bytes) + _MULTIPART_FRAMING_MARGIN_BYTES


class ContentLengthGuard:
    """Reject an oversized upload by its Content-Length header alone.

    This must run before routing/body parsing - it is registered as the
    outermost middleware (added last; Starlette wraps in LIFO order) so it
    sees the request before FastAPI ever resolves the route's ``UploadFile``
    parameter. Scoped to the upload path only: every other endpoint takes a
    small JSON body, so applying the same cap there would be meaningless
    weight on every request for no protection gained.

    A missing or unparseable Content-Length must not block the request:
    that just means this fast-path guard does not apply, and ingest.py's
    existing streaming guard (``_stream_upload_to_disk``) still bounds the
    bytes actually received for those requests.

    This is a pure-ASGI middleware (not ``BaseHTTPMiddleware``) on purpose:
    ``BaseHTTPMiddleware`` wraps EVERY request it sees in a receive-caching
    ``Request`` plus an anyio task group and memory-object stream, even ones
    it does nothing with. The MCP server mounted at ``/mcp`` in this same
    app serves a long-lived SSE stream (``/mcp/sse``), and that wrapping is
    the classic source of broken disconnect propagation and buffered
    streaming responses. Implementing this as a plain ``__call__(scope,
    receive, send)`` means non-upload traffic - including every MCP
    request - is forwarded with the original ``receive``/``send`` untouched,
    so it is never at risk of that interaction. Do not convert this back to
    ``BaseHTTPMiddleware``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _is_upload_request(scope: Scope) -> bool:
        method: str = scope["method"]
        path: str = scope["path"]
        return method == "POST" and path.endswith(_UPLOAD_PATH_SUFFIX)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._is_upload_request(scope):
            # Delegate immediately, with the original receive/send, no
            # wrapping at all - see the class docstring for why this
            # matters for websocket/lifespan scopes and streaming responses.
            await self.app(scope, receive, send)
            return

        declared = next(
            (v for k, v in scope.get("headers", []) if k.lower() == b"content-length"),
            None,
        )
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except ValueError:
                declared_bytes = None
            if (
                declared_bytes is not None
                and declared_bytes > _upload_cap_with_margin()
            ):
                response = JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            "Request body exceeds the maximum upload size of "
                            f"{_upload_cap_with_margin()} bytes"
                        )
                    },
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown."""
    from grimoire.config.settings import get_settings
    from grimoire.db.session import close_db, initialize_db

    settings = get_settings()
    await initialize_db(settings.database.url)
    try:
        yield
    finally:
        await close_db()


def create_app(use_lifespan: bool = True) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Grimoire",
        description="Knowledge management and content generation API.",
        version="2.0.0",
        lifespan=lifespan if use_lifespan else None,
    )

    # Rate limiting (must be added before CORS so it runs first in request pipeline)
    from grimoire.api.rate_limit import setup_rate_limiting

    limiter = setup_rate_limiting(app)

    # CORS — origins configurable via GRIMOIRE_AUTH__CORS_ORIGINS
    from grimoire.config.settings import ConfigurationError, get_settings

    # This module is imported as `grimoire.api.main:app`, so a config failure
    # here happens at uvicorn import time. Exit with a readable message instead
    # of letting a Pydantic/PyYAML traceback be the operator's only clue.
    try:
        settings = get_settings()
    except ConfigurationError as e:
        logger.error(f"Cannot start the Grimoire API - invalid configuration:\n{e}")
        raise SystemExit(1) from e
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.auth.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Added last so it is the OUTERMOST middleware (Starlette wraps
    # add_middleware calls in LIFO order - see ContentLengthGuard's
    # docstring): it must see every request before CORS, rate limiting, or
    # routing get anywhere near it, so an oversized upload never reaches
    # FastAPI's whole-body multipart parsing. It is pure-ASGI, so every
    # non-upload request (including the /mcp SSE stream) still passes
    # through this position completely unwrapped.
    app.add_middleware(ContentLengthGuard)

    # API routes
    app.include_router(ingest.router, prefix="/api/v1")
    app.include_router(query.router, prefix="/api/v1")
    app.include_router(documents.router, prefix="/api/v1")
    app.include_router(categories.router, prefix="/api/v1")
    app.include_router(generate.router, prefix="/api/v1")
    app.include_router(watch.router, prefix="/api/v1")

    # API key introspection
    from grimoire.api.routes.api_keys import router as api_keys_router

    app.include_router(api_keys_router, prefix="/api/v1")

    # MCP SSE server mounted at /mcp
    from grimoire.mcp.router import mount_mcp

    mount_mcp(app, path="/mcp")

    @app.get("/health")
    @limiter.limit("60/minute")
    async def health_check(request: Request) -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
