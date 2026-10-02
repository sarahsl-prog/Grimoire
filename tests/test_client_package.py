"""The shared client package and the GUI's compatibility shims (B6)."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

FORBIDDEN = (
    "PySide6",
    "textual",
    "torch",
    "chromadb",
    "docling",
    "sqlalchemy",
    "sentence_transformers",
    "grimoire.agents",
    "grimoire.core",
    "grimoire.db",
    "grimoire.cli",
    "grimoire.config.settings",
    "grimoire.utils.logger",
    "grimoire.gui",
    "grimoire.tui",
)


def test_client_package_loads_no_ui_or_pipeline() -> None:
    """A fresh interpreter importing grimoire.client must not pull any of these."""
    code = (
        "import sys, json\n"
        "import grimoire.client, grimoire.client.client, grimoire.client.config, "
        "grimoire.client.errors\n"
        f"print(json.dumps([m for m in {FORBIDDEN!r} if m in sys.modules]))"
    )
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == []


def test_neutral_names_are_the_same_objects() -> None:
    import grimoire.client as client
    import grimoire.client.config as config
    import grimoire.client.errors as errors

    assert client.ClientConfig is client.GuiConfig is config.ClientConfig
    assert client.ClientError is client.GuiError is errors.ClientError


@pytest.mark.parametrize(
    ("old", "new", "names"),
    [
        ("grimoire.gui.client", "grimoire.client.client", ["GrimoireClient"]),
        (
            "grimoire.gui.config",
            "grimoire.client.config",
            [
                "DEFAULT_BASE_URL",
                "ENV_API_KEY",
                "ENV_BASE_URL",
                "SUPPORTED_EXTENSIONS",
                "GuiConfig",
            ],
        ),
        (
            "grimoire.gui.errors",
            "grimoire.client.errors",
            [
                "AuthFailed",
                "ConnectionFailed",
                "GuiError",
                "MalformedResponse",
                "RateLimited",
                "RequestRejected",
                "ServerError",
                "TimedOut",
            ],
        ),
    ],
)
def test_gui_shims_re_export_the_same_objects(
    old: str, new: str, names: list[str]
) -> None:
    """Identity, not equality: ``except GuiError`` must catch either import path."""
    import importlib

    old_mod, new_mod = importlib.import_module(old), importlib.import_module(new)
    for name in names:
        assert getattr(old_mod, name) is getattr(new_mod, name), name
    assert sorted(old_mod.__all__) == sorted(names)
