"""Strategy abstraction layer for domain-specific ingestion and retrieval.

This package houses the abstract types (in :mod:`grimoire.strategies.base`)
and the concrete domain implementations (currently
:mod:`grimoire.strategies.security`).

Public re-exports (``BaseChunker``, ``BaseRetriever``, ``get_chunker_for``)
are loaded lazily via :pep:`562` ``__getattr__`` to avoid a circular import:
``grimoire.db.models`` imports the security enums from
:mod:`grimoire.strategies.security.metadata`, and Python's package-import
order would otherwise pull :mod:`grimoire.strategies.base` (and through it
``grimoire.core.dedup`` → ``grimoire.db.models``) before ``models`` finishes
defining its names.
"""

from typing import TYPE_CHECKING, Any

__all__ = ["BaseChunker", "BaseRetriever", "get_chunker_for"]

if TYPE_CHECKING:
    from grimoire.strategies.base import BaseChunker, BaseRetriever, get_chunker_for


def __getattr__(name: str) -> Any:
    if name in __all__:
        from grimoire.strategies import base as _base

        value = getattr(_base, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
