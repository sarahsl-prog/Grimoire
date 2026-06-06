"""MCP server integration for Grimoire.

Provides both HTTP/SSE and stdio transports for MCP-capable clients
to interact with the Grimoire knowledge base.
"""

from __future__ import annotations

from grimoire.mcp.server import create_mcp_server

__all__ = ["create_mcp_server"]
