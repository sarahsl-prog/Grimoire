"""The ``all`` extra must stay in step with the other extras.

``uv sync --extra all`` is only a convenience if it really installs everything;
a new extra that nobody adds to ``all`` would silently be left out.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _extras() -> dict[str, list[str]]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    extras: dict[str, list[str]] = data["project"]["optional-dependencies"]
    return extras


def test_all_extra_exists() -> None:
    assert "all" in _extras()


def test_all_extra_covers_every_other_extra() -> None:
    extras = _extras()
    covered: set[str] = set()
    for requirement in extras["all"]:
        match = re.fullmatch(r"grimoire\[([^\]]+)\]", requirement.strip())
        assert match, f"'all' should only self-reference grimoire[...]: {requirement}"
        covered |= {name.strip() for name in match.group(1).split(",")}

    assert covered == set(extras) - {"all"}
