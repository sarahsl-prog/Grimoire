"""Domain-specific chunking and retrieval strategies.

This package houses pluggable strategy implementations that adapt Grimoire's
ingestion and query pipelines to specialized content domains. Phase 0 wires
in only the abstract scaffolding; concrete general-purpose and security
strategies arrive in subsequent phases (see
``docs/plans/security_strategy_plan.md``).

Public surface:

* :class:`BaseChunker` — alias for the existing :class:`grimoire.core.chunker.base.Chunker`
  ABC, exposed here so callers can import strategy types from a single place.
* :class:`BaseRetriever` — abstract retriever interface that concrete
  implementations satisfy by composing :class:`grimoire.search.hybrid.HybridSearch`.
* :func:`get_chunker_for` — registry stub for chunker dispatch by file path
  and source type.
"""

from grimoire.strategies.base import BaseChunker, BaseRetriever, get_chunker_for

__all__ = [
    "BaseChunker",
    "BaseRetriever",
    "get_chunker_for",
]
