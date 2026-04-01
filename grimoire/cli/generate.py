"""CLI commands for content generation."""

from __future__ import annotations

import json

import click

from grimoire.cli.helpers import (
    async_command,
    build_content_gen_agent,
    echo_error,
    echo_success,
    get_db_context,
    setup_db,
    teardown_db,
)


@click.group()
def generate() -> None:
    """Generate content from ingested documents."""


def _parse_doc_ids(doc_id: tuple[str, ...], tag: str | None) -> list[str]:
    """Resolve document IDs from --doc-id flags or --tag filter."""
    if doc_id:
        return list(doc_id)
    return []


@generate.command()
@click.option("--doc-id", "-d", type=str, multiple=True, required=True, help="Document ID (repeatable).")
@click.option("--style", type=click.Choice(["concise", "detailed"]), default="concise", help="Summary style.")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", help="Output format.")
@click.pass_context
@async_command
async def summary(ctx: click.Context, doc_id: tuple[str, ...], style: str, fmt: str) -> None:
    """Generate a summary of specified documents.

    Examples:

        grimoire generate summary --doc-id abc123 --style detailed

        grimoire generate summary -d id1 -d id2 --format json
    """
    await setup_db()
    try:
        agent = build_content_gen_agent()
        ids = list(doc_id)

        async with get_db_context() as db:
            result = await agent.generate_summary(db, ids, style=style)

        _output_result(result, fmt)
    finally:
        await teardown_db()


@generate.command("flashcards")
@click.option("--doc-id", "-d", type=str, multiple=True, required=True, help="Document ID (repeatable).")
@click.option("--count", "-n", type=int, default=10, help="Number of flash cards.")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", help="Output format.")
@click.pass_context
@async_command
async def flashcards(ctx: click.Context, doc_id: tuple[str, ...], count: int, fmt: str) -> None:
    """Generate flash cards from documents.

    Examples:

        grimoire generate flashcards --doc-id abc123 --count 20
    """
    await setup_db()
    try:
        agent = build_content_gen_agent()
        ids = list(doc_id)

        async with get_db_context() as db:
            result = await agent.generate_flash_cards(db, ids, count=count)

        _output_result(result, fmt)
    finally:
        await teardown_db()


@generate.command("cliff-notes")
@click.option("--doc-id", "-d", type=str, multiple=True, required=True, help="Document ID (repeatable).")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", help="Output format.")
@click.pass_context
@async_command
async def cliff_notes(ctx: click.Context, doc_id: tuple[str, ...], fmt: str) -> None:
    """Generate cliff notes from documents.

    Examples:

        grimoire generate cliff-notes --doc-id abc123
    """
    await setup_db()
    try:
        agent = build_content_gen_agent()
        ids = list(doc_id)

        async with get_db_context() as db:
            result = await agent.generate_cliff_notes(db, ids)

        _output_result(result, fmt)
    finally:
        await teardown_db()


@generate.command()
@click.option("--doc-id", "-d", type=str, multiple=True, required=True, help="Document ID (repeatable).")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", help="Output format.")
@click.pass_context
@async_command
async def outline(ctx: click.Context, doc_id: tuple[str, ...], fmt: str) -> None:
    """Generate an outline from documents.

    Examples:

        grimoire generate outline --doc-id abc123
    """
    await setup_db()
    try:
        agent = build_content_gen_agent()
        ids = list(doc_id)

        async with get_db_context() as db:
            result = await agent.generate_outline(db, ids)

        _output_result(result, fmt)
    finally:
        await teardown_db()


def _output_result(result: object, fmt: str) -> None:
    """Print generation result in requested format."""
    if fmt == "json":
        click.echo(json.dumps(result.model_dump(), indent=2, default=str))  # type: ignore[union-attr]
        return

    content = getattr(result, "content", "")
    if not content:
        echo_error("No content generated.")
        return

    click.echo(f"\n{content}\n")

    cached = getattr(result, "cached", False)
    duration = getattr(result, "duration_ms", 0)
    model = getattr(result, "model_used", "")
    meta_parts = []
    if model:
        meta_parts.append(f"model={model}")
    if duration:
        meta_parts.append(f"{duration}ms")
    if cached:
        meta_parts.append("cached")
    if meta_parts:
        click.echo(click.style(f"({', '.join(meta_parts)})", dim=True))
