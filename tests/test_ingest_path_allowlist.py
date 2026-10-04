"""``/ingest`` path allowlist is ``/tmp`` only.

A hardcoded developer home directory has no business in a shipped allowlist.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from grimoire.api.routes.ingest import _ALLOWED_ROOTS, _is_path_allowed


def test_allowlist_is_tmp_only() -> None:
    assert [str(r) for r in _ALLOWED_ROOTS] == [str(Path("/tmp").resolve())]


def test_tmp_paths_are_still_allowed() -> None:
    assert _is_path_allowed("/tmp/some-doc.txt") == Path("/tmp/some-doc.txt").resolve()


@pytest.mark.parametrize("raw", ["/home/sunds/notes.md", "/home/anyone/notes.md"])
def test_home_directories_are_rejected(raw: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _is_path_allowed(raw)
    assert exc.value.status_code == 403
    assert "/home/sunds" not in str(exc.value.detail)
