"""Strategy abstraction layer for domain-specific ingestion and retrieval.

Grimoire splits its content handling into "general" (prose, docs, mixed) and
"security" (Sigma rules, CVEs, MITRE ATT&CK, IOCs) domains. This module
defines the small set of abstract types those domain-specific implementations
must satisfy so the rest of the system can stay agnostic of which strategy is
in use.

Design notes:

* :class:`BaseChunker` is an alias for the existing async
  :class:`grimoire.core.chunker.base.Chunker` ABC. We intentionally do **not**
  introduce a parallel class hierarchy — domain chunkers subclass the same
  base every other chunker uses.
* :class:`BaseRetriever` defines the retriever contract. Concrete retrievers
  are expected to *compose* :class:`grimoire.search.hybrid.HybridSearch`
  rather than duplicate its merging/reranking logic. The signature
  intentionally mirrors a subset of ``HybridSearch.search`` so a thin wrapper
  can satisfy it directly.
* :func:`get_chunker_for` is a registry stub. The real lookup table is
  populated in later phases (the strategy loader arrives in Phase 8). Wiring
  call sites against the stub now lets us land caller changes incrementally
  without breaking imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from grimoire.core.chunker.base import Chunker as BaseChunker

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from grimoire.search.hybrid import HybridResult

__all__ = [
    "BaseChunker",
    "BaseRetriever",
    "get_chunker_for",
]


class BaseRetriever(ABC):
    """Abstract retriever interface for domain-specific search strategies.

    Concrete subclasses are expected to **compose** rather than replace
    :class:`grimoire.search.hybrid.HybridSearch`: instantiate (or receive)
    a ``HybridSearch`` and delegate the heavy lifting to it, layering any
    domain-specific filter construction, query rewriting, or post-processing
    on top.

    The :meth:`retrieve` signature mirrors the public surface of
    ``HybridSearch.search`` (the most common subset), so a minimal
    implementation can be a one-line passthrough.
    """

    @abstractmethod
    async def retrieve(
        self,
        db: AsyncSession,
        query: str,
        *,
        top_k: int = 10,
        filter_dict: Optional[Dict[str, Any]] = None,
    ) -> List[HybridResult]:
        """Retrieve ranked results for ``query``.

        Args:
            db: Database session, forwarded to FTS-backed components.
            query: User-provided search query string.
            top_k: Number of final results to return.
            filter_dict: Optional metadata filter dictionary, applied to the
                vector store and/or other backing search backends.

        Returns:
            List of :class:`HybridResult` ordered by descending relevance.
        """
        raise NotImplementedError("Subclasses must implement retrieve()")


def get_chunker_for(
    file_path: Union[str, Path],
    source_type: Optional[str] = None,
) -> Optional[BaseChunker]:
    """Return a chunker appropriate for ``file_path`` / ``source_type``.

    This is a stub. Real registration arrives in later phases (Phase 8
    strategy loader). The stub exists so callers can wire the lookup early
    without breaking — it always returns ``None``, signalling "no domain
    chunker registered; fall back to the default chunker pipeline".

    Args:
        file_path: Path to the source file (string or :class:`pathlib.Path`).
        source_type: Optional source-type hint, e.g. ``"sigma_rule"`` or
            ``"nvd_cve"``. Detection logic for this value lands in Phase 1.

    Returns:
        ``None`` for now. Future phases will return concrete
        :class:`BaseChunker` instances.
    """
    return None
