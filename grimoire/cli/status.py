"""CLI commands for system status and maintenance."""

from __future__ import annotations

import click

from grimoire.cli.helpers import (
    async_command,
    echo_success,
    get_db_context,
    setup_db,
    teardown_db,
)
from grimoire.config.settings import get_settings
from grimoire.core.cache import CacheFactory, DiskCache


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
    await setup_db()
    try:
        from grimoire.db.models import Category, Document, ProcessingStatus
        from sqlalchemy import func, select

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

            # Cache stats
            try:
                settings = get_settings()
                cache = CacheFactory.create(
                    backend=settings.cache.storage, path=settings.cache.path
                )
                if isinstance(cache, DiskCache):
                    stats = cache.get_stats()
                    click.echo("\n  Cache:")
                    click.echo(f"    Size:     {stats.get('size', 0)} items")
                    click.echo(f"    Disk:     {stats.get('volume', 0)} bytes")
            except Exception:
                pass
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
