"""FastAPI integration for the Grimoire MCP server.

Provides the mountable ASGI application and a helper to wire it into
the main FastAPI app at ``/mcp``.
"""

from __future__ import annotations

from fastapi import FastAPI

from grimoire.api.auth import get_api_key
from grimoire.db.models import ApiKey

from .server import create_mcp_server

# Lazy singleton — created on first access so that settings are already
# resolved when the app imports this module.
_mcp_asgi_app = None


def get_mcp_app():
    """Return the cached FastMCP ASGI application."""
    global _mcp_asgi_app
    if _mcp_asgi_app is None:
        mcp_server = create_mcp_server()
        _mcp_asgi_app = mcp_server.sse_app()
    return _mcp_asgi_app


def mount_mcp(app: FastAPI, path: str = "/mcp") -> None:
    """Mount the MCP SSE application under *path* on the given FastAPI app.

    Authentication is handled by wrapping the MCP ASGI app with a middleware
    that validates ``X-API-Key`` before allowing the request through.
    """
    mcp_app = get_mcp_app()

    async def auth_middleware(scope, receive, send):
        """ASGI middleware that injects API key auth."""
        if scope["type"] == "http":
            # Extract headers from ASGI scope
            raw_key = next(
                (v.decode() for k, v in scope.get("headers", []) if k.lower() == b"x-api-key"),
                None,
            )

            if not raw_key:
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [[b"content-type", b"application/json"]],
                })
                await send({
                    "type": "http.response.body",
                    "body": b'{"detail": "API key required. Pass X-API-Key header."}',
                })
                return

            # Validate key via existing auth logic
            from grimoire.db.session import get_db_manager
            db_mgr = get_db_manager()
            async with db_mgr.session() as db:
                from grimoire.api.auth import authenticate_api_key
                api_key = await authenticate_api_key(raw_key, db)
                if api_key is None:
                    await send({
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [[b"content-type", b"application/json"]],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": b'{"detail": "Invalid or expired API key."}',
                    })
                    return

                # Store in scope state for downstream tools
                scope.setdefault("state", {})["api_key"] = api_key
                from .auth_stdio import set_current_api_key as _set_key
                _set_key(api_key)

        await mcp_app(scope, receive, send)

    app.mount(path, auth_middleware)
