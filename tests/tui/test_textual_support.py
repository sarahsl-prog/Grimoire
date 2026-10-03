"""The declared Textual range, and the versions it has actually been run on.

``pyproject.toml`` says which Textual releases are allowed;
``scripts/test-textual-versions.sh`` lists the ones the TUI suite has been run
against.  A range nobody has run is a guess, so these tests keep the two from
drifting apart: lowering the floor, or adding a version to the script, has to
be done in both places.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("textual", reason="TUI extra not installed")

from packaging.requirements import Requirement  # noqa: E402
from packaging.version import Version  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def _textual_requirements() -> dict[str, Requirement]:
    """The ``textual`` requirement from each extra that declares one."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    found: dict[str, Requirement] = {}
    for extra, requirements in data["project"]["optional-dependencies"].items():
        for text in requirements:
            requirement = Requirement(text)
            if requirement.name == "textual":
                found[extra] = requirement
    return found


def _tested_versions() -> list[Version]:
    """``DEFAULT_VERSIONS`` from the matrix script."""
    script = (ROOT / "scripts" / "test-textual-versions.sh").read_text(encoding="utf-8")
    match = re.search(r"DEFAULT_VERSIONS=\(([^)]*)\)", script)
    assert match, "DEFAULT_VERSIONS not found in scripts/test-textual-versions.sh"
    return [Version(item) for item in match.group(1).split()]


def test_every_extra_declares_the_same_textual_range() -> None:
    """``tui`` and ``dev`` each carry their own copy; they must agree."""
    requirements = _textual_requirements()

    assert set(requirements) >= {"tui", "dev"}
    specifiers = {str(r.specifier) for r in requirements.values()}
    assert len(specifiers) == 1, requirements


def test_the_installed_textual_is_inside_the_declared_range() -> None:
    import textual

    (specifier,) = {r.specifier for r in _textual_requirements().values()}

    assert Version(textual.__version__) in specifier


def test_the_floor_is_a_version_the_suite_has_been_run_on() -> None:
    (specifier,) = {r.specifier for r in _textual_requirements().values()}
    floors = [Version(s.version) for s in specifier if s.operator == ">="]

    assert len(floors) == 1
    assert floors[0] in _tested_versions()


def test_every_tested_version_is_inside_the_declared_range() -> None:
    (specifier,) = {r.specifier for r in _textual_requirements().values()}

    outside = [v for v in _tested_versions() if v not in specifier]

    assert outside == []


def test_the_tested_versions_are_unique_and_ascending() -> None:
    tested = _tested_versions()

    assert tested == sorted(set(tested))
    assert len(tested) >= 2
