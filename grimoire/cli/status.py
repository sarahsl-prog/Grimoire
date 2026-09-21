"""CLI commands for system status and maintenance."""

from __future__ import annotations

from typing import Any

import click
from loguru import logger

from grimoire.cli.helpers import (
    async_command,
    echo_error,
    echo_success,
    echo_warning,
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

            # Ollama reachability -- a bad GRIMOIRE_OLLAMA_URL only otherwise
            # surfaces later, mid-query or mid-generation, as a 404/connect error.
            import httpx

            llm = settings.llm
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(f"{llm.url}/api/tags")
                    resp.raise_for_status()
                click.echo(f"\n  Ollama ({llm.model}): reachable at {llm.url}")
            except Exception as e:
                click.echo(
                    click.style(
                        f"\n  Ollama ({llm.model}): unreachable at {llm.url} ({e})",
                        fg="red",
                    )
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


@click.command()
@click.option(
    "--dry-run", is_flag=True, help="Report missing chunks without repairing them."
)
@click.option("--confirm/--no-confirm", default=True, help="Require confirmation.")
@click.option(
    "--batch-size",
    type=click.IntRange(min=1),
    default=500,
    show_default=True,
    help="Chunks to check/re-embed per batch.",
)
@click.pass_context
@async_command
async def reindex(
    ctx: click.Context, dry_run: bool, confirm: bool, batch_size: int
) -> None:
    """Re-embed chunks that are missing from the vector store.

    Compares every chunk in Postgres against the configured vector store and
    re-embeds any chunk id the vector store doesn't have, using the chunk
    text already stored in Postgres -- no re-parsing of source documents.
    Run this after `grimoire status --detailed` reports a chunk/embedding
    drift.

    Examples:

        grimoire reindex --dry-run

        grimoire reindex
    """
    settings = get_settings()
    if settings.vector_store.type != VectorStoreType.CHROMADB:
        echo_error("`grimoire reindex` only supports the chromadb backend today.")
        raise SystemExit(1)

    await setup_db()
    try:
        from sqlalchemy import select, update

        from grimoire.core.cache import CacheFactory
        from grimoire.core.embedder import Embedder, EmbeddingConfig
        from grimoire.db.models import Chunk as ChunkModel
        from grimoire.vectorstore.chromadb import ChromaDBStore

        vector_store = ChromaDBStore(
            persist_directory=settings.vector_store.chromadb.path,
            collection_name=settings.vector_store.chromadb.collection_name,
            host=settings.vector_store.host,
            port=settings.vector_store.port,
        )
        try:
            await vector_store.initialize(
                settings.vector_store.chromadb.collection_name, embedding_dim=1
            )
        except Exception as e:
            echo_error(f"Could not reach the vector store: {e}")
            raise SystemExit(1) from e

        async with get_db_context() as db:
            all_chunks = (
                (await db.execute(select(ChunkModel).order_by(ChunkModel.id)))
                .scalars()
                .all()
            )

        if not all_chunks:
            echo_success("No chunks in Postgres -- nothing to reindex.")
            return

        # A chunk id doubles as its vector store id (see
        # IngestionAgent._embed_and_store), so a batched `get()` tells us
        # exactly which chunk ids the vector store is missing -- this catches
        # both a failed embed write and the host/container vector-store split
        # footgun (vector_id set in Postgres, but against the wrong backend).
        missing: list[ChunkModel] = []
        for i in range(0, len(all_chunks), batch_size):
            batch = all_chunks[i : i + batch_size]
            found_ids = {r["id"] for r in await vector_store.get([c.id for c in batch])}
            missing.extend(c for c in batch if c.id not in found_ids)

        if not missing:
            echo_success(
                f"All {len(all_chunks)} chunks are present in the vector store."
            )
            return

        click.echo(
            f"{len(missing)} of {len(all_chunks)} chunks are missing from the "
            "vector store."
        )
        if dry_run:
            for c in missing[:20]:
                click.echo(
                    f"  {c.id}  doc={c.document_id}  chunk_index={c.chunk_index}"
                )
            if len(missing) > 20:
                click.echo(f"  ... and {len(missing) - 20} more")
            return

        if confirm and not click.confirm(
            f"Re-embed {len(missing)} chunk(s) and write them to the vector store?"
        ):
            return

        embed_config = EmbeddingConfig(
            model=settings.embeddings.model,
            fallback_model=settings.embeddings.fallback_model,
            device=settings.embeddings.device,
            batch_size=settings.embeddings.batch_size,
        )
        cache = CacheFactory.create(
            backend=settings.cache.storage, path=settings.cache.path
        )
        embedder = Embedder(config=embed_config, cache=cache)

        repaired = 0
        async with get_db_context() as db:
            for i in range(0, len(missing), batch_size):
                batch = missing[i : i + batch_size]
                texts = [c.content for c in batch]
                embeddings = await embedder.embed(texts)
                ids = [c.id for c in batch]
                metadatas = [
                    {
                        "document_id": c.document_id,
                        "chunk_index": c.chunk_index,
                        "token_count": c.token_count,
                    }
                    for c in batch
                ]

                await vector_store.add_documents(
                    ids=ids,
                    embeddings=embeddings,
                    metadatas=metadatas,
                    documents=texts,
                )
                await db.execute(
                    update(ChunkModel)
                    .where(ChunkModel.id.in_(ids))
                    .values(
                        vector_id=ChunkModel.id,
                        embedding_model=settings.embeddings.model,
                    )
                )
                repaired += len(batch)
                click.echo(f"  Re-embedded {repaired}/{len(missing)}")
            await db.commit()

        echo_warning(
            "Security metadata (e.g. CVE/Sigma fields) is not restored by "
            "reindex -- re-ingest affected documents if you rely on it for "
            "filtered search."
        )
        echo_success(f"Re-embedded {repaired} chunk(s).")
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
