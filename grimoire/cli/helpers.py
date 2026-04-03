"""Shared CLI helpers for dependency setup and output formatting."""

from __future__ import annotations

import asyncio
import functools
import sys
from typing import Any, Callable

import click
from loguru import logger

from grimoire.config.settings import get_settings


def async_command(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to run async Click commands."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(func(*args, **kwargs))

    return wrapper


def get_db_url() -> str:
    """Resolve database URL from settings."""
    settings = get_settings()
    url = settings.database.url
    # Use dev_url if the primary is not reachable (simple heuristic)
    if not url or url == "postgresql+asyncpg://":
        url = settings.database.dev_url
    return url


async def setup_db() -> None:
    """Initialize the database connection."""
    from grimoire.db.session import initialize_db

    url = get_db_url()
    settings = get_settings()
    await initialize_db(url, pool_size=settings.database.pool_size)


async def teardown_db() -> None:
    """Close the database connection."""
    from grimoire.db.session import close_db

    await close_db()


def build_ingestion_agent() -> Any:
    """Create an IngestionAgent from current settings."""
    from grimoire.core.cache import CacheFactory
    from grimoire.core.embedder import Embedder, EmbeddingConfig
    from grimoire.core.parser import DocumentParser
    from grimoire.core.tagger import Tagger
    from grimoire.vectorstore.chromadb import ChromaDBStore
    from grimoire.agents.ingestion import IngestionAgent

    settings = get_settings()

    parser = DocumentParser()
    embed_config = EmbeddingConfig(
        model=settings.embeddings.model,
        fallback_model=settings.embeddings.fallback_model,
        device=settings.embeddings.device,
        batch_size=settings.embeddings.batch_size,
    )
    cache = CacheFactory.create(
        backend=settings.cache.storage,
        path=settings.cache.path,
    )
    embedder = Embedder(config=embed_config, cache=cache)

    vector_store = ChromaDBStore(
        persist_directory=settings.vector_store.chromadb.path,
        collection_name=settings.vector_store.chromadb.collection_name,
    )

    tagger = Tagger(settings)

    return IngestionAgent(
        parser=parser,
        embedder=embedder,
        vector_store=vector_store,
        tagger=tagger,
    )


def build_query_agent() -> Any:
    """Create a QueryAgent from current settings."""
    from grimoire.core.cache import CacheFactory
    from grimoire.core.embedder import Embedder, EmbeddingConfig
    from grimoire.search.hybrid import HybridSearch
    from grimoire.vectorstore.chromadb import ChromaDBStore
    from grimoire.agents.query import QueryAgent

    settings = get_settings()

    embed_config = EmbeddingConfig(
        model=settings.embeddings.model,
        device=settings.embeddings.device,
        batch_size=settings.embeddings.batch_size,
    )
    cache = CacheFactory.create(
        backend=settings.cache.storage,
        path=settings.cache.path,
    )
    embedder = Embedder(config=embed_config, cache=cache)

    vector_store = ChromaDBStore(
        persist_directory=settings.vector_store.chromadb.path,
        collection_name=settings.vector_store.chromadb.collection_name,
    )

    hybrid = HybridSearch(
        vector_store=vector_store,
        embedder=embedder,
        vector_weight=settings.query.hybrid_alpha,
        fts_weight=1.0 - settings.query.hybrid_alpha,
    )

    return QueryAgent(
        hybrid_search=hybrid,
        llm_url=settings.llm.url,
        llm_model=settings.llm.model,
        cache=cache,
        temperature=settings.llm.temperature,
        max_tokens=settings.llm.max_tokens,
    )


def build_content_gen_agent() -> Any:
    """Create a ContentGenerationAgent from current settings."""
    from grimoire.core.cache import CacheFactory
    from grimoire.agents.content_gen import ContentGenerationAgent

    settings = get_settings()

    cache = CacheFactory.create(
        backend=settings.cache.storage,
        path=settings.cache.path,
    )

    return ContentGenerationAgent(
        llm_url=settings.llm.url,
        llm_model=settings.llm.model,
        cache=cache,
        temperature=settings.llm.temperature,
        max_tokens=settings.llm.max_tokens,
    )


def get_db_context():
    """Get async DB context manager. Import wrapper for testability."""
    from grimoire.db.session import get_db_context as _ctx
    return _ctx()


def echo_error(msg: str) -> None:
    """Print an error message in red."""
    click.echo(click.style(f"Error: {msg}", fg="red"), err=True)


def echo_success(msg: str) -> None:
    """Print a success message in green."""
    click.echo(click.style(msg, fg="green"))


def echo_warning(msg: str) -> None:
    """Print a warning message in yellow."""
    click.echo(click.style(msg, fg="yellow"))
