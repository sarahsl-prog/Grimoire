"""FastAPI dependency injection for database sessions and services."""

from __future__ import annotations

import functools
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.db.session import get_db


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async database session."""
    async for session in get_db():
        yield session


async def get_by_uuid[ModelT](
    db: AsyncSession, model: type[ModelT], raw_id: str
) -> ModelT | None:
    """Look up a row by a UUID primary key taken from the URL.

    Path parameters arrive as free text. Handing a malformed one straight to
    ``db.get`` makes asyncpg reject it against the Postgres ``UUID`` column,
    which surfaced as a 500. An ID that is not a UUID cannot name a row, so it
    is reported the same way as an unknown one: ``None``, which callers turn
    into a 404.

    Args:
        db: Database session.
        model: Mapped class whose primary key is a ``UUID`` column.
        raw_id: Identifier exactly as the client sent it.

    Returns:
        The row, or ``None`` when ``raw_id`` is not a UUID or no row matches.
    """
    try:
        uuid.UUID(raw_id)
    except (ValueError, TypeError):
        return None
    return await db.get(model, raw_id)


@functools.lru_cache
def _get_ingestion_agent_cached() -> Any:
    """Build and cache IngestionAgent instance."""
    from grimoire.cli.helpers import build_ingestion_agent

    return build_ingestion_agent()


def get_ingestion_agent() -> Any:
    """Return cached IngestionAgent instance."""
    return _get_ingestion_agent_cached()


@functools.lru_cache
def _get_query_agent_cached() -> Any:
    """Build and cache QueryAgent instance."""
    from grimoire.cli.helpers import build_query_agent

    return build_query_agent()


def get_query_agent() -> Any:
    """Return cached QueryAgent instance."""
    return _get_query_agent_cached()


@functools.lru_cache
def _get_content_gen_agent_cached() -> Any:
    """Build and cache ContentGenerationAgent instance."""
    from grimoire.cli.helpers import build_content_gen_agent

    return build_content_gen_agent()


def get_content_gen_agent() -> Any:
    """Return cached ContentGenerationAgent instance."""
    return _get_content_gen_agent_cached()
