"""CLI commands for querying the knowledge base."""

from __future__ import annotations

import json
from typing import Any

import click

from grimoire.api.schemas import warn_unknown_filter_keys
from grimoire.cli.helpers import (
    async_command,
    build_query_agent,
    get_db_context,
    setup_db,
    teardown_db,
)


def _build_filter_dict(
    tag: tuple[str, ...],
    *,
    severity: str | None,
    tactic: str | None,
    technique: str | None,
    source_type: str | None,
    cve_id: str | None,
    content_date_after: str | None,
    platforms: tuple[str, ...],
) -> dict[str, Any] | None:
    """Compose the filter dict from CLI flags. Returns ``None`` if empty."""
    filters: dict[str, Any] = {}
    if tag:
        filters["tags"] = list(tag)
    if severity:
        filters["severity"] = severity
    if tactic:
        filters["mitre_tactic"] = tactic
    if technique:
        filters["mitre_technique_id"] = technique
    if source_type:
        filters["source_type"] = source_type
    if cve_id:
        filters["cve_id"] = cve_id
    if content_date_after:
        filters["content_date_after"] = content_date_after
    if platforms:
        filters["platforms"] = list(platforms)
    if not filters:
        return None
    warn_unknown_filter_keys(filters)
    return filters


_SECURITY_FILTER_OPTIONS = [
    click.option(
        "--severity",
        type=str,
        default=None,
        help="Filter by severity (critical, high, medium, low, info).",
    ),
    click.option("--tactic", type=str, default=None, help="Filter by MITRE tactic."),
    click.option(
        "--technique",
        type=str,
        default=None,
        help="Filter by MITRE technique ID (e.g. T1059).",
    ),
    click.option(
        "--source-type",
        type=str,
        default=None,
        help="Filter by source type (sigma_rule, nvd_cve, mitre_attack, prose).",
    ),
    click.option("--cve-id", type=str, default=None, help="Filter by CVE ID."),
    click.option(
        "--content-date-after",
        type=str,
        default=None,
        help="ISO-8601 date — only content on or after this date.",
    ),
    click.option(
        "--platform",
        "platforms",
        type=str,
        multiple=True,
        help="Filter by platform (repeatable: --platform windows --platform linux).",
    ),
]


def _apply_security_options(fn):
    """Stack the shared security filter options onto a Click command."""
    for opt in reversed(_SECURITY_FILTER_OPTIONS):
        fn = opt(fn)
    return fn


@click.command()
@click.argument("question", type=str)
@click.option(
    "--top-k", "-k", type=int, default=5, help="Number of source chunks to retrieve."
)
@click.option(
    "--tag", "-t", type=str, multiple=True, help="Filter by tag (repeatable)."
)
@click.option("--no-cache", is_flag=True, help="Bypass result cache.")
@_apply_security_options
@click.pass_context
@async_command
async def ask(
    ctx: click.Context,
    question: str,
    top_k: int,
    tag: tuple[str, ...],
    no_cache: bool,
    severity: str | None,
    tactic: str | None,
    technique: str | None,
    source_type: str | None,
    cve_id: str | None,
    content_date_after: str | None,
    platforms: tuple[str, ...],
) -> None:
    """Ask a question and get an AI-generated answer with citations.

    Examples:

        grimoire ask "What are the key findings on neural scaling laws?"

        grimoire ask "Summarize the Q3 results" --tag finance --top-k 10

        grimoire ask --severity high --tactic execution "powershell"
    """
    await setup_db()
    try:
        agent = build_query_agent()
        filter_dict = _build_filter_dict(
            tag,
            severity=severity,
            tactic=tactic,
            technique=technique,
            source_type=source_type,
            cve_id=cve_id,
            content_date_after=content_date_after,
            platforms=platforms,
        )

        async with get_db_context() as db:
            result = await agent.query(
                db,
                question,
                top_k=top_k,
                filter_dict=filter_dict,
                use_cache=not no_cache,
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
@click.option(
    "--tag", "-t", type=str, multiple=True, help="Filter by tag (repeatable)."
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json", "markdown"]),
    default="text",
    help="Output format.",
)
@_apply_security_options
@click.pass_context
@async_command
async def search(
    ctx: click.Context,
    query: str,
    top_k: int,
    tag: tuple[str, ...],
    fmt: str,
    severity: str | None,
    tactic: str | None,
    technique: str | None,
    source_type: str | None,
    cve_id: str | None,
    content_date_after: str | None,
    platforms: tuple[str, ...],
) -> None:
    """Search for documents without generating an answer.

    Examples:

        grimoire search "machine learning" --tag research

        grimoire search "quarterly report" --format json --top-k 20

        grimoire search "neural networks" --format markdown

        grimoire search --severity critical "lateral movement"
    """
    await setup_db()
    try:
        agent = build_query_agent()
        filter_dict = _build_filter_dict(
            tag,
            severity=severity,
            tactic=tactic,
            technique=technique,
            source_type=source_type,
            cve_id=cve_id,
            content_date_after=content_date_after,
            platforms=platforms,
        )

        async with get_db_context() as db:
            result = await agent.search(db, query, top_k=top_k, filter_dict=filter_dict)

        if fmt == "json":
            click.echo(json.dumps(result.model_dump(), indent=2, default=str))
            return

        if not result.results:
            click.echo("No results found.")
            return

        if fmt == "markdown":
            click.echo(
                f"Found {result.total_results} results ({result.duration_ms}ms):\n"
            )
            click.echo("| # | Score | Title | Snippet |")
            click.echo("|---|-------|-------|---------|")
            for i, r in enumerate(result.results, 1):
                title = r.get("document_title", r.get("document_id", "")[:8])
                score = r.get("score", 0)
                snippet = (
                    r.get("content", "")[:80].replace("\n", " ").replace("|", "\\|")
                )
                click.echo(f"| {i} | {score:.2f} | {title} | {snippet}... |")
            click.echo()
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
