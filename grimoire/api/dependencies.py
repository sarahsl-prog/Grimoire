"""FastAPI dependency injection for database sessions and services."""

from __future__ import annotations

import functools
from typing import Any, AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.db.session import get_db


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async database session."""
    async for session in get_db():
        yield session


@functools.lru_cache()
def _get_ingestion_agent_cached() -> Any:
    """Build and cache IngestionAgent instance."""
    from grimoire.cli.helpers import build_ingestion_agent
    return build_ingestion_agent()


def get_ingestion_agent() -> Any:
    """Return cached IngestionAgent instance."""
    return _get_ingestion_agent_cached()


@functools.lru_cache()
def _get_query_agent_cached() -> Any:
    """Build and cache QueryAgent instance."""
    from grimoire.cli.helpers import build_query_agent
    return build_query_agent()


def get_query_agent() -> Any:
    """Return cached QueryAgent instance."""
    return _get_query_agent_cached()


@functools.lru_cache()
def _get_content_gen_agent_cached() -> Any:
    """Build and cache ContentGenerationAgent instance."""
    from grimoire.cli.helpers import build_content_gen_agent
    return build_content_gen_agent()


def get_content_gen_agent() -> Any:
    """Return cached ContentGenerationAgent instance."""
    return _get_content_gen_agent_cached()
