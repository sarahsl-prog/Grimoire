"""The TUI must stay a thin client: importing it may not load the pipeline.

These run in a subprocess.  Inside the pytest process the modules are already
imported (and so is everything the other tests pulled in), so ``sys.modules``
there says nothing about what a fresh ``grimoire-tui`` would load.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

# Heavy or server-side: loading any of these in the TUI process means the thin
# client is no longer thin (slow startup, GPU libraries, database drivers), or
# that a log sink was installed that would paint over the screen.
FORBIDDEN = (
    "PySide6",
    "torch",
    "chromadb",
    "docling",
    "sqlalchemy",
    "sentence_transformers",
    "grimoire.agents",
    "grimoire.core",
    "grimoire.db",
    "grimoire.cli",
    "grimoire.gui",
    "grimoire.config.settings",
    "grimoire.utils.logger",
)


def _loaded_after(statement: str) -> list[str]:
    """Run ``statement`` in a fresh interpreter; return which FORBIDDEN loaded."""
    code = (
        f"import sys, json\n{statement}\n"
        f"print(json.dumps([m for m in {FORBIDDEN!r} if m in sys.modules]))"
    )
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    loaded: list[str] = json.loads(proc.stdout.strip().splitlines()[-1])
    return loaded


def test_the_entry_point_alone_loads_nothing_heavy_and_not_even_textual() -> None:
    """So `grimoire-tui --version` and the missing-extra hint work without it."""
    statement = (
        "import grimoire.tui.__main__\n"
        "assert 'textual' not in sys.modules, 'textual loaded by the entry point'"
    )

    assert _loaded_after(statement) == []


def test_the_whole_tui_package_loads_none_of_the_pipeline() -> None:
    pytest.importorskip("textual", reason="TUI extra not installed")
    statement = (
        "import grimoire.tui.__main__, grimoire.tui.app, grimoire.tui.errors\n"
        "import grimoire.tui.formatting, grimoire.tui.logsetup, grimoire.tui.messages\n"
        "import grimoire.tui.screens.document_detail\n"
        "import grimoire.tui.widgets.documents_pane, grimoire.tui.widgets.search_pane\n"
        "import grimoire.tui.widgets.status_bar"
    )

    assert _loaded_after(statement) == []


def test_the_api_client_the_tui_reuses_is_free_of_qt_and_the_pipeline() -> None:
    statement = (
        "import grimoire.client.client, grimoire.client.config, grimoire.client.errors"
    )

    assert _loaded_after(statement) == []


def test_importing_the_cli_does_not_load_the_tui_or_textual() -> None:
    """`--tui` imports the TUI lazily; ordinary CLI startup must not pay for it."""
    pytest.importorskip("click")
    code = (
        "import sys\n"
        "import grimoire.cli.main\n"
        "bad = [m for m in ('grimoire.tui', 'grimoire.tui.__main__', 'textual') "
        "if m in sys.modules]\n"
        "print(','.join(bad))"
    )
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        pytest.skip(
            f"the CLI cannot be imported in this environment: {proc.stderr[-200:]}"
        )

    assert proc.stdout.strip() == ""
