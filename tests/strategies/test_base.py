"""Tests for the Phase 0 strategy scaffolding.

Covers the public surface introduced by ``grimoire.strategies``:

* package imports resolve cleanly,
* ``BaseChunker`` is an alias (not a parallel class) of the core ``Chunker``,
* ``BaseRetriever`` is abstract and minimally subclassable,
* ``get_chunker_for`` stub returns ``None`` without raising,
* the ``Chunk`` model accepts the new optional ``chunk_type`` /
  ``source_type`` fields and remains backward-compatible.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.core.chunker.base import Chunk, Chunker
from grimoire.search.hybrid import HybridResult
from grimoire.strategies import BaseChunker, BaseRetriever, get_chunker_for


def test_strategies_package_imports() -> None:
    """All strategy submodules import without error."""
    import grimoire.strategies  # noqa: F401
    import grimoire.strategies.base  # noqa: F401
    import grimoire.strategies.security  # noqa: F401


def test_base_chunker_is_core_chunker() -> None:
    """``BaseChunker`` must be the same class as the core ``Chunker`` ABC."""
    assert BaseChunker is Chunker


def test_base_retriever_is_abstract() -> None:
    """Instantiating ``BaseRetriever`` directly must raise ``TypeError``."""
    with pytest.raises(TypeError):
        BaseRetriever()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_base_retriever_subclass_works() -> None:
    """A minimal subclass that implements ``retrieve`` must work."""

    class _NoopRetriever(BaseRetriever):
        async def retrieve(
            self,
            db: AsyncSession,
            query: str,
            *,
            top_k: int = 10,
            filter_dict: Optional[Dict[str, Any]] = None,
        ) -> List[HybridResult]:
            return []

    retriever = _NoopRetriever()
    result = await retriever.retrieve(db=None, query="x")  # type: ignore[arg-type]
    assert result == []


def test_get_chunker_for_stub_returns_none() -> None:
    """``get_chunker_for`` must return ``None`` for any input in Phase 0."""
    assert get_chunker_for("foo.md") is None
    assert get_chunker_for("foo.yml", source_type="sigma_rule") is None


def test_chunk_accepts_new_optional_fields() -> None:
    """``Chunk`` accepts and round-trips ``chunk_type`` and ``source_type``."""
    chunk = Chunk(
        content="x",
        token_count=1,
        index=0,
        chunk_type="sigma_rule",
        source_type="sigma",
    )
    assert chunk.chunk_type == "sigma_rule"
    assert chunk.source_type == "sigma"


def test_chunk_without_new_fields_still_validates() -> None:
    """Existing chunks (no new fields) keep working with ``None`` defaults."""
    chunk = Chunk(content="x", token_count=1, index=0)
    assert chunk.chunk_type is None
    assert chunk.source_type is None
