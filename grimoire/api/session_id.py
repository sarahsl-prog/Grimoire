"""Per-request ``X-Session-Id`` handling for the API.

The terminal UI (and, later, the desktop GUI) send one opaque id per launch so
that a user's requests can be correlated across client and server logs.  This
module reads it, validates it, and exposes it to logging and tracing through a
``ContextVar`` and loguru's ``contextualize``.

The id is *advisory*: a missing or malformed value is ignored, never rejected,
because a client that sends garbage must still get its answer.
"""

from __future__ import annotations

import re
from contextvars import ContextVar

from loguru import logger
from starlette.types import ASGIApp, Receive, Scope, Send

SESSION_ID_HEADER = "X-Session-Id"
_HEADER_KEY = SESSION_ID_HEADER.lower().encode("latin-1")

# ``fullmatch`` on purpose: ``$`` also matches before a trailing newline, which
# would let "abc\n" through into log lines.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: The validated session id of the request being handled, if any.
current_session_id: ContextVar[str | None] = ContextVar(
    "grimoire_session_id", default=None
)


def parse_session_id(raw: bytes | str | None) -> str | None:
    """Return ``raw`` if it is a valid session id, else ``None``."""
    if raw is None:
        return None
    try:
        value = raw if isinstance(raw, str) else raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return value if _SESSION_ID_RE.fullmatch(value) else None


class SessionIdMiddleware:
    """Bind a valid ``X-Session-Id`` to the log context for each request.

    Pure ASGI (not ``BaseHTTPMiddleware``) for the same reason as
    ``ContentLengthGuard``: the mounted MCP server streams SSE, and the
    ``BaseHTTPMiddleware`` wrapper buffers and mishandles disconnects.  The
    original ``receive``/``send`` are always passed through untouched.

    If the header is repeated, the first occurrence wins.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw = next((v for k, v in scope["headers"] if k == _HEADER_KEY), None)
        session_id = parse_session_id(raw)
        if session_id is None:
            await self.app(scope, receive, send)
            return

        token = current_session_id.set(session_id)
        try:
            with logger.contextualize(session_id=session_id):
                await self.app(scope, receive, send)
        finally:
            current_session_id.reset(token)
