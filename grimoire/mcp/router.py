"""FastAPI integration for the Grimoire MCP server.

Provides the mountable ASGI application and a helper to wire it into
the main FastAPI app at ``/mcp``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.types import Receive, Scope, Send

from .server import create_mcp_server

# Lazy singleton — created on first access so that settings are already
# resolved when the app imports this module.
_mcp_asgi_app: Any = None


def get_mcp_app() -> Any:
    """Return the cached MCPServer ASGI application."""
    global _mcp_asgi_app
    if _mcp_asgi_app is None:
        mcp_server = create_mcp_server()
        _mcp_asgi_app = mcp_server.sse_app()
    return _mcp_asgi_app


async def _send_json(
    send: Send,
    status: int,
    detail: str,
    headers: Mapping[str, str] | None = None,
) -> None:
    """Send a complete JSON error response from raw ASGI."""
    raw_headers = [[b"content-type", b"application/json"]]
    raw_headers += [
        [k.lower().encode(), v.encode()] for k, v in (headers or {}).items()
    ]
    await send(
        {"type": "http.response.start", "status": status, "headers": raw_headers}
    )
    await send(
        {"type": "http.response.body", "body": json.dumps({"detail": detail}).encode()}
    )


def mount_mcp(app: FastAPI, path: str = "/mcp") -> None:
    """Mount the MCP SSE application under *path* on the given FastAPI app.

    Authentication is handled by wrapping the MCP ASGI app with a middleware
    that validates ``X-API-Key`` before allowing the request through.
    """
    mcp_app = get_mcp_app()

    async def auth_middleware(scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI middleware that injects API key auth."""
        if scope["type"] != "http":
            # No non-HTTP (e.g. websocket) routes are mounted under this
            # path today, but this port is published outside localhost now,
            # so an unrecognized scope type must be rejected rather than
            # forwarded to the MCP app unauthenticated. Starlette only ever
            # routes "http" and "websocket" scopes to a mounted app (never
            # "lifespan"), so this only ever rejects a websocket handshake.
            await send({"type": "websocket.close", "code": 1008})
            return

        # Extract headers from ASGI scope
        raw_key = next(
            (
                v.decode()
                for k, v in scope.get("headers", [])
                if k.lower() == b"x-api-key"
            ),
            None,
        )

        if not raw_key:
            await _send_json(send, 401, "API key required. Pass X-API-Key header.")
            return

        # The same rate-limit stages as REST (grimoire.api.rate_limit): a
        # Request over this scope finds the app's limiter via scope["app"].
        from grimoire.api.rate_limit import (
            check_failed_auth,
            enforce_tier_limit,
            record_failed_auth,
        )

        request = Request(scope)
        try:
            check_failed_auth(request)
        except HTTPException as exc:
            await _send_json(send, exc.status_code, str(exc.detail), exc.headers)
            return

        # Validate key via existing auth logic
        from grimoire.api.auth import authenticate_api_key
        from grimoire.db.session import get_db_manager

        try:
            db_mgr = get_db_manager()
        except RuntimeError:
            await _send_json(send, 503, "Database not initialized.")
            return

        async with db_mgr.session() as db:
            api_key = await authenticate_api_key(raw_key, db)
            if api_key is None:
                record_failed_auth(request)
                await _send_json(send, 401, "Invalid or expired API key.")
                return

            try:
                enforce_tier_limit(request, api_key)
            except HTTPException as exc:
                await _send_json(send, exc.status_code, str(exc.detail), exc.headers)
                return

            # Store in scope state for downstream tools
            scope.setdefault("state", {})["api_key"] = api_key
            from .auth_stdio import set_current_api_key as _set_key

            _set_key(api_key)

        await mcp_app(scope, receive, send)

    app.mount(path, auth_middleware)
