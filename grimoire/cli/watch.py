"""CLI commands for file watching."""

from __future__ import annotations

import asyncio
import signal

import click

from grimoire.cli.helpers import (
    async_command,
    build_ingestion_agent,
    echo_error,
    echo_success,
    setup_db,
    teardown_db,
)


@click.group()
def watch() -> None:
    """Watch directories for changes and auto-ingest new files."""


@watch.command("start")
@click.argument("path", type=str)
@click.option("--recursive/--no-recursive", default=True, help="Watch subdirectories.")
@click.option("--poll-interval", type=int, default=None, help="Poll interval in seconds (cloud backends).")
@click.option("--backend", type=click.Choice(["local", "gdrive", "onedrive"]), default="local", help="Storage backend.")
@click.pass_context
@async_command
async def watch_start(
    ctx: click.Context,
    path: str,
    recursive: bool,
    poll_interval: int | None,
    backend: str,
) -> None:
    """Start watching PATH for file changes.

    Runs until interrupted with Ctrl+C.

    Examples:

        grimoire watch start /home/user/docs

        grimoire watch start gdrive://Research --backend gdrive --poll-interval 300
    """
    await setup_db()
    try:
        from grimoire.db.session import get_db_context, get_db_manager
        from grimoire.storage.watch_manager import WatchManager
        from grimoire.agents.watcher import WatcherAgent

        agent_ingest = build_ingestion_agent()
        manager = WatchManager()
        db_manager = get_db_manager()

        watcher = WatcherAgent(
            watch_manager=manager,
            ingestion_agent=agent_ingest,
            db_session_factory=db_manager.session,
        )

        watch_kwargs: dict = {"backend": backend, "recursive": recursive}
        if poll_interval is not None:
            watch_kwargs["poll_interval"] = poll_interval

        watch_id = await watcher.watch(path, **watch_kwargs)
        echo_success(f"Watching {path} (id={watch_id}, backend={backend})")
        click.echo("Press Ctrl+C to stop.")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        await stop.wait()

        click.echo("\nStopping watcher...")
        await watcher.stop_all()
        echo_success("Watcher stopped.")
    finally:
        await teardown_db()


@watch.command("list")
@click.pass_context
@async_command
async def watch_list(ctx: click.Context) -> None:
    """List active watches.

    Note: watches only persist for the lifetime of a 'watch start' process.
    This command shows watches from the current process context.
    """
    click.echo("Active watches are only visible within a running 'watch start' process.")
    click.echo("Use 'grimoire status' for general system status.")


@watch.command("unwatch")
@click.argument("watch_id", type=str)
@click.pass_context
@async_command
async def watch_unwatch(ctx: click.Context, watch_id: str) -> None:
    """Stop watching by WATCH_ID.

    Note: watches are scoped to a running 'watch start' process.
    Use Ctrl+C in the watch process to stop it.
    """
    click.echo(f"Watch {watch_id}: use Ctrl+C in the running watch process to stop.")
