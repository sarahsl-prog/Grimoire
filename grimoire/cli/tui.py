"""``grimoire tui``: start the terminal UI.

A thin shortcut.  The UI itself lives in ``grimoire.tui`` and is also available
as the standalone ``grimoire-tui`` command, which is the canonical way to start
it.  Both this command and the ``--tui`` flag on the main group call the same
entry point, so they behave identically.
"""

from __future__ import annotations

import click


@click.command("tui")
@click.option(
    "--url",
    default=None,
    help="Grimoire API base URL. Overrides GRIMOIRE_API_URL.",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Write DEBUG-level records (and HTTP request lines) to the TUI log file.",
)
@click.pass_context
def tui(ctx: click.Context, url: str | None, debug: bool) -> None:
    """Start the terminal UI (needs `uv sync --extra tui`).

    A client for a running Grimoire API server: search, ask, and browse
    documents.  Reads GRIMOIRE_API_URL and GRIMOIRE_API_KEY from the
    environment.  There is deliberately no --api-key option, because
    command-line arguments leak into process listings and shell history.
    """
    # Imported here, not at module level: Textual is an optional extra, and the
    # rest of the CLI must start without it.
    from grimoire.tui.__main__ import main as tui_main

    argv: list[str] = []
    if url is not None:
        argv += ["--url", url]
    if debug:
        argv.append("--debug")
    ctx.exit(tui_main(argv))
