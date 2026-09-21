"""CLI commands for system status and maintenance."""

from __future__ import annotations

from typing import Any

import click
from loguru import logger

from grimoire.cli.helpers import (
    async_command,
    echo_success,
    get_db_context,
    setup_db,
    teardown_db,
)
from grimoire.config.settings import VectorStoreType, get_settings
from grimoire.core.cache import CacheFactory, DiskCache


def _vector_store_summary(settings: Any) -> str:
    """Describe the configured vector store backend without connecting to it."""
    vs = settings.vector_store
    if vs.type == VectorStoreType.QDRANT:
        return f"qdrant ({vs.qdrant.url})"
    if vs.host:
        return f"chromadb (remote {vs.host}:{vs.port or 8000})"
    return f"chromadb (embedded, path={vs.chromadb.path})"


@click.command()
@click.option("--detailed", is_flag=True, help="Show detailed statistics.")
@click.pass_context
@async_command
async def status(ctx: click.Context, detailed: bool) -> None:
    """Show system status and document statistics.

    Examples:

        grimoire status

        grimoire status --detailed
    """
    settings = get_settings()
    await setup_db()
    try:
        from sqlalchemy import func, select

        from grimoire.db.models import Category, Document, ProcessingStatus

        async with get_db_context() as db:
            # Total documents
            total = (await db.execute(select(func.count(Document.id)))).scalar() or 0

            # By status
            status_counts = {}
            for ps in ProcessingStatus:
                stmt = select(func.count(Document.id)).where(
                    Document.processing_status == ps
                )
                count = (await db.execute(stmt)).scalar() or 0
                if count > 0:
                    status_counts[ps.value] = count

            # Categories
            cat_count = (
                await db.execute(select(func.count(Category.id)))
            ).scalar() or 0

        click.echo(click.style("Grimoire Status", bold=True))
        click.echo(f"  Documents:  {total}")
        click.echo(f"  Categories: {cat_count}")
        click.echo(f"  Vector store: {_vector_store_summary(settings)}")

        if status_counts:
            click.echo("\n  Processing status:")
            for s, c in status_counts.items():
                click.echo(f"    {s:<12} {c}")

        if detailed:
            from grimoire.db.models import Chunk, GeneratedContent

            async with get_db_context() as db:
                chunk_count = (
                    await db.execute(select(func.count(Chunk.id)))
                ).scalar() or 0
                gen_count = (
                    await db.execute(select(func.count(GeneratedContent.id)))
                ).scalar() or 0

            click.echo(f"\n  Chunks:     {chunk_count}")
            click.echo(f"  Generated:  {gen_count}")

            # Vector store reachability + drift check against Postgres.
            # A chunk row lands in Postgres before its embedding reaches the
            # vector store, and the two can point at entirely different
            # ChromaDB instances (see docs/deploy/docker.md's host-vs-container
            # vector-store split footgun) -- a count mismatch is the fastest
            # signal something is out of sync.
            if settings.vector_store.type == VectorStoreType.CHROMADB:
                from grimoire.vectorstore.chromadb import ChromaDBStore

                vector_store = ChromaDBStore(
                    persist_directory=settings.vector_store.chromadb.path,
                    collection_name=settings.vector_store.chromadb.collection_name,
                    host=settings.vector_store.host,
                    port=settings.vector_store.port,
                )
                try:
                    await vector_store.initialize(
                        settings.vector_store.chromadb.collection_name,
                        embedding_dim=1,
                    )
                    vector_count = await vector_store.count()
                    click.echo(f"\n  Vector store: {vector_count} embeddings")
                    if vector_count != chunk_count:
                        click.echo(
                            click.style(
                                f"    WARNING: {chunk_count} chunks in Postgres "
                                f"vs {vector_count} embeddings in the vector "
                                "store -- they may be out of sync.",
                                fg="yellow",
                            )
                        )
                except Exception as e:
                    click.echo(
                        click.style(f"\n  Vector store: unreachable ({e})", fg="red")
                    )

            # Cache stats
            try:
                cache = CacheFactory.create(
                    backend=settings.cache.storage, path=settings.cache.path
                )
                if isinstance(cache, DiskCache):
                    stats = cache.get_stats()
                    click.echo("\n  Cache:")
                    click.echo(f"    Size:     {stats.get('size', 0)} items")
                    click.echo(f"    Disk:     {stats.get('volume', 0)} bytes")
                else:
                    # Redis cache -- same fields `grimoire cache stats` reports
                    # for this backend, so --detailed doesn't go quiet on it.
                    info = (
                        await cache.client.info() if hasattr(cache, "client") else {}
                    )
                    click.echo("\n  Cache (redis):")
                    click.echo(
                        f"    Version:  {info.get('redis_version', 'Unknown')}"
                    )
                    click.echo(
                        f"    Clients:  {info.get('connected_clients', 'Unknown')}"
                    )
            except Exception as e:
                # A status command should say why it could not read cache stats.
                logger.debug(f"Could not read cache stats: {e}")
    finally:
        await teardown_db()


@click.group("cache")
def cache_group() -> None:
    """Cache management commands."""


@cache_group.command("clear")
@click.option("--confirm/--no-confirm", default=True, help="Require confirmation.")
@click.pass_context
@async_command
async def cache_clear(ctx: click.Context, confirm: bool) -> None:
    """Clear all cached data.

    Examples:

        grimoire cache clear

        grimoire cache clear --no-confirm
    """
    if confirm and not click.confirm("Clear all cached data?"):
        return

    settings = get_settings()
    cache = CacheFactory.create(
        backend=settings.cache.storage, path=settings.cache.path
    )
    await cache.clear()
    echo_success("Cache cleared.")


@cache_group.command("stats")
@click.pass_context
@async_command
async def cache_stats(ctx: click.Context) -> None:
    """Show cache statistics.

    Examples:

        grimoire cache stats
    """
    settings = get_settings()
    cache = CacheFactory.create(
        backend=settings.cache.storage, path=settings.cache.path
    )

    click.echo(click.style("Cache Statistics", bold=True))
    click.echo(f"  Backend: {settings.cache.storage}")

    if isinstance(cache, DiskCache):
        stats = cache.get_stats()
        click.echo(f"  Size:    {stats.get('size', 0)} items")
        click.echo(f"  Volume:  {stats.get('volume', 0)} bytes")
        click.echo(f"  Hits:    {stats.get('hits', 0)}")
        click.echo(f"  Misses:  {stats.get('misses', 0)}")
        click.echo(f"  Hit Rate: {stats.get('hit_rate', 0):.2%}")
    else:
        # For Redis cache, we can try to get some basic info
        try:
            # Try to get basic Redis info
            info = await cache.client.info() if hasattr(cache, "client") else {}
            click.echo(f"  Redis version: {info.get('redis_version', 'Unknown')}")
            click.echo(
                f"  Connected clients: {info.get('connected_clients', 'Unknown')}"
            )
        except Exception:
            click.echo("  Unable to retrieve detailed statistics for this backend")
