"""Connections as the ``grimoire_readonly`` role (migration 0008).

Used only by the MCP ``pg_query`` tool, so that arbitrary user SQL runs with a
role that can't read ``api_keys`` or write anything, instead of the
application's own role behind a regex. The engine is separate from the main
pool, small, and created on first use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from grimoire.config.settings import GrimoireSettings

_engine: AsyncEngine | None = None
_engine_url: str | None = None


def readonly_url(settings: GrimoireSettings | None = None) -> str | None:
    """The configured read-only URL, or None when pg_query is disabled."""
    from grimoire.config.settings import get_settings

    return (settings or get_settings()).database.readonly_url


def _get_engine(url: str) -> AsyncEngine:
    global _engine, _engine_url
    if _engine is None or _engine_url != url:
        # A couple of connections is plenty for ad-hoc queries, and keeps a
        # flood of pg_query calls from taking the main pool's connections.
        _engine = create_async_engine(
            url, pool_size=2, max_overflow=0, pool_pre_ping=True
        )
        _engine_url = url
        logger.info("Read-only database engine created for pg_query")
    return _engine


@asynccontextmanager
async def readonly_session(
    settings: GrimoireSettings | None = None,
) -> AsyncIterator[AsyncSession]:
    """Yield a session connected as ``grimoire_readonly``; always rolls back.

    Raises:
        RuntimeError: If ``database.readonly_url`` is not configured.
    """
    url = readonly_url(settings)
    if url is None:
        raise RuntimeError("database.readonly_url is not configured")
    maker = async_sessionmaker(_get_engine(url), expire_on_commit=False)
    async with maker() as session:
        try:
            yield session
        finally:
            await session.rollback()


async def close_readonly_engine() -> None:
    """Dispose of the read-only engine, if one was created."""
    global _engine, _engine_url
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _engine_url = None
