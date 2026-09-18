"""Standalone ASGI application for the Grimoire MCP server.

Serves the MCP tools over SSE behind the same ``X-API-Key`` middleware the
REST API applies, so no transport exposes the corpus unauthenticated.  Run
it with ``uvicorn grimoire.mcp.app:app`` or via ``grimoire mcp --sse``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Open and close the database pool the auth middleware depends on."""
    from grimoire.config.settings import get_settings
    from grimoire.db.session import close_db, initialize_db

    settings = get_settings()
    await initialize_db(settings.database.url)
    try:
        yield
    finally:
        await close_db()


def create_mcp_app(use_lifespan: bool = True) -> FastAPI:
    """Build the FastAPI app that serves only the authenticated MCP endpoint.

    Args:
        use_lifespan: Whether to manage the database pool.  Tests pass False.

    Returns:
        A FastAPI application with MCP mounted at ``/mcp``.
    """
    from grimoire.config.settings import ConfigurationError, get_settings
    from grimoire.mcp.router import mount_mcp

    # Imported as `grimoire.mcp.app:app`, so a config failure surfaces at
    # uvicorn import time. Exit with a readable message rather than a
    # Pydantic traceback, matching grimoire/api/main.py:49.
    try:
        get_settings()
    except ConfigurationError as e:
        logger.error(
            f"Cannot start the Grimoire MCP server - invalid configuration:\n{e}"
        )
        raise SystemExit(1) from e

    app = FastAPI(
        title="Grimoire MCP",
        description="Model Context Protocol server for the Grimoire knowledge base.",
        version="2.0.0",
        lifespan=lifespan if use_lifespan else None,
    )

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    mount_mcp(app, path="/mcp")

    return app


app = create_mcp_app()
