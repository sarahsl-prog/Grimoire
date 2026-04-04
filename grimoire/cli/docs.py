"""CLI commands for listing and browsing ingested documents."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import click
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from grimoire.cli.helpers import (
    async_command,
    echo_error,
    get_db_context,
    setup_db,
    teardown_db,
)


def _parse_since(value: str) -> datetime:
    """Parse a --since value into a datetime.

    Supports relative durations (7d, 2w, 3m) and ISO dates.
    """
    match = re.match(r"^(\d+)([dwm])$", value)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit == "d":
            delta = timedelta(days=amount)
        elif unit == "w":
            delta = timedelta(weeks=amount)
        else:  # "m"
            delta = timedelta(days=amount * 30)
        return datetime.utcnow() - delta

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise click.BadParameter(
            f"Invalid date '{value}'. Use ISO format (2026-03-01) or relative (7d, 2w, 3m)."
        )


@click.group()
def docs() -> None:
    """Browse and list ingested documents."""


@docs.command("list")
@click.option("--category", "-c", type=str, default=None, help="Filter by category name or slug.")
@click.option("--search", "-s", type=str, default=None, help="Case-insensitive title substring search.")
@click.option("--since", type=str, default=None, help="Date filter (ISO date or relative: 7d, 2w, 3m).")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json", "markdown"]),
    default="text",
    help="Output format.",
)
@click.pass_context
@async_command
async def docs_list(
    ctx: click.Context,
    category: str | None,
    search: str | None,
    since: str | None,
    fmt: str,
) -> None:
    """List ingested documents with optional filters.

    Examples:

        grimoire docs list

        grimoire docs list --category "machine-learning"

        grimoire docs list --search "quantization" --since 7d

        grimoire docs list --format json
    """
    await setup_db()
    try:
        from grimoire.db.models import Category, Document, DocumentTag

        stmt = select(Document)

        if category:
            stmt = (
                stmt.join(DocumentTag, Document.id == DocumentTag.document_id)
                .join(Category, DocumentTag.category_id == Category.id)
                .where(
                    (func.lower(Category.name) == category.lower())
                    | (Category.slug == category.lower())
                )
            )

        if search:
            stmt = stmt.where(Document.title.ilike(f"%{search}%"))

        if since:
            parsed_date = _parse_since(since)
            stmt = stmt.where(Document.created_at >= parsed_date)

        stmt = stmt.order_by(Document.created_at.desc())

        async with get_db_context() as db:
            result = await db.execute(stmt)
            documents = result.scalars().all()

            # Fetch categories for each document (for JSON output)
            doc_categories: dict[str, list[str]] = {}
            if fmt == "json":
                for doc in documents:
                    cat_stmt = (
                        select(Category.name)
                        .join(DocumentTag, DocumentTag.category_id == Category.id)
                        .where(DocumentTag.document_id == doc.id)
                    )
                    cat_result = await db.execute(cat_stmt)
                    doc_categories[doc.id] = list(cat_result.scalars().all())

        if fmt == "json":
            _output_json(documents, doc_categories)
        elif fmt == "markdown":
            _output_markdown(documents)
        else:
            _output_text(documents)
    finally:
        await teardown_db()


def _output_text(documents: list) -> None:
    """Print documents in formatted text table."""
    if not documents:
        click.echo("No documents found.")
        return

    click.echo(
        f"{'ID':<10}{'Title':<34}{'Type':<6}{'Status':<12}{'Created'}"
    )
    click.echo(
        f"{'─' * 8:<10}{'─' * 32:<34}{'─' * 4:<6}{'─' * 9:<12}{'─' * 10}"
    )
    for doc in documents:
        title = (doc.title or "Untitled")[:32]
        file_type = doc.file_type.value if hasattr(doc.file_type, "value") else str(doc.file_type)
        status = doc.processing_status.value if hasattr(doc.processing_status, "value") else str(doc.processing_status)
        created = doc.created_at.strftime("%Y-%m-%d") if doc.created_at else ""
        click.echo(f"{doc.id[:8]:<10}{title:<34}{file_type:<6}{status:<12}{created}")

    click.echo(f"\n{len(documents)} document(s) found.")


def _output_markdown(documents: list) -> None:
    """Print documents as a GitHub-flavored markdown table."""
    if not documents:
        click.echo("No documents found.")
        return

    click.echo("| ID       | Title                            | Type | Status    | Created    |")
    click.echo("|----------|----------------------------------|------|-----------|------------|")
    for doc in documents:
        title = (doc.title or "Untitled")[:32]
        file_type = doc.file_type.value if hasattr(doc.file_type, "value") else str(doc.file_type)
        status = doc.processing_status.value if hasattr(doc.processing_status, "value") else str(doc.processing_status)
        created = doc.created_at.strftime("%Y-%m-%d") if doc.created_at else ""
        click.echo(f"| {doc.id[:8]:<8} | {title:<32} | {file_type:<4} | {status:<9} | {created:<10} |")

    click.echo(f"\n{len(documents)} document(s) found.")


def _output_json(documents: list, doc_categories: dict[str, list[str]]) -> None:
    """Print documents as JSON."""
    if not documents:
        click.echo("[]")
        return

    items = []
    for doc in documents:
        file_type = doc.file_type.value if hasattr(doc.file_type, "value") else str(doc.file_type)
        status = doc.processing_status.value if hasattr(doc.processing_status, "value") else str(doc.processing_status)
        items.append({
            "id": doc.id,
            "title": doc.title,
            "file_type": file_type,
            "processing_status": status,
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
            "categories": doc_categories.get(doc.id, []),
        })

    click.echo(json.dumps(items, indent=2, default=str))
