"""Migration CLI commands for Grimoire.

This module provides CLI commands for migrating data between different vector stores.

No target backend is implemented yet. The command is kept registered so the CLI
surface (and its ``--help``) stays stable, but it fails loudly instead of
pretending to migrate: the previous implementation logged a success message
without moving a single document, which is worse than an outright failure.
"""

import click
from loguru import logger

# Backends that ``--to`` accepts. Every entry here must have a working migration
# path in _run_migration; entries are only added alongside that implementation.
SUPPORTED_TARGETS: tuple[str, ...] = ("qdrant",)


@click.command()
@click.option(
    "--to",
    type=click.Choice(SUPPORTED_TARGETS),
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
    No target backend is currently available.
    """
    logger.error(
        "Migration to {target} requested but no migration backend is implemented",
        target=to,
    )
    raise click.ClickException(
        f"Migration to '{to}' is not implemented. Grimoire currently stores vectors "
        "in ChromaDB only; there is no grimoire.vectorstore.qdrant module and no "
        "qdrant-client dependency. Track this in the project issue tracker before "
        "relying on `grimoire migrate`."
    )
