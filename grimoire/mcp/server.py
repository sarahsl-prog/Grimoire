"""FastMCP server builder for Grimoire.

Provides ``create_mcp_server()`` which returns a configured ``FastMCP``
instance with all Grimoire tools registered.  Supports both HTTP/SSE
(when mounted inside the FastAPI app) and stdio (when run standalone).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from loguru import logger
from mcp.server.fastmcp import FastMCP

from grimoire.db.session import initialize_db, close_db
from grimoire.config.settings import get_settings

from .auth_stdio import authenticate_stdio_key, set_current_api_key
from . import tools


@asynccontextmanager
async def _grimoire_lifespan(app: FastMCP) -> AsyncGenerator[dict[str, Any], None]:
    """Shared lifespan for both transports.

    Initialises the database connection.  For stdio transport the
    ``GRIMOIRE_API_KEY`` env var is also validated here so that every
    tool call can safely assume an authenticated key is present.
    """
    settings = get_settings()
    await initialize_db(settings.database.url, pool_size=settings.database.pool_size)
    logger.info("MCP lifespan: database initialised")

    # Validate GRIMOIRE_API_KEY eagerly if set (primarily for stdio mode).
    # HTTP/SSE validates per-request in the ASGI middleware instead.
    import os as _os
    _raw_key = _os.getenv("GRIMOIRE_API_KEY", "")
    if _raw_key:
        try:
            api_key = await authenticate_stdio_key(_raw_key)
            set_current_api_key(api_key)
        except RuntimeError as e:
            logger.error(f"MCP lifespan: GRIMOIRE_API_KEY is set but invalid: {e}")
            raise
    else:
        logger.info("MCP lifespan: no GRIMOIRE_API_KEY set (SSE validates per-request)")

    try:
        yield {}
    finally:
        await close_db()
        logger.info("MCP lifespan: database closed")


def create_mcp_server() -> FastMCP:
    """Build and return a FastMCP server with all Grimoire tools registered."""
    mcp = FastMCP("grimoire", lifespan=_grimoire_lifespan)

    # Register read-only tools (available to all tiers)
    mcp.add_tool(tools.grimoire_search, name="grimoire_search")
    mcp.add_tool(tools.grimoire_ask, name="grimoire_ask")
    mcp.add_tool(tools.grimoire_get_document, name="grimoire_get_document")
    mcp.add_tool(tools.grimoire_list_documents, name="grimoire_list_documents")
    mcp.add_tool(tools.grimoire_list_categories, name="grimoire_list_categories")
    mcp.add_tool(tools.grimoire_watch_status, name="grimoire_watch_status")
    mcp.add_tool(tools.grimoire_status, name="grimoire_status")

    # Register write tools (DEV + AGENT tiers)
    mcp.add_tool(tools.grimoire_ingest_file, name="grimoire_ingest_file")
    mcp.add_tool(tools.grimoire_ingest_directory, name="grimoire_ingest_directory")
    mcp.add_tool(tools.grimoire_generate, name="grimoire_generate")
    mcp.add_tool(tools.grimoire_create_category, name="grimoire_create_category")
    mcp.add_tool(tools.grimoire_watch_start, name="grimoire_watch_start")
    mcp.add_tool(tools.grimoire_pg_query, name="grimoire_pg_query")  # DEV+ only

    # Register destructive tools (AGENT tier only)
    mcp.add_tool(tools.grimoire_delete_document, name="grimoire_delete_document")

    logger.info("MCP server created with Grimoire tools")
    return mcp
