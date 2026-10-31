"""FastAPI application factory for the Grimoire REST API."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from grimoire import __version__
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
    from grimoire.db.session import close_db, get_db_manager, initialize_db

    settings = get_settings()
    await initialize_db(settings.database.url)

    # Initialize watcher agent for REST /watch endpoints
    watcher_agent = None
    try:
        from grimoire.agents.watcher import WatcherAgent
        from grimoire.api.routes import watch as watch_module
        from grimoire.cli.helpers import build_ingestion_agent
        from grimoire.storage.watch_manager import WatchManager

        db_manager = get_db_manager()
        watcher_agent = WatcherAgent(
            watch_manager=WatchManager(),
            ingestion_agent=build_ingestion_agent(),
            db_session_factory=db_manager.session,
        )
        watch_module.set_watcher(watcher_agent)
        app.state.watcher_status = "ok"
        logger.info("WatcherAgent initialized for REST /watch endpoints")
    except Exception as e:
        # The rest of the API still works, so this doesn't stop startup; but
        # /watch returns 503 until a restart, so make it visible: an ERROR log
        # and a "watcher" entry in /readyz.
        app.state.watcher_status = "error"
        logger.error(
            f"Failed to initialize WatcherAgent; /watch endpoints will return "
            f"503: {type(e).__name__}: {e}"
        )

    try:
        yield
    finally:
        if watcher_agent is not None:
            await watcher_agent.stop_all()
        await close_db()


def create_app(use_lifespan: bool = True) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Grimoire",
        description="Knowledge management and content generation API.",
        version=__version__,
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

    # Outermost, so even a request ContentLengthGuard rejects is logged with its
    # session id. Pure-ASGI, so /mcp SSE streaming is unaffected.
    from grimoire.api.session_id import SessionIdMiddleware

    app.add_middleware(SessionIdMiddleware)

    # OpenTelemetry instrumentation (if enabled in settings)
    try:
        settings = get_settings()
        if settings.observability.tracing:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            from grimoire.utils.tracing import configure_tracing

            configure_tracing()

            FastAPIInstrumentor.instrument_app(app)
            logger.info("OpenTelemetry FastAPI instrumentation enabled")
    except Exception as e:
        # OTel is optional, so failure must not prevent startup, but it must
        # not be silent either: tracing that quietly stops is worse than none.
        logger.warning(
            f"OpenTelemetry FastAPI instrumentation failed: {type(e).__name__}: {e}"
        )

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

    # /health is a liveness probe (Docker, the TUI status bar), so it must not
    # fail just because Redis is down.  It keeps its W-7 rate limit, but on a
    # dedicated in-memory limiter, and is exempt from the Redis-backed one.
    from grimoire.api.rate_limit import create_health_limiter

    health_limiter = create_health_limiter()

    @app.get("/health")
    @health_limiter.limit("60/minute")
    async def health_check(request: Request) -> dict[str, str]:
        return {"status": "ok"}

    # Called, not used as a decorator: slowapi's `exempt` is untyped, and as a
    # decorator it would make `health_check` untyped.  It only records the route
    # name, so the Redis-backed limiter's default limit skips /health.
    limiter.exempt(health_check)  # type: ignore[no-untyped-call]

    @app.get("/readyz")
    async def readyz_check(request: Request) -> dict[str, Any]:
        """Readiness probe: pings Postgres, ChromaDB, and Redis.

        Returns 503 if any dependency is unreachable. Also reports whether the
        filesystem watcher behind ``/watch`` started (``watcher``: ok, error or
        disabled); that doesn't affect readiness.
        """
        checks = {
            "postgres": await _check_postgres(),
            "chromadb": await _check_chromadb(),
            "redis": await _check_redis(),
        }
        healthy = all(v in ("ok", "disabled") for v in checks.values())
        # Reported, but not part of readiness: without the watcher only the
        # /watch endpoints are unavailable (they return 503).
        checks["watcher"] = getattr(request.app.state, "watcher_status", "disabled")

        if not healthy:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=503,
                detail={"status": "not_ready", "checks": checks},
            )

        return {"status": "ready", "checks": checks}

    return app


# ---------------------------------------------------------------------------
# /readyz dependency checks. Each returns "ok", "error", or a state such as
# "not_initialized" / "disabled". The endpoint is unauthenticated (and public
# behind the proxy), so causes (hostnames, ports, driver messages) go to the
# log, never into the response.
# ---------------------------------------------------------------------------


def _log_readyz_failure(dependency: str, exc: Exception) -> None:
    logger.warning(f"/readyz: {dependency} check failed: {type(exc).__name__}: {exc}")


async def _check_postgres() -> str:
    try:
        from sqlalchemy import text

        from grimoire.db.session import get_db_manager

        db_manager = get_db_manager()
        if db_manager._engine is None:
            return "not_initialized"
        async with db_manager._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as e:
        _log_readyz_failure("postgres", e)
        return "error"


async def _check_chromadb() -> str:
    try:
        from grimoire.vectorstore.factory import get_maintenance_store

        # count() makes a real round trip, so a cached store still proves the
        # server is reachable right now.
        store = await get_maintenance_store()
        await store.count()
        return "ok"
    except Exception as e:
        _log_readyz_failure("chromadb", e)
        return "error"


async def _check_redis() -> str:
    try:
        from grimoire.config.settings import get_settings

        settings = get_settings()
        if not settings.redis.host:
            return "disabled"
        import redis.asyncio as aioredis

        r = aioredis.from_url(
            f"redis://{settings.redis.host}:{settings.redis.port}",
            password=settings.redis.password or None,
        )
        try:
            # redis-py types commands as `Awaitable[T] | T` for both clients;
            # the asyncio client always returns an awaitable.
            await cast(Awaitable[bool], r.ping())
        finally:
            await r.aclose()
        return "ok"
    except Exception as e:
        _log_readyz_failure("redis", e)
        return "error"


app = create_app()
