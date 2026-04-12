"""CLI commands for configuration management."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import click
import yaml

from grimoire.cli.helpers import echo_error, echo_success
from grimoire.config.settings import get_settings


@click.group("config")
def config() -> None:
    """Manage Grimoire configuration."""


@config.command("init")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=Path("grimoire.yaml"), help="Output config file path.")
@click.pass_context
def config_init(ctx: click.Context, output: Path) -> None:
    """Create a default configuration file.

    Examples:

        grimoire config init

        grimoire config init -o /etc/grimoire/config.yaml
    """
    if output.exists():
        if not click.confirm(f"{output} already exists. Overwrite?"):
            return

    default_config = {
        "llm": {
            "model": "llama3.2",
            "url": "http://localhost:11434",
            "temperature": 0.7,
            "max_tokens": 4096,
            "timeout": 30,
        },
        "embeddings": {
            "model": "sentence-transformers/all-mpnet-base-v2",
            "device": "auto",
            "batch_size": 32,
        },
        "database": {
            "url": "postgresql+asyncpg://grimoire:grimoire@localhost:5432/grimoire",
        },
        "vector_store": {
            "type": "chromadb",
            "chromadb": {
                "path": ".data/chromadb",
                "collection": "documents",
            },
        },
        "cache": {
            "storage": "disk",
            "path": ".cache",
        },
        "chunking": {
            "default_strategy": "recursive",
            "chunk_size": 1000,
            "chunk_overlap": 200,
        },
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w") as f:
        yaml.dump(default_config, f, default_flow_style=False, sort_keys=False)

    echo_success(f"Configuration written to {output}")


@config.command("show")
@click.option("--section", "-s", type=str, default=None, help="Show only a specific section.")
@click.pass_context
def config_show(ctx: click.Context, section: str | None) -> None:
    """Display current configuration.

    Examples:

        grimoire config show

        grimoire config show --section llm
    """
    try:
        settings = get_settings()
    except Exception as e:
        echo_error(f"Failed to load settings: {e}")
        return

    data = settings.model_dump()

    if section:
        if section in data:
            data = {section: data[section]}
        else:
            echo_error(f"Unknown section '{section}'. Available: {', '.join(data.keys())}")
            return

    click.echo(yaml.dump(data, default_flow_style=False, sort_keys=False))


@config.command("edit")
@click.option("--file", "-f", type=click.Path(path_type=Path), default=Path("grimoire.yaml"), help="Config file to edit.")
@click.pass_context
def config_edit(ctx: click.Context, file: Path) -> None:
    """Open configuration file in your editor.

    Uses $EDITOR, falls back to vi.

    Examples:

        grimoire config edit

        grimoire config edit --file /etc/grimoire/config.yaml
    """
    if not file.exists():
        echo_error(f"{file} not found. Run 'grimoire config init' first.")
        return

    editor = os.environ.get("EDITOR", "vi")
    try:
        subprocess.run([editor, str(file)], check=True)
    except FileNotFoundError:
        echo_error(f"Editor '{editor}' not found. Set $EDITOR.")
    except subprocess.CalledProcessError:
        echo_error("Editor exited with error.")
