"""CLI commands for document ingestion."""

from __future__ import annotations

from pathlib import Path

import click

from grimoire.cli.helpers import (
    async_command,
    build_ingestion_agent,
    echo_error,
    echo_success,
    get_db_context,
    setup_db,
    teardown_db,
)


@click.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--recursive/--no-recursive", "-r", default=True, help="Recurse into subdirectories.")
@click.option("--strategy", type=click.Choice(["semantic", "markdown", "recursive"]), default=None, help="Chunking strategy.")
@click.option("--auto-tag/--no-auto-tag", default=True, help="Auto-tag documents with LLM.")
@click.pass_context
@async_command
async def ingest(ctx: click.Context, path: Path, recursive: bool, strategy: str | None, auto_tag: bool) -> None:
    """Ingest documents from PATH into the knowledge base.

    PATH can be a file or directory. Directories are processed recursively by default.

    Examples:

        grimoire ingest /path/to/documents

        grimoire ingest ./paper.pdf --no-auto-tag

        grimoire ingest /docs --strategy semantic
    """
    await setup_db()
    try:
        agent = build_ingestion_agent()

        async with get_db_context() as db:
            if path.is_file():
                click.echo(f"Ingesting file: {path}")
                result = await agent.ingest_file(db, path, auto_tag=auto_tag)
                if result.status == "completed":
                    echo_success(
                        f"Ingested {path.name}: {result.chunks_created} chunks, "
                        f"{result.tags_applied} tags ({result.duration_ms}ms)"
                    )
                elif result.status == "skipped":
                    click.echo(f"Skipped (duplicate): {path.name}")
                else:
                    echo_error(f"Failed: {result.error_message}")
            else:
                click.echo(f"Ingesting directory: {path} (recursive={recursive})")
                result = await agent.ingest_directory(
                    db, path, recursive=recursive, auto_tag=auto_tag,
                )
                echo_success(
                    f"Done: {result.succeeded}/{result.total} succeeded, "
                    f"{result.skipped} skipped, {result.failed} failed "
                    f"({result.duration_ms}ms)"
                )
                for r in result.results:
                    if r.status == "failed":
                        echo_error(f"  {r.file_path}: {r.error_message}")
    finally:
        await teardown_db()
