"""Main CLI entry point for Grimoire.

This module provides the main Click CLI entry point for the Grimoire
knowledge management system.
"""

from pathlib import Path

import click

# Import version from package
from grimoire import __version__


def _launch_tui(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """Callback for ``--tui``: run the terminal UI and exit with its code.

    The option is *eager*, so Click runs this before the group body.  That is
    what lets the UI start without the body's settings validation and stderr
    log sink: it is an HTTP client that needs neither, and a log line on stderr
    would paint over a full-screen UI.
    """
    # Shell completion parses the command line with `resilient_parsing` set; a
    # callback that launched a full-screen UI there would hang the shell.
    if not value or ctx.resilient_parsing:
        return
    # Lazy: Textual is an optional extra and must not be imported at CLI start.
    from grimoire.tui.__main__ import main as tui_main

    ctx.exit(tui_main([]))


@click.group()
@click.version_option(version=__version__, prog_name="grimoire")
@click.option(
    "--tui",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_launch_tui,
    help="Start the terminal UI and exit. Same as `grimoire-tui`.",
)
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to configuration file (YAML or .env)",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose (DEBUG) logging",
)
@click.pass_context
def cli(ctx: click.Context, config: Path | None, verbose: bool) -> None:
    """Grimoire - Agent-based Knowledge Management System.

    A production-ready tool for managing large document collections with
    AI-powered search, auto-tagging, and content generation.
    """
    # Initialize context dict
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    ctx.obj["verbose"] = verbose

    # `grimoire tui` behaves like `grimoire --tui`: the UI is an HTTP client
    # that configures its own file-only logging, so it needs neither the
    # settings validation below nor the stderr log sink that setup_logger()
    # installs (which would paint over the screen).  A broken grimoire.yaml
    # must not be able to stop it from starting.
    if ctx.invoked_subcommand == "tui":
        return

    # Configure logging — always includes file sink via setup_logger()
    from grimoire.utils.logger import CLI_LOG_FORMAT, setup_logger

    log_level = "DEBUG" if verbose else "INFO"
    setup_logger(level=log_level, console_format=CLI_LOG_FORMAT)

    # Validate configuration up front. Subcommands load settings lazily, deep in
    # the call stack, so without this a broken grimoire.yaml surfaces as a raw
    # traceback from wherever the first get_settings() call happens to be.
    # The `config` group is exempt so `config init`/`edit`/`show` stay usable
    # for repairing the very file that is broken.
    if ctx.invoked_subcommand != "config":
        from grimoire.cli.helpers import echo_error
        from grimoire.config.settings import ConfigurationError, get_settings

        try:
            get_settings()
        except ConfigurationError as e:
            echo_error(str(e))
            click.echo(
                "Run 'grimoire config show' to inspect the loaded configuration.",
                err=True,
            )
            raise SystemExit(1) from e


# Register subcommands
from grimoire.cli.categories import categories, tag, untag
from grimoire.cli.config import config as config_cmd
from grimoire.cli.docs import docs
from grimoire.cli.generate import generate
from grimoire.cli.ingest import ingest
from grimoire.cli.keys import keys
from grimoire.cli.mcp import mcp
from grimoire.cli.migrate import migrate
from grimoire.cli.query import ask, search
from grimoire.cli.status import cache_group, reindex, status
from grimoire.cli.tui import tui  # noqa: E402 - joins the import block above
from grimoire.cli.watch import watch
from grimoire.cli.wiki import wiki

cli.add_command(ingest)
cli.add_command(watch)
cli.add_command(ask)
cli.add_command(search)
cli.add_command(generate)
cli.add_command(categories)
cli.add_command(tag)
cli.add_command(untag)
cli.add_command(config_cmd)
cli.add_command(status)
cli.add_command(reindex)
cli.add_command(cache_group)
cli.add_command(docs)
cli.add_command(wiki)
cli.add_command(keys)
cli.add_command(migrate)
cli.add_command(mcp)
cli.add_command(tui)


def main() -> None:
    """Entry point for the Grimoire CLI."""
    cli()


if __name__ == "__main__":
    main()
