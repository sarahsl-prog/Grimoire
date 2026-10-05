"""Confine caller-supplied filesystem paths to configured roots.

Several network-facing operations take a path *on the server*: ingesting a file
or a directory, and starting a watch.  Ingestion copies whatever it reads into
the searchable corpus, so an unrestricted path is an arbitrary-file-read: point
it at ``/etc`` or a home directory and every supported file there becomes
searchable by any key.  This module is the one place that decides whether such
a path is acceptable, so the REST routes and the MCP tools cannot drift apart.

It knows nothing about HTTP or MCP: it raises :class:`PathNotAllowedError`, which says
whether the failure is a malformed request or a refusal, and each caller turns
that into its own error shape.  The CLI does not use it: running it already
requires a shell on the host.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

# Far longer than any real path (PATH_MAX is 4096 on Linux, usually 255 per
# component), short enough that it cannot be used to waste work.
MAX_PATH_LEN = 2048


class PathNotAllowedError(Exception):
    """A path was rejected.

    Attributes:
        message: Safe to show a caller.  Never contains the rejected path.
        forbidden: ``True`` when the request was well formed but refused (outside
            every allowed root); ``False`` when it was malformed (a null byte,
            or too long).  Callers map these to 403 and 400 respectively.
    """

    def __init__(self, message: str, *, forbidden: bool) -> None:
        super().__init__(message)
        self.message = message
        self.forbidden = forbidden


def _canonical(path: Path) -> Path:
    """Absolute, ``..`` collapsed, and every symlink followed."""
    resolved = path.resolve()
    try:
        return Path(os.path.realpath(resolved))
    except OSError:
        return resolved


def resolve_allowed(raw_path: str, roots: Sequence[Path]) -> Path:
    """Return the canonical form of ``raw_path`` if it lies inside a root.

    The path is resolved *before* it is compared, so ``..`` segments and
    symlinks cannot be used to climb out of a root, and the roots are resolved
    the same way, so a root that is itself a symlink is judged by where it
    points.  ``is_relative_to`` compares path components, so ``/data-evil`` is
    not inside ``/data``.

    Args:
        raw_path: What the caller supplied.
        roots: The allowed roots.  Empty means nothing is allowed.

    Raises:
        PathNotAllowedError: The path is malformed or outside every root.
    """
    if "\x00" in raw_path:
        raise PathNotAllowedError(
            "Null bytes are not allowed in a path.", forbidden=False
        )
    if len(raw_path) > MAX_PATH_LEN:
        raise PathNotAllowedError(
            f"A path can be at most {MAX_PATH_LEN} characters.", forbidden=False
        )

    real = _canonical(Path(raw_path))
    canonical_roots = [_canonical(Path(root)) for root in roots]
    if any(real.is_relative_to(root) for root in canonical_roots):
        return real

    allowed = ", ".join(str(r) for r in canonical_roots) or "(none configured)"
    raise PathNotAllowedError(
        f"Path is not in an allowed directory. Allowed: {allowed}.", forbidden=True
    )
