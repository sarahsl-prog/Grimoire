"""``/ingest`` server-path handling goes through the shared guard and the setting.

The default is ``/tmp`` only; operators widen it with ``api.allowed_roots``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from grimoire.api.routes.ingest import _is_path_allowed
from grimoire.config.settings import APIConfig


def _with_roots(*roots: Path):  # type: ignore[no-untyped-def]
    settings = MagicMock()
    settings.api = APIConfig(allowed_roots=list(roots))
    return patch("grimoire.config.settings.get_settings", return_value=settings)


def test_the_default_allows_tmp() -> None:
    assert _is_path_allowed("/tmp/some-doc.txt") == Path("/tmp/some-doc.txt").resolve()


@pytest.mark.parametrize("raw", ["/home/sunds/notes.md", "/home/anyone/notes.md"])
def test_home_directories_are_rejected_by_default(raw: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _is_path_allowed(raw)
    assert exc.value.status_code == 403
    assert "notes.md" not in str(exc.value.detail)


def test_a_configured_root_is_honoured(tmp_path: Path) -> None:
    target = tmp_path / "doc.txt"
    target.write_text("x")

    with _with_roots(tmp_path):
        assert _is_path_allowed(str(target)) == target.resolve()


def test_configuring_a_root_replaces_the_default(tmp_path: Path) -> None:
    with _with_roots(tmp_path), pytest.raises(HTTPException) as exc:
        _is_path_allowed("/tmp/some-doc.txt")
    assert exc.value.status_code == 403


def test_the_refusal_names_the_configured_roots(tmp_path: Path) -> None:
    with _with_roots(tmp_path), pytest.raises(HTTPException) as exc:
        _is_path_allowed("/etc/passwd")
    assert str(tmp_path.resolve()) in str(exc.value.detail)


def test_no_roots_refuses_everything() -> None:
    with _with_roots(), pytest.raises(HTTPException) as exc:
        _is_path_allowed("/tmp/some-doc.txt")
    assert exc.value.status_code == 403


def test_a_malformed_path_is_a_400_not_a_403() -> None:
    with pytest.raises(HTTPException) as exc:
        _is_path_allowed("/tmp/a\x00b")
    assert exc.value.status_code == 400
