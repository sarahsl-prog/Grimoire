"""Click subcommand for the Grimoire MCP server.

Usage:
    grimoire mcp --stdio          # stdio transport (Claude Code, etc.)
    grimoire mcp --sse --port 8100  # Standalone SSE server

For HTTP/SSE inside the main FastAPI app, see ``grimoire.api.main``.
"""

from __future__ import annotations

import asyncio

import click
from loguru import logger

from grimoire.cli.helpers import async_command, setup_db, teardown_db
from grimoire.mcp.auth_stdio import authenticate_stdio_key, set_current_api_key
from grimoire.mcp.server import create_mcp_server


@click.command(name="mcp")
@click.option("--stdio", is_flag=True, default=False, help="Run stdio transport (default).")
@click.option("--sse", is_flag=True, default=False, help="Run standalone SSE transport.")
@click.option("--host", default="0.0.0.0", help="Bind host for SSE mode.")
@click.option("--port", default=8100, type=int, help="Port for SSE mode.")
@async_command
async def mcp(stdio: bool, sse: bool, host: str, port: int) -> None:
    """Start the Grimoire MCP server."""
    # Default to stdio if no transport specified
    if not stdio and not sse:
        stdio = True

    await setup_db()
    try:
        if stdio:
            logger.info("Starting Grimoire MCP server (stdio transport)")
            # Authenticate eagerly so that tools can assume a valid key
            api_key = await authenticate_stdio_key()
            set_current_api_key(api_key)
            mcp_server = create_mcp_server()
            mcp_server.run()  # stdio by default
        elif sse:
            logger.info(f"Starting Grimoire MCP server (SSE transport on {host}:{port})")
            mcp_server = create_mcp_server()
            from uvicorn import Config, Server
            config = Config(
                app=mcp_server.sse_app(),
                host=host,
                port=port,
                log_level="info",
            )
            server = Server(config)
            await server.serve()
    finally:
        await teardown_db()
