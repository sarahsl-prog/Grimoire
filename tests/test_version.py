"""One version, defined once and read everywhere."""

from __future__ import annotations

import tomllib
from pathlib import Path

from grimoire import __version__
from grimoire.api.main import create_app
from grimoire.mcp.app import create_mcp_app

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_package_version_matches_pyproject() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert __version__ == data["project"]["version"]


def test_api_app_reports_the_package_version() -> None:
    assert create_app(use_lifespan=False).version == __version__


def test_mcp_app_reports_the_package_version() -> None:
    assert create_mcp_app(use_lifespan=False).version == __version__
