"""Security-domain chunker with source-type dispatch.

Phase 3 lands the ``SecurityChunker`` — a single chunker that internally
routes to the appropriate parser and chunking strategy based on
detected source type. For Sigma rules this means one chunk per rule with
full structured metadata. For prose it delegates to the existing
``RecursiveCharacterTextSplitter``. NVD CVE and MITRE ATT&CK handlers
arrive in Phases 4 and 5.

The chunker is **async** (inherits from the existing async ``Chunker``
ABC) but the Sigma and prose paths are CPU-bound; they use ``asyncio``
to yield the event loop so the caller isn't blocked.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from grimoire.core.chunker.base import Chunk, ChunkConfig, Chunker
from grimoire.core.chunker.recursive import (
    RecursiveCharacterTextSplitter,
    RecursiveChunkConfig,
)
from grimoire.strategies.security.corpus import SourceType, detect_source_type
from grimoire.strategies.security.metadata import SecurityMetadata
from grimoire.strategies.security.parsers.sigma import parse_sigma

__all__ = ["SecurityChunker"]


class SecurityChunker(Chunker):
    """Domain chunker for security content.

    Detects source type and dispatches to the appropriate handler:

    * ``sigma_rule`` → one chunk per rule with structured metadata.
    * ``prose`` / ``unknown`` → recursive prose chunking.
    * ``nvd_cve`` / ``mitre_attack`` → raise :class:`NotImplementedError`
      until Phases 4–5 land.

    Args:
        config: Chunking configuration (used for prose fallback).
    """

    def __init__(self, config: Optional[ChunkConfig] = None) -> None:
        """Initialize the security chunker.

        Args:
            config: Base chunk config. Used only by the prose fallback.
        """
        super().__init__(config)
        self._prose_chunker = RecursiveCharacterTextSplitter(
            config or RecursiveChunkConfig()
        )

    async def chunk(
        self,
        text: str,
        doc_id: Optional[str] = None,
        *,
        source_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Chunk]:
        """Chunk security-domain text using source-type dispatch.

        Args:
            text: Raw document text.
            doc_id: Optional document ID for continuity links.
            source_metadata: Optional metadata dict (may contain ``path`` or
                ``source_path`` for detection hints).

        Returns:
            List of :class:`Chunk` objects with continuity links.

        Raises:
            NotImplementedError: If the detected source type is ``nvd_cve`` or
                ``mitre_attack`` (handled in later phases).
            ValueError: If ``text`` is empty.
        """

        if not text or not text.strip():
            return []

        source_type = detect_source_type(text, source_metadata)

        if source_type is SourceType.SIGMA_RULE:
            return await self._chunk_sigma(text, doc_id)
        if source_type is SourceType.PROSE:
            return await self._chunk_prose(text, doc_id)
        if source_type is SourceType.UNKNOWN:
            return await self._chunk_prose(text, doc_id)
        if source_type is SourceType.NVD_CVE:
            raise NotImplementedError("NVD CVE chunking not yet implemented (Phase 4)")
        if source_type is SourceType.MITRE_ATTACK:
            raise NotImplementedError(
                "MITRE ATT&CK chunking not yet implemented (Phase 5)"
            )

        # IOC_LIST and any future types fall back to prose for now.
        return await self._chunk_prose(text, doc_id)

    async def _chunk_sigma(
        self, text: str, doc_id: Optional[str] = None
    ) -> List[Chunk]:
        """Chunk Sigma rules: one chunk per rule.

        Each chunk's ``metadata`` contains the rule's
        ``SecurityMetadata.to_chromadb_metadata()`` dict under the key
        ``"security_metadata"`` so that ``_embed_and_store`` can merge it
        into the ChromaDB payload.
        """

        # parse_sigma is CPU-bound YAML parsing; yield the loop.
        parsed = await asyncio.to_thread(parse_sigma, text)
        if not parsed:
            return []

        chunks: List[Chunk] = []
        for rule_text, sec_meta in parsed:
            token_count = self._count_tokens(rule_text)
            chunk = Chunk(
                content=rule_text,
                token_count=token_count,
                index=len(chunks),
                chunk_type="sigma_rule",
                source_type=sec_meta.source_type.value,
                metadata={
                    "security_metadata": sec_meta.to_chromadb_metadata(),
                    "strategy": "sigma_rule",
                },
            )
            chunks.append(chunk)

        if chunks:
            self._set_continuity_links(chunks, doc_id or "doc")
        return chunks

    async def _chunk_prose(
        self, text: str, doc_id: Optional[str] = None
    ) -> List[Chunk]:
        """Chunk prose content via ``RecursiveCharacterTextSplitter``.

        Stamps each chunk with ``chunk_type="prose"`` and
        ``source_type="prose"`` and an empty security metadata dict so
        downstream code has a consistent metadata shape.
        """

        prose_chunks = await self._prose_chunker.chunk(text, doc_id=doc_id)
        for chunk in prose_chunks:
            chunk.chunk_type = "prose"
            chunk.source_type = "prose"
            if "security_metadata" not in chunk.metadata:
                chunk.metadata["security_metadata"] = SecurityMetadata(
                    source_type=SourceType.PROSE
                ).to_chromadb_metadata()
        return prose_chunks
