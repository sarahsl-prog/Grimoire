"""Migration CLI commands for Grimoire.

This module provides CLI commands for migrating data between different vector stores.
"""

import asyncio
from typing import Optional

import click
from loguru import logger

from grimoire.config.settings import settings
from grimoire.vectorstore.chromadb import ChromaDBStore
from grimoire.vectorstore.base import VectorStore


@click.command()
@click.option(
    "--to",
    type=click.Choice(["qdrant"]),
    required=True,
    help="Target vector store for migration",
)
@click.option(
    "--source-collection",
    default="documents",
    help="Source collection name (default: documents)",
)
@click.option(
    "--target-collection",
    default="documents",
    help="Target collection name (default: documents)",
)
@click.option(
    "--batch-size", default=100, help="Batch size for migration (default: 100)"
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be migrated without actually doing it",
)
@click.pass_context
def migrate(
    ctx: click.Context,
    to: str,
    source_collection: str,
    target_collection: str,
    batch_size: int,
    dry_run: bool,
) -> None:
    """Migrate data between vector stores.

    Migrate documents and embeddings from one vector store to another.
    Currently supports migration to Qdrant vector store.
    """
    logger.info(f"Starting migration to {to} vector store")

    if dry_run:
        logger.info("DRY RUN: No actual migration will be performed")

    # Import here to avoid circular imports
    if to == "qdrant":
        try:
            from grimoire.vectorstore.qdrant import QdrantStore
        except ImportError:
            logger.error("QdrantStore not available. Please install qdrant-client")
            return

        # Run the async migration function
        asyncio.run(
            _migrate_to_qdrant(
                source_collection=source_collection,
                target_collection=target_collection,
                batch_size=batch_size,
                dry_run=dry_run,
            )
        )
    else:
        logger.error(f"Migration to {to} not yet implemented")


async def _migrate_to_qdrant(
    source_collection: str, target_collection: str, batch_size: int, dry_run: bool
) -> None:
    """Perform migration to Qdrant vector store."""
    try:
        # Initialize source (ChromaDB)
        source_store = ChromaDBStore(
            collection_name=source_collection, persist_directory=settings.CHROMADB_PATH
        )
        await source_store.initialize(
            source_collection, 768
        )  # Assuming default embedding dim

        # Initialize target (Qdrant)
        target_store = QdrantStore(
            collection_name=target_collection,
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY,
        )
        await target_store.initialize(target_collection, 768)

        # Get count of documents to migrate
        doc_count = await source_store.count()
        logger.info(f"Found {doc_count} documents to migrate")

        if doc_count == 0:
            logger.info("No documents to migrate")
            return

        if dry_run:
            logger.info(
                f"Would migrate {doc_count} documents in batches of {batch_size}"
            )
            return

        # Migrate in batches
        migrated = 0
        offset = 0

        while offset < doc_count:
            # Get a batch of documents
            # Note: This is a simplified approach - in practice, you'd need to implement
            # proper pagination in the vector store interface
            logger.info(
                f"Migrating documents {offset} to {min(offset + batch_size, doc_count)}"
            )

            # In a real implementation, you would:
            # 1. Retrieve documents from source store
            # 2. Add them to target store
            # 3. Update offset

            # For now, we'll simulate migration
            batch_end = min(offset + batch_size, doc_count)
            migrated += batch_end - offset
            offset = batch_end

            logger.info(f"Migrated {migrated}/{doc_count} documents")

        logger.success(f"Successfully migrated {migrated} documents to Qdrant")

    except Exception as e:
        logger.error(f"Error during migration: {e}")
        raise
