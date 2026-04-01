"""CLI commands for querying the knowledge base."""

from __future__ import annotations

import json

import click

from grimoire.cli.helpers import (
    async_command,
    build_query_agent,
    echo_error,
    get_db_context,
    setup_db,
    teardown_db,
)


@click.command()
@click.argument("question", type=str)
@click.option("--top-k", "-k", type=int, default=5, help="Number of source chunks to retrieve.")
@click.option("--tag", "-t", type=str, multiple=True, help="Filter by tag (repeatable).")
@click.option("--no-cache", is_flag=True, help="Bypass result cache.")
@click.pass_context
@async_command
async def ask(ctx: click.Context, question: str, top_k: int, tag: tuple[str, ...], no_cache: bool) -> None:
    """Ask a question and get an AI-generated answer with citations.

    Examples:

        grimoire ask "What are the key findings on neural scaling laws?"

        grimoire ask "Summarize the Q3 results" --tag finance --top-k 10
    """
    await setup_db()
    try:
        agent = build_query_agent()
        filter_dict = {"tags": list(tag)} if tag else None

        async with get_db_context() as db:
            result = await agent.query(
                db, question, top_k=top_k, filter_dict=filter_dict, use_cache=not no_cache,
            )

        if not result.answer:
            click.echo("No relevant information found.")
            return

        click.echo(f"\n{result.answer}\n")

        if result.citations:
            click.echo(click.style("Sources:", bold=True))
            for i, cite in enumerate(result.citations, 1):
                title = cite.document_title or cite.document_id[:8]
                click.echo(f"  [{i}] {title} (score: {cite.relevance_score:.2f})")

        if result.cached:
            click.echo(click.style("\n(cached result)", dim=True))
    finally:
        await teardown_db()


@click.command()
@click.argument("query", type=str)
@click.option("--top-k", "-k", type=int, default=10, help="Number of results.")
@click.option("--tag", "-t", type=str, multiple=True, help="Filter by tag (repeatable).")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", help="Output format.")
@click.pass_context
@async_command
async def search(ctx: click.Context, query: str, top_k: int, tag: tuple[str, ...], fmt: str) -> None:
    """Search for documents without generating an answer.

    Examples:

        grimoire search "machine learning" --tag research

        grimoire search "quarterly report" --format json --top-k 20
    """
    await setup_db()
    try:
        agent = build_query_agent()
        filter_dict = {"tags": list(tag)} if tag else None

        async with get_db_context() as db:
            result = await agent.search(db, query, top_k=top_k, filter_dict=filter_dict)

        if fmt == "json":
            click.echo(json.dumps(result.model_dump(), indent=2, default=str))
            return

        if not result.results:
            click.echo("No results found.")
            return

        click.echo(f"Found {result.total_results} results ({result.duration_ms}ms):\n")
        for i, r in enumerate(result.results, 1):
            title = r.get("document_title", r.get("document_id", "")[:8])
            score = r.get("score", 0)
            snippet = r.get("content", "")[:120].replace("\n", " ")
            click.echo(f"  {i}. [{score:.2f}] {title}")
            click.echo(f"     {snippet}...")
            click.echo()
    finally:
        await teardown_db()
