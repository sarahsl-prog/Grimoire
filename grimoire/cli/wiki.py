"""CLI commands for wiki compilation and management."""

from __future__ import annotations

import click

from grimoire.cli.helpers import (
    async_command,
    build_wiki_agent,
    echo_error,
    echo_success,
    get_db_context,
    setup_db,
    teardown_db,
)


@click.group()
def wiki() -> None:
    """Manage the compiled wiki -- compile, view, and export wiki pages."""


@wiki.command("compile")
@click.option("--doc-id", "-d", type=str, default=None, help="Compile a specific document.")
@click.option("--category", type=str, default=None, help="Compile all docs in a category.")
@click.pass_context
@async_command
async def wiki_compile(
    ctx: click.Context, doc_id: str | None, category: str | None
) -> None:
    """Compile wiki-pending documents into wiki pages."""
    await setup_db()
    try:
        from sqlalchemy import select

        from grimoire.db.models import Document, WikiCompileJob

        agent = build_wiki_agent()

        async with get_db_context() as db:
            if doc_id:
                results = [await agent.compile_document(db, doc_id)]
            elif category:
                from grimoire.db.models import Category, DocumentTag

                stmt = (
                    select(Document.id)
                    .join(DocumentTag, DocumentTag.document_id == Document.id)
                    .join(Category, Category.id == DocumentTag.category_id)
                    .where(Category.slug == category)
                )
                result = await db.execute(stmt)
                doc_ids = [row[0] for row in result.all()]
                if not doc_ids:
                    echo_error(f"No documents found in category '{category}'")
                    return
                results = []
                for did in doc_ids:
                    r = await agent.compile_document(db, did)
                    results.append(r)
            else:
                results = await agent.compile_pending(db)

            for r in results:
                if r.error:
                    echo_error(f"Failed {r.document_id[:8]}: {r.error}")
                else:
                    echo_success(
                        f"Compiled {r.document_id[:8]}: "
                        f"{r.pages_created} created, {r.pages_updated} updated, "
                        f"{r.contradictions_found} contradictions"
                    )
    finally:
        await teardown_db()


@wiki.command("list")
@click.pass_context
@async_command
async def wiki_list(ctx: click.Context) -> None:
    """List all wiki pages."""
    await setup_db()
    try:
        from sqlalchemy import select

        from grimoire.db.models import WikiPage

        async with get_db_context() as db:
            stmt = select(WikiPage).order_by(WikiPage.title)
            result = await db.execute(stmt)
            pages = result.scalars().all()

        if not pages:
            click.echo("No wiki pages found.")
            return

        for page in pages:
            status_mark = {"compiled": "+", "draft": "~", "flagged": "!"}.get(
                page.status.value, "?"
            )
            click.echo(
                f"  [{status_mark}] {page.slug:<35} {page.title} "
                f"(v{page.version}, {page.entity_type or 'unknown'})"
            )
    finally:
        await teardown_db()


@wiki.command("show")
@click.argument("slug", type=str)
@click.pass_context
@async_command
async def wiki_show(ctx: click.Context, slug: str) -> None:
    """Display a wiki page by slug."""
    await setup_db()
    try:
        from sqlalchemy import select

        from grimoire.db.models import WikiPage

        async with get_db_context() as db:
            stmt = select(WikiPage).where(WikiPage.slug == slug)
            result = await db.execute(stmt)
            page = result.scalars().first()

        if not page:
            echo_error(f"Wiki page '{slug}' not found.")
            return

        click.echo(page.content)
    finally:
        await teardown_db()


@wiki.command("export")
@click.argument("slug", type=str, required=False)
@click.pass_context
@async_command
async def wiki_export(ctx: click.Context, slug: str | None) -> None:
    """Export wiki pages to markdown files."""
    await setup_db()
    try:
        from pathlib import Path

        from sqlalchemy import select

        from grimoire.config import get_settings
        from grimoire.db.models import WikiPage

        settings = get_settings()
        export_dir = Path(settings.wiki.wiki_pages_dir)
        export_dir.mkdir(parents=True, exist_ok=True)

        async with get_db_context() as db:
            if slug:
                stmt = select(WikiPage).where(WikiPage.slug == slug)
            else:
                stmt = select(WikiPage).order_by(WikiPage.title)
            result = await db.execute(stmt)
            pages = result.scalars().all()

        if not pages:
            echo_error("No wiki pages to export.")
            return

        for page in pages:
            file_path = export_dir / f"{page.slug}.md"
            file_path.write_text(page.content, encoding="utf-8")
            click.echo(f"  Exported: {file_path}")

        if not slug:
            index_path = export_dir / "_index.md"
            lines = ["# Wiki Index\n"]
            for page in pages:
                lines.append(
                    f"- [[{page.title}]] ({page.entity_type or 'unknown'}, v{page.version})"
                )
            index_path.write_text("\n".join(lines), encoding="utf-8")
            click.echo(f"  Exported: {index_path}")

        echo_success(f"Exported {len(pages)} page(s) to {export_dir}/")
    finally:
        await teardown_db()


@wiki.command("status")
@click.pass_context
@async_command
async def wiki_status(ctx: click.Context) -> None:
    """Show wiki compile queue status."""
    await setup_db()
    try:
        from sqlalchemy import func, select

        from grimoire.db.models import WikiCompileJob, WikiPage

        async with get_db_context() as db:
            job_counts = {}
            for status_val in ["pending", "compiling", "completed", "failed"]:
                stmt = select(func.count()).where(
                    WikiCompileJob.status == status_val
                )
                result = await db.execute(stmt)
                job_counts[status_val] = result.scalar() or 0

            page_count = await db.execute(select(func.count()).select_from(WikiPage))
            total_pages = page_count.scalar() or 0

        click.echo("Wiki Status:")
        click.echo(f"  Pages: {total_pages}")
        click.echo(f"  Compile queue:")
        click.echo(f"    Pending:   {job_counts['pending']}")
        click.echo(f"    Compiling: {job_counts['compiling']}")
        click.echo(f"    Completed: {job_counts['completed']}")
        click.echo(f"    Failed:    {job_counts['failed']}")
    finally:
        await teardown_db()