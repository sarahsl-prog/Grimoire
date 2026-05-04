"""Main CLI entry point for Grimoire.

This module provides the main Click CLI entry point for the Grimoire
knowledge management system.
"""

from pathlib import Path

import click
from loguru import logger

# Import version from package
from grimoire import __version__


@click.group()
@click.version_option(version=__version__, prog_name="grimoire")
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

    # Configure logging — always includes file sink via setup_logger()
    from grimoire.utils.logger import CLI_LOG_FORMAT, setup_logger
    log_level = "DEBUG" if verbose else "INFO"
    setup_logger(level=log_level, console_format=CLI_LOG_FORMAT)


# Register subcommands
from grimoire.cli.categories import categories, tag, untag
from grimoire.cli.docs import docs
from grimoire.cli.config import config as config_cmd
from grimoire.cli.generate import generate
from grimoire.cli.ingest import ingest
from grimoire.cli.keys import keys
from grimoire.cli.migrate import migrate
from grimoire.cli.query import ask, search
from grimoire.cli.status import cache_group, status
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
cli.add_command(cache_group)
cli.add_command(docs)
cli.add_command(wiki)
cli.add_command(keys)
cli.add_command(migrate)


def main() -> None:
    """Entry point for the Grimoire CLI."""
    cli()


if __name__ == "__main__":
    main()
