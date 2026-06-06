"""Authentication helpers for the stdio MCP transport.

Wraps Grimoire's existing bcrypt-hashed API key system for MCP stdio mode.
The HTTP/SSE transport uses the normal FastAPI ``Depends(get_api_key)``
path instead.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Optional

from loguru import logger

from grimoire.api.auth import authenticate_api_key
from grimoire.db.models import ApiKey, ApiKeyTier
from grimoire.db.session import get_db_context

# Context variable that holds the authenticated ApiKey for the current
# stdio session.  Populated during lifespan startup and read by tool
# decorators that enforce tier-based access.
_stdio_api_key: ContextVar[Optional[ApiKey]] = ContextVar("_stdio_api_key", default=None)


async def authenticate_stdio_key(raw_key: str | None = None) -> ApiKey:
    """Validate the API key provided for stdio transport.

    Reads ``GRIMOIRE_API_KEY`` from the environment when *raw_key* is not
    supplied, then performs the same prefix+bcrypt lookup that the REST
    API uses.

    Args:
        raw_key: Optional explicit key.  Falls back to the env var.

    Returns:
        The authenticated :class:`~grimoire.db.models.ApiKey`.

    Raises:
        RuntimeError: If no key is available or authentication fails.
    """
    if raw_key is None:
        raw_key = os.getenv("GRIMOIRE_API_KEY", "")

    if not raw_key:
        raise RuntimeError(
            "MCP stdio transport requires an API key. "
            "Set GRIMOIRE_API_KEY environment variable."
        )

    async with get_db_context() as db:
        api_key = await authenticate_api_key(raw_key, db)

    if api_key is None:
        raise RuntimeError("Invalid or expired API key.")

    logger.info(f"MCP stdio authenticated: {api_key.name} ({api_key.tier.value})")
    return api_key


def get_current_api_key() -> ApiKey:
    """Return the currently authenticated API key for stdio transport.

    Raises:
        RuntimeError: If called before the lifespan has authenticated.
    """
    key = _stdio_api_key.get()
    if key is None:
        raise RuntimeError("No API key in context.  Server lifespan not complete?")
    return key


def set_current_api_key(key: ApiKey) -> None:
    """Store the authenticated key in the context variable."""
    _stdio_api_key.set(key)


def require_tier(*tiers: ApiKeyTier) -> None:
    """Enforce that the current stdio session holds a key of the given tier(s).

    Args:
        tiers: One or more tiers that are permitted.

    Raises:
        RuntimeError: If the current key is missing or not in *tiers*.
    """
    key = get_current_api_key()
    if key.tier not in tiers:
        tier_names = ", ".join(t.value for t in tiers)
        raise RuntimeError(
            f"Tool requires API key tier in ({tier_names}).  "
            f"Current key '{key.name}' is tier '{key.tier.value}'."
        )
