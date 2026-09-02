"""MCPServer builder for Grimoire.

Provides ``create_mcp_server()`` which returns a configured ``MCPServer``
instance with all Grimoire tools registered.  Supports both HTTP/SSE
(when mounted inside the FastAPI app) and stdio (when run standalone).

``MCPServer`` is the mcp 2.x name for what mcp 1.x called ``FastMCP``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from loguru import logger
from mcp.server.mcpserver import MCPServer

from grimoire.db.session import initialize_db, close_db
from grimoire.config.settings import get_settings

from .auth_stdio import authenticate_stdio_key, set_current_api_key
from . import tools
from .mlflow_logging import configure_mlflow, shutdown_mlflow, trace_mcp_tool


@asynccontextmanager
async def _grimoire_lifespan(app: MCPServer) -> AsyncGenerator[dict[str, Any], None]:
    """Shared lifespan for both transports.

    Initialises the database connection.  For stdio transport the
    ``GRIMOIRE_API_KEY`` env var is also validated here so that every
    tool call can safely assume an authenticated key is present.
    """
    settings = get_settings()
    await initialize_db(settings.database.url, pool_size=settings.database.pool_size)
    logger.info("MCP lifespan: database initialised")
    configure_mlflow(settings.observability)

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
        try:
            watcher = tools._get_mcp_watcher()
            await watcher.stop_all()
            logger.info("MCP lifespan: stopped active watchers")
        except Exception as e:
            # Watcher may never have been instantiated; log but don't fail shutdown.
            logger.debug(f"MCP lifespan: watcher cleanup skipped: {e}")
        shutdown_mlflow()
        await close_db()
        logger.info("MCP lifespan: database closed")


def _register_tool(mcp: MCPServer, func: Any, name: str) -> None:
    """Register an MCP tool, wrapping it with MLflow tracing when enabled."""
    mcp.add_tool(trace_mcp_tool(func, name=name), name=name)


def create_mcp_server() -> MCPServer:
    """Build and return an MCPServer with all Grimoire tools registered."""
    settings = get_settings()
    configure_mlflow(settings.observability)

    mcp = MCPServer("grimoire", lifespan=_grimoire_lifespan)

    # Register read-only tools (available to all tiers)
    _register_tool(mcp, tools.grimoire_search, "grimoire_search")
    _register_tool(mcp, tools.grimoire_search_cve, "grimoire_search_cve")
    _register_tool(mcp, tools.grimoire_search_playbook, "grimoire_search_playbook")
    _register_tool(mcp, tools.grimoire_ask, "grimoire_ask")
    _register_tool(mcp, tools.grimoire_get_document, "grimoire_get_document")
    _register_tool(mcp, tools.grimoire_list_documents, "grimoire_list_documents")
    _register_tool(mcp, tools.grimoire_list_categories, "grimoire_list_categories")
    _register_tool(mcp, tools.grimoire_watch_status, "grimoire_watch_status")
    _register_tool(mcp, tools.grimoire_status, "grimoire_status")

    # Register write tools (DEV + AGENT tiers)
    _register_tool(mcp, tools.grimoire_ingest_file, "grimoire_ingest_file")
    _register_tool(mcp, tools.grimoire_ingest_directory, "grimoire_ingest_directory")
    _register_tool(mcp, tools.grimoire_generate, "grimoire_generate")
    _register_tool(mcp, tools.grimoire_create_category, "grimoire_create_category")
    _register_tool(mcp, tools.grimoire_watch_start, "grimoire_watch_start")
    _register_tool(mcp, tools.grimoire_watch_stop, "grimoire_watch_stop")
    _register_tool(mcp, tools.grimoire_pg_query, "grimoire_pg_query")  # DEV+ only

    # Register destructive tools (AGENT tier only)
    _register_tool(mcp, tools.grimoire_delete_document, "grimoire_delete_document")

    logger.info("MCP server created with Grimoire tools")
    return mcp
