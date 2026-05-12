"""Strategy loader — single entry point for domain-aware chunker/retriever.

The Phase 8 strategy loader is the seam between Grimoire's "general" and
"security" domains. Callers (agents, factories, dependency injectors) ask
the loader for a chunker / retriever; the loader inspects
``settings.security.domain`` and returns either the security-domain
implementation (`SecurityChunker` / `SecurityRetriever`) or ``None`` to
signal "use the default general-domain pipeline".

Design notes:

* The functions return optional values rather than raising on missing config
  so callers can keep a simple ``loader.load_xxx(...) or _default_xxx()``
  pattern. A missing or unrecognized ``settings.security.domain`` defaults
  to ``general`` (i.e. ``None`` returned).
* The loader does not own the existing default chunker/retriever
  construction. Each call site already has that logic (``_create_chunker``
  in ``agents/ingestion.py``, ``HybridSearch`` use in ``QueryAgent``), so
  centralising it here would be premature.
* Imports of ``SecurityChunker`` / ``SecurityRetriever`` are deferred to
  inside the functions to keep cold-import cost on the general-domain path
  at zero — the security package lazy-loads heavy submodules itself, but
  even the lazy entry has a small fixed cost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from loguru import logger

if TYPE_CHECKING:
    from grimoire.config.settings import GrimoireSettings
    from grimoire.core.chunker.base import ChunkConfig, Chunker
    from grimoire.search.hybrid import HybridSearch
    from grimoire.strategies.base import BaseRetriever

__all__ = ["load_chunker", "load_retriever"]


def load_chunker(
    settings: GrimoireSettings,
    *,
    chunk_config: Optional[ChunkConfig] = None,
) -> Optional[Chunker]:
    """Return a domain-specific chunker, or ``None`` for the general pipeline.

    When ``settings.security.domain == "security"`` this returns a
    :class:`grimoire.strategies.security.chunker.SecurityChunker` which
    internally dispatches per detected source type. Otherwise returns
    ``None`` — callers should fall back to their existing per-extension
    chunker selection.

    Args:
        settings: Grimoire settings.
        chunk_config: Optional ChunkConfig forwarded to the SecurityChunker's
            prose fallback. Defaults to the security chunker's own default
            when omitted.

    Returns:
        A ``Chunker`` instance when in security domain, else ``None``.
    """
    domain = _get_domain(settings)
    if domain != "security":
        return None

    from grimoire.strategies.security.chunker import SecurityChunker

    logger.debug("Strategy loader: domain=security → SecurityChunker")
    # Forward the full GrimoireSettings — SecurityChunker reads
    # ``settings.security.llm_extract_enabled`` and the optional
    # SecurityMetadataExtractor inside it needs ``settings.llm``.
    # SecurityChunker promotes the optional ``chunk_config`` to a
    # RecursiveChunkConfig internally, so callers don't need to.
    return SecurityChunker(config=chunk_config, settings=settings)


def load_retriever(
    settings: GrimoireSettings,
    hybrid_search: HybridSearch,
) -> Optional[BaseRetriever]:
    """Return a domain-specific retriever, or ``None`` for the general pipeline.

    When ``settings.security.domain == "security"`` this returns a
    :class:`grimoire.strategies.security.retriever.SecurityRetriever`
    wrapping the supplied ``hybrid_search``. Otherwise returns ``None`` —
    callers should keep using ``HybridSearch`` directly.

    Args:
        settings: Grimoire settings.
        hybrid_search: The hybrid search engine to wrap.

    Returns:
        A ``BaseRetriever`` instance when in security domain, else ``None``.
    """
    domain = _get_domain(settings)
    if domain != "security":
        return None

    from grimoire.strategies.security.retriever import SecurityRetriever

    logger.debug("Strategy loader: domain=security → SecurityRetriever")
    return SecurityRetriever(hybrid=hybrid_search, settings=settings)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_domain(settings: GrimoireSettings) -> str:
    """Pull ``settings.security.domain`` with a safe fallback.

    Returns ``"general"`` if the ``security`` block is missing or the field
    is missing. This keeps older settings instances (e.g. from on-disk YAML
    predating Phase 7) working without a migration.
    """
    security = getattr(settings, "security", None)
    if security is None:
        return "general"
    return getattr(security, "domain", "general") or "general"
