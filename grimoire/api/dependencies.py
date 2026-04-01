"""FastAPI dependency injection for database sessions and services."""

from __future__ import annotations

from typing import Any, AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.config.settings import get_settings
from grimoire.db.session import get_db


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async database session."""
    async for session in get_db():
        yield session


def get_ingestion_agent() -> Any:
    """Build and return an IngestionAgent."""
    from grimoire.cli.helpers import build_ingestion_agent

    return build_ingestion_agent()


def get_query_agent() -> Any:
    """Build and return a QueryAgent."""
    from grimoire.cli.helpers import build_query_agent

    return build_query_agent()


def get_content_gen_agent() -> Any:
    """Build and return a ContentGenerationAgent."""
    from grimoire.cli.helpers import build_content_gen_agent

    return build_content_gen_agent()
