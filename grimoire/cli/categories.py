"""CLI commands for category and tag management."""

from __future__ import annotations

from uuid import uuid4

import click

from grimoire.cli.helpers import (
    async_command,
    echo_error,
    echo_success,
    get_db_context,
    setup_db,
    teardown_db,
)


@click.group("category")
def categories() -> None:
    """Manage categories and tags."""


@categories.command("add")
@click.argument("name", type=str)
@click.option("--description", "-d", type=str, default=None, help="Category description.")
@click.option("--parent", type=str, default=None, help="Parent category slug.")
@click.option("--color", type=str, default=None, help="Display color (hex).")
@click.pass_context
@async_command
async def category_add(
    ctx: click.Context,
    name: str,
    description: str | None,
    parent: str | None,
    color: str | None,
) -> None:
    """Add a new category.

    Examples:

        grimoire category add "Research" --description "Research papers"

        grimoire category add "AI" --parent research --color "#2ecc71"
    """
    await setup_db()
    try:
        from slugify import slugify
    except ImportError:
        # Fallback slugify
        def slugify(text: str) -> str:  # type: ignore[misc]
            return text.lower().replace(" ", "-")

    try:
        from grimoire.db.models import Category

        from sqlalchemy import select

        async with get_db_context() as db:
            parent_id = None
            if parent:
                stmt = select(Category).where(Category.slug == parent)
                result = await db.execute(stmt)
                parent_cat = result.scalars().first()
                if not parent_cat:
                    echo_error(f"Parent category '{parent}' not found.")
                    return
                parent_id = parent_cat.id

            cat = Category(
                id=str(uuid4()),
                name=name,
                slug=slugify(name),
                description=description or "",
                parent_id=parent_id,
                color=color or "#3498db",
            )
            db.add(cat)
            await db.commit()
            echo_success(f"Category '{name}' created (id={cat.id[:8]}...)")
    finally:
        await teardown_db()


@categories.command("list")
@click.option("--tree", is_flag=True, help="Show as tree hierarchy.")
@click.pass_context
@async_command
async def category_list(ctx: click.Context, tree: bool) -> None:
    """List all categories.

    Examples:

        grimoire category list

        grimoire category list --tree
    """
    await setup_db()
    try:
        from grimoire.db.models import Category

        from sqlalchemy import select

        async with get_db_context() as db:
            stmt = select(Category).order_by(Category.name)
            result = await db.execute(stmt)
            cats = result.scalars().all()

        if not cats:
            click.echo("No categories found.")
            return

        if tree:
            _print_tree(cats)
        else:
            for cat in cats:
                parent_info = f" (parent: {cat.parent_id[:8]})" if cat.parent_id else ""
                click.echo(f"  {cat.slug:<25} {cat.name}{parent_info}")
    finally:
        await teardown_db()


@categories.command("remove")
@click.argument("slug", type=str)
@click.option("--force", is_flag=True, help="Remove even if documents are tagged.")
@click.pass_context
@async_command
async def category_remove(ctx: click.Context, slug: str, force: bool) -> None:
    """Remove a category by slug.

    Examples:

        grimoire category remove old-category --force
    """
    await setup_db()
    try:
        from grimoire.db.models import Category

        from sqlalchemy import select

        async with get_db_context() as db:
            stmt = select(Category).where(Category.slug == slug)
            result = await db.execute(stmt)
            cat = result.scalars().first()

            if not cat:
                echo_error(f"Category '{slug}' not found.")
                return

            await db.delete(cat)
            await db.commit()
            echo_success(f"Category '{cat.name}' removed.")
    finally:
        await teardown_db()


@click.command()
@click.argument("doc_id", type=str)
@click.argument("tags", nargs=-1, required=True)
@click.pass_context
@async_command
async def tag(ctx: click.Context, doc_id: str, tags: tuple[str, ...]) -> None:
    """Tag a document with one or more categories.

    Examples:

        grimoire tag abc123 "AI/ML" "Important"
    """
    await setup_db()
    try:
        from grimoire.db.models import Category, Document, DocumentTag, TaggedBy

        from sqlalchemy import select

        async with get_db_context() as db:
            doc = await db.get(Document, doc_id)
            if not doc:
                echo_error(f"Document '{doc_id}' not found.")
                return

            for tag_name in tags:
                stmt = select(Category).where(Category.name == tag_name)
                result = await db.execute(stmt)
                cat = result.scalars().first()
                if not cat:
                    echo_error(f"Category '{tag_name}' not found. Skipping.")
                    continue

                dt = DocumentTag(
                    id=str(uuid4()),
                    document_id=doc_id,
                    category_id=cat.id,
                    confidence=1.0,
                    tagged_by=TaggedBy.USER,
                )
                db.add(dt)

            await db.commit()
            echo_success(f"Tagged document {doc_id[:8]} with: {', '.join(tags)}")
    finally:
        await teardown_db()


@click.command()
@click.argument("doc_id", type=str)
@click.argument("tags", nargs=-1, required=True)
@click.pass_context
@async_command
async def untag(ctx: click.Context, doc_id: str, tags: tuple[str, ...]) -> None:
    """Remove tags from a document.

    Examples:

        grimoire untag abc123 "OldTag"
    """
    await setup_db()
    try:
        from grimoire.db.models import Category, DocumentTag

        from sqlalchemy import select

        async with get_db_context() as db:
            for tag_name in tags:
                stmt = select(Category).where(Category.name == tag_name)
                result = await db.execute(stmt)
                cat = result.scalars().first()
                if not cat:
                    echo_error(f"Category '{tag_name}' not found.")
                    continue

                stmt_dt = select(DocumentTag).where(
                    DocumentTag.document_id == doc_id,
                    DocumentTag.category_id == cat.id,
                )
                result_dt = await db.execute(stmt_dt)
                dt = result_dt.scalars().first()
                if dt:
                    await db.delete(dt)

            await db.commit()
            echo_success(f"Removed tags from document {doc_id[:8]}: {', '.join(tags)}")
    finally:
        await teardown_db()


def _print_tree(cats: list, indent: int = 0, parent_id: str | None = None) -> None:
    """Print categories as an indented tree."""
    for cat in cats:
        if cat.parent_id == parent_id:
            prefix = "  " * indent + ("|- " if indent > 0 else "")
            click.echo(f"{prefix}{cat.name} ({cat.slug})")
            _print_tree(cats, indent + 1, cat.id)
