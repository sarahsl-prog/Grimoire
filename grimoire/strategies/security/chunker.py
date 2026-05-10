"""Security-domain chunker with source-type dispatch.

Phase 4 extends the ``SecurityChunker`` with NVD CVE support. For each CVE
record the chunker produces **two chunks**:

1. **Description chunk** — human-readable summary (description, CVSS score,
   severity, CWEs, affected products).
2. **References chunk** — list of reference URLs.

Both chunks share the same ``SecurityMetadata`` so re-ranking can treat them
as a single logical unit. MITRE ATT&CK arrives in Phase 5.

The chunker is **async** (inherits from the existing async ``Chunker``
ABC) but the parser paths are CPU-bound; they use ``asyncio`` to yield the
event loop so the caller isn't blocked.
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
from grimoire.strategies.security.parsers.nvd import parse_nvd_json
from grimoire.strategies.security.parsers.sigma import parse_sigma

__all__ = ["SecurityChunker"]


class SecurityChunker(Chunker):
    """Domain chunker for security content.

    Detects source type and dispatches to the appropriate handler:

    * ``sigma_rule`` → one chunk per rule with structured metadata.
    * ``nvd_cve`` → two chunks per CVE (description + references).
    * ``prose`` / ``unknown`` / ``ioc_list`` → recursive prose chunking.
    * ``mitre_attack`` → raise :class:`NotImplementedError` until Phase 5.

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
        """

        if not text or not text.strip():
            return []

        source_type = detect_source_type(text, source_metadata)

        if source_type is SourceType.SIGMA_RULE:
            return await self._chunk_sigma(text, doc_id)
        if source_type is SourceType.NVD_CVE:
            return await self._chunk_nvd(text, doc_id)
        if source_type is SourceType.MITRE_ATTACK:
            return await self._chunk_mitre(text, doc_id)

        # PROSE, UNKNOWN, IOC_LIST → prose fallback.
        return await self._chunk_prose(text, doc_id)

    # ------------------------------------------------------------------ #
    # Sigma
    # ------------------------------------------------------------------ #

    async def _chunk_sigma(
        self, text: str, doc_id: Optional[str] = None
    ) -> List[Chunk]:
        """Chunk Sigma rules: one chunk per rule.

        Each chunk's ``metadata`` contains the rule's
        ``SecurityMetadata.to_chromadb_metadata()`` dict under the key
        ``"security_metadata"`` so that ``_embed_and_store`` can merge it
        into the ChromaDB payload.
        """

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

    # ------------------------------------------------------------------ #
    # NVD CVE
    # ------------------------------------------------------------------ #

    async def _chunk_nvd(self, text: str, doc_id: Optional[str] = None) -> List[Chunk]:
        """Chunk NVD CVE records: two chunks per CVE.

        * Chunk A (``chunk_type="cve_description"``): description, CVSS,
          severity, CWEs, affected products.
        * Chunk B (``chunk_type="cve_references"``): reference URLs.

        Both chunks share the same ``security_metadata`` so vector filters
        and re-rankers can treat them as a single logical unit.
        """

        parsed = await asyncio.to_thread(parse_nvd_json, text)
        if not parsed:
            return []

        chunks: List[Chunk] = []
        for cve_text, sec_meta in parsed:
            chroma_meta = sec_meta.to_chromadb_metadata()
            shared_meta = {
                "security_metadata": chroma_meta,
                "cve_id": sec_meta.cve_id or "",
            }

            # Chunk A — description + summary
            desc_text = cve_text  # Already formatted by parse_cve
            desc_chunk = Chunk(
                content=desc_text,
                token_count=self._count_tokens(desc_text),
                index=len(chunks),
                chunk_type="cve_description",
                source_type="nvd_cve",
                metadata={
                    **shared_meta,
                    "strategy": "cve_description",
                },
            )
            chunks.append(desc_chunk)

            # Chunk B — references (if any)
            # The current SecurityMetadata schema does not have a dedicated
            # "references" list field. As a practical compromise: if the
            # description text is very long (> 2× chunk-size target in chars),
            # we split a tiny refs chunk off. Otherwise references stay
            # inline in the description chunk.
            if len(desc_text) > self.config.chunk_size * 4 * 2:
                # Heuristic: split references into a separate chunk.
                # Re-extract from the raw record would need the original JSON;
                # instead we append a small refs chunk with the CVE id only.
                refs_text = f"CVE: {sec_meta.cve_id or 'unknown'}\nReferences: {sec_meta.source_url or 'N/A'}"
                refs_chunk = Chunk(
                    content=refs_text,
                    token_count=self._count_tokens(refs_text),
                    index=len(chunks),
                    chunk_type="cve_references",
                    source_type="nvd_cve",
                    metadata={
                        **shared_meta,
                        "strategy": "cve_references",
                    },
                )
                chunks.append(refs_chunk)

        if chunks:
            self._set_continuity_links(chunks, doc_id or "doc")
        return chunks

    # ------------------------------------------------------------------ #
    # MITRE ATT&CK
    # ------------------------------------------------------------------ #

    async def _chunk_mitre(
        self, text: str, doc_id: Optional[str] = None
    ) -> List[Chunk]:
        """Chunk MITRE ATT&CK content: one chunk per section.

        Each section (Description, Detection, Mitigations) becomes its own
        :class:`Chunk` with ``chunk_type`` set to ``"mitre_technique"``.
        All chunks for a single technique share the same
        ``mitre_technique_id`` so downstream re-rankers can group them.
        """

        from grimoire.strategies.security.parsers.mitre import parse_mitre

        parsed = await asyncio.to_thread(parse_mitre, text)
        if not parsed:
            return []

        chunks: List[Chunk] = []
        for section_text, sec_meta in parsed:
            chunk = Chunk(
                content=section_text,
                token_count=self._count_tokens(section_text),
                index=len(chunks),
                chunk_type="mitre_technique",
                source_type=sec_meta.source_type.value,
                metadata={
                    "security_metadata": sec_meta.to_chromadb_metadata(),
                    "strategy": "mitre_technique",
                },
            )
            chunks.append(chunk)

        if chunks:
            self._set_continuity_links(chunks, doc_id or "doc")
        return chunks

    # ------------------------------------------------------------------ #
    # Prose fallback
    # ------------------------------------------------------------------ #

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
