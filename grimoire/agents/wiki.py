"""WikiAgent — compiles raw source documents into structured wiki pages.

Reads ingested documents, extracts entities via LLM, creates or updates
wiki pages with section-level granularity, detects contradictions
(newer-wins for factual/temporal, source-priority for scope/terminology),
and maintains cross-references between pages.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Optional

import httpx
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.db.models import (
    Chunk,
    CompileStatus,
    Document,
    WikiCompileJob,
    WikiCrossReference,
    WikiPage,
    WikiPageSection,
    WikiPageStatus,
    WikiRefType,
)


# ============================================================================
# Data Models
# ============================================================================


class ContradictionAction(str, Enum):
    """Action to take on a detected contradiction."""

    NEWER_WINS = "newer_wins"
    SOURCE_PRIORITY = "source_priority"
    NO_CONFLICT = "no_conflict"


@dataclass
class EntityExtraction:
    """An entity or concept identified in a source document."""

    name: str
    entity_type: str
    summary: str
    confidence: float


@dataclass
class ContradictionResult:
    """Result of comparing new content against existing wiki content."""

    conflict_type: str
    description: str
    existing_claim: str
    new_claim: str
    severity: str


@dataclass
class CompileResult:
    """Result of compiling a single document into wiki pages."""

    document_id: str
    pages_created: int = 0
    pages_updated: int = 0
    sections_added: int = 0
    sections_superseded: int = 0
    contradictions_found: int = 0
    cross_references_added: int = 0
    error: str | None = None


# ============================================================================
# WikiAgent
# ============================================================================


class WikiAgent:
    """Compiles raw source documents into structured, cross-referenced wiki pages."""

    def __init__(
        self,
        llm_url: str = "http://localhost:11434",
        llm_model: str = "llama3:8b",
        fallback_llm_url: str | None = None,
        fallback_llm_model: str | None = None,
        source_priorities: dict[str, int] | None = None,
        max_sections_per_page: int = 10,
        max_compile_batch_size: int = 20,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> None:
        self._llm_url = llm_url.rstrip("/")
        self._llm_model = llm_model
        self._fallback_llm_url = fallback_llm_url.rstrip("/") if fallback_llm_url else None
        self._fallback_llm_model = fallback_llm_model or llm_model
        self._source_priorities = source_priorities or {}
        self._max_sections_per_page = max_sections_per_page
        self._max_compile_batch_size = max_compile_batch_size
        self._temperature = temperature
        self._max_tokens = max_tokens
        logger.debug(f"WikiAgent initialized (model={llm_model}, fallback_url={fallback_llm_url})")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compile_document(
        self,
        db: AsyncSession,
        document_id: str,
    ) -> CompileResult:
        """Compile a single document into wiki pages."""
        result = CompileResult(document_id=document_id)
        logger.info(f"Starting wiki compile for document {document_id}")

        job = await self._get_or_create_job(db, document_id)
        if job.status == CompileStatus.COMPLETED:
            logger.info(f"Document {document_id} already compiled, skipping")
            return result
        job.status = CompileStatus.COMPILING
        await db.flush()

        try:
            chunks = await self._fetch_chunks(db, document_id)
            if not chunks:
                logger.warning(f"No chunks found for document {document_id} — was it ingested?")
                job.status = CompileStatus.COMPLETED
                job.compiled_at = datetime.now(timezone.utc)
                await db.flush()
                return result

            doc = await db.get(Document, document_id)
            source_priority = self._resolve_source_priority(
                doc.source_path if doc else ""
            )
            logger.debug(f"Document {document_id}: {len(chunks)} chunks, source_priority={source_priority}")

            chunk_texts = [c.content for c in chunks if c.content]
            entities = await self._identify_entities(chunk_texts)
            if not entities:
                logger.warning(
                    f"No entities extracted from document {document_id} — "
                    "LLM may have failed or returned unparseable output"
                )
                job.status = CompileStatus.COMPLETED
                job.compiled_at = datetime.now(timezone.utc)
                await db.flush()
                return result

            logger.info(f"Document {document_id}: extracted {len(entities)} entities")

            for entity in entities:
                existing_page = await self._match_existing_page(db, entity.name)

                if existing_page is None:
                    logger.debug(f"Creating new wiki page: '{entity.name}' ({entity.entity_type})")
                    page = await self._generate_page(
                        db, entity, document_id, source_priority, chunk_texts
                    )
                    result.pages_created += 1
                    result.sections_added += len(page.sections)
                else:
                    logger.debug(f"Updating existing wiki page: '{entity.name}'")
                    updated = await self._update_page(
                        db, existing_page, entity, document_id,
                        source_priority, chunk_texts,
                    )
                    result.pages_updated += 1
                    result.sections_added += updated.sections_added
                    result.sections_superseded += updated.sections_superseded
                    result.contradictions_found += updated.contradictions_found

            for entity in entities:
                page = await self._match_existing_page(db, entity.name)
                if page:
                    refs = await self._discover_cross_references(
                        db, page, [e.name for e in entities]
                    )
                    result.cross_references_added += refs

            job.status = CompileStatus.COMPLETED
            job.compiled_at = datetime.now(timezone.utc)
            await db.flush()
            logger.info(
                f"Compiled {document_id}: {result.pages_created} created, "
                f"{result.pages_updated} updated, "
                f"{result.contradictions_found} contradictions"
            )

        except Exception as e:
            logger.error(f"Wiki compile failed for {document_id}: {e}")
            job.status = CompileStatus.FAILED
            job.error_message = str(e)
            result.error = str(e)
            await db.flush()

        return result

    async def compile_pending(self, db: AsyncSession) -> list[CompileResult]:
        """Compile all wiki-pending documents."""
        stmt = (
            select(WikiCompileJob)
            .where(WikiCompileJob.status == CompileStatus.PENDING)
            .limit(self._max_compile_batch_size)
        )
        result = await db.execute(stmt)
        jobs = result.scalars().all()

        results: list[CompileResult] = []
        for job in jobs:
            compile_result = await self.compile_document(db, job.document_id)
            results.append(compile_result)

        return results

    # ------------------------------------------------------------------
    # Entity Extraction
    # ------------------------------------------------------------------

    async def _identify_entities(
        self, chunks: list[str]
    ) -> list[EntityExtraction]:
        """Use LLM to identify entities/concepts in document chunks."""
        combined = "\n\n".join(chunks[:5])
        prompt = (
            "Identify the key entities, concepts, components, or processes "
            "described in this document. For each, provide a name, type "
            "(concept/component/process/entity), a 1-2 sentence summary, "
            "and a confidence score (0.0-1.0) for how central it is.\n\n"
            "Return a JSON array: "
            '[{"name": "...", "entity_type": "...", "summary": "...", '
            '"confidence": 0.9}]\n\n'
            f"Document content:\n{combined}"
        )

        response = await self._call_llm(prompt)
        return self._parse_entity_response(response)

    def _parse_entity_response(self, response: str) -> list[EntityExtraction]:
        """Parse LLM entity extraction response."""
        try:
            match = re.search(r"\[.*\]", response, re.DOTALL)
            if not match:
                return []
            data = json.loads(match.group())
            entities: list[EntityExtraction] = []
            for item in data:
                if isinstance(item, dict) and "name" in item:
                    entities.append(
                        EntityExtraction(
                            name=item["name"],
                            entity_type=item.get("entity_type", "concept"),
                            summary=item.get("summary", ""),
                            confidence=float(item.get("confidence", 0.5)),
                        )
                    )
            return entities
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"Failed to parse entity extraction: {e}")
            return []

    # ------------------------------------------------------------------
    # Page Matching
    # ------------------------------------------------------------------

    async def _match_existing_page(
        self, db: AsyncSession, entity_name: str
    ) -> WikiPage | None:
        """Find an existing wiki page by slug match."""
        slug = self._slugify(entity_name)
        stmt = select(WikiPage).where(WikiPage.slug == slug)
        result = await db.execute(stmt)
        return result.scalars().first()

    # ------------------------------------------------------------------
    # Page Generation
    # ------------------------------------------------------------------

    async def _generate_page(
        self,
        db: AsyncSession,
        entity: EntityExtraction,
        document_id: str,
        source_priority: int,
        chunk_texts: list[str],
    ) -> WikiPage:
        """Generate a new wiki page for an entity."""
        slug = self._slugify(entity.name)
        combined = "\n\n".join(chunk_texts[:5])

        prompt = (
            f"Write a wiki page about '{entity.name}' "
            f"(type: {entity.entity_type}) based on this source document. "
            "Use markdown sections (## Heading). Each section should be "
            "a focused, factual description. Limit to "
            f"{self._max_sections_per_page} sections.\n\n"
            "Return the page as JSON:\n"
            '{"sections": [{"heading": "...", "content": "..."}]}\n\n'
            f"Source content:\n{combined}"
        )

        response = await self._call_llm(prompt)
        sections_data = self._parse_sections_response(response)

        page = WikiPage(
            title=entity.name,
            slug=slug,
            content="",
            version=1,
            status=WikiPageStatus.COMPILED,
            entity_type=entity.entity_type,
        )
        db.add(page)
        await db.flush()

        for i, sec in enumerate(sections_data[: self._max_sections_per_page]):
            section = WikiPageSection(
                wiki_page_id=page.id,
                heading=sec.get("heading", f"Section {i + 1}"),
                content=sec.get("content", ""),
                section_index=i,
                source_document_id=document_id,
                source_priority=source_priority,
            )
            db.add(section)

        await db.flush()
        await self._assemble_page_content(db, page)
        return page

    # ------------------------------------------------------------------
    # Page Update
    # ------------------------------------------------------------------

    @dataclass
    class _UpdateStats:
        sections_added: int = 0
        sections_superseded: int = 0
        contradictions_found: int = 0

    async def _update_page(
        self,
        db: AsyncSession,
        existing_page: WikiPage,
        entity: EntityExtraction,
        document_id: str,
        source_priority: int,
        chunk_texts: list[str],
    ) -> _UpdateStats:
        """Update an existing wiki page with new source content."""
        stats = self._UpdateStats()
        combined = "\n\n".join(chunk_texts[:5])

        stmt = (
            select(WikiPageSection)
            .where(
                WikiPageSection.wiki_page_id == existing_page.id,
                WikiPageSection.superseded_by_section_id.is_(None),
            )
            .order_by(WikiPageSection.section_index)
        )
        result = await db.execute(stmt)
        current_sections = result.scalars().all()

        current_summary = "\n".join(
            f"## {s.heading}\n{s.content}" for s in current_sections
        )

        prompt = (
            f"Here is the existing wiki page for '{entity.name}':\n\n"
            f"{current_summary}\n\n"
            "Here is new source content:\n\n"
            f"{combined}\n\n"
            "Based on the new source, return JSON with sections to add or update:\n"
            '{"updates": [{"heading": "...", "content": "...", '
            '"action": "add" or "update", '
            '"existing_heading": "..." (if update), '
            '"conflict_type": null or "factual"/"temporal"/"scope"/"terminology", '
            '"conflict_description": null or "..."}]}\n'
            "Only include sections that the new source actually changes or adds."
        )

        response = await self._call_llm(prompt)
        updates = self._parse_updates_response(response)

        for update in updates:
            heading = update.get("heading", "New Section")
            content = update.get("content", "")
            action = update.get("action", "add")
            conflict_type = update.get("conflict_type")
            conflict_desc = update.get("conflict_description")

            if action == "add":
                new_index = len(current_sections) + stats.sections_added
                section = WikiPageSection(
                    wiki_page_id=existing_page.id,
                    heading=heading,
                    content=content,
                    section_index=new_index,
                    source_document_id=document_id,
                    source_priority=source_priority,
                )
                if conflict_type and conflict_desc:
                    section.contradiction_flag = conflict_desc
                    stats.contradictions_found += 1
                db.add(section)
                stats.sections_added += 1

            elif action == "update":
                existing_heading = update.get("existing_heading", heading)
                existing_section = next(
                    (s for s in current_sections if s.heading == existing_heading),
                    None,
                )
                if existing_section and conflict_type:
                    contradiction = ContradictionResult(
                        conflict_type=conflict_type,
                        description=conflict_desc or "",
                        existing_claim=existing_section.content[:200],
                        new_claim=content[:200],
                        severity="medium",
                    )
                    policy = self._apply_contradiction_policy(contradiction)
                    stats.contradictions_found += 1

                    if policy == ContradictionAction.NEWER_WINS:
                        new_index = len(current_sections) + stats.sections_added
                        new_section = WikiPageSection(
                            wiki_page_id=existing_page.id,
                            heading=heading,
                            content=content,
                            section_index=new_index,
                            source_document_id=document_id,
                            source_priority=source_priority,
                        )
                        db.add(new_section)
                        await db.flush()
                        existing_section.superseded_by_section_id = new_section.id
                        stats.sections_superseded += 1
                        stats.sections_added += 1

                    elif policy == ContradictionAction.SOURCE_PRIORITY:
                        if source_priority > existing_section.source_priority:
                            new_index = len(current_sections) + stats.sections_added
                            new_section = WikiPageSection(
                                wiki_page_id=existing_page.id,
                                heading=heading,
                                content=content,
                                section_index=new_index,
                                source_document_id=document_id,
                                source_priority=source_priority,
                            )
                            db.add(new_section)
                            await db.flush()
                            existing_section.superseded_by_section_id = new_section.id
                            existing_section.contradiction_flag = (
                                f"Superseded by higher-priority source: {conflict_desc}"
                            )
                            stats.sections_superseded += 1
                            stats.sections_added += 1
                        else:
                            existing_section.contradiction_flag = (
                                f"Lower-priority source conflicts: {conflict_desc}"
                            )
                elif existing_section:
                    existing_section.content = content
                    existing_section.source_document_id = document_id
                    existing_section.source_priority = source_priority
                    stats.sections_added += 1

        existing_page.version += 1
        existing_page.updated_at = datetime.now(timezone.utc)
        await db.flush()
        await self._assemble_page_content(db, existing_page)
        return stats

    # ------------------------------------------------------------------
    # Contradiction Handling
    # ------------------------------------------------------------------

    async def _detect_contradictions(
        self,
        existing_section: WikiPageSection,
        new_content: str,
    ) -> ContradictionResult | None:
        """Use LLM to detect contradictions between existing and new content."""
        prompt = (
            "Compare these two statements about the same topic and determine "
            "if there is a contradiction.\n\n"
            f"Existing: {existing_section.content[:500]}\n\n"
            f"New: {new_content[:500]}\n\n"
            "If there is a contradiction, return JSON:\n"
            '{"conflict_type": "factual"/"temporal"/"scope"/"terminology", '
            '"description": "...", "existing_claim": "...", '
            '"new_claim": "...", "severity": "high"/"medium"/"low"}\n\n'
            'If no contradiction, return: "none"'
        )

        response = await self._call_llm(prompt)
        return self._parse_contradiction_response(response)

    def _apply_contradiction_policy(
        self, contradiction: ContradictionResult
    ) -> ContradictionAction:
        """Route contradiction to the correct policy."""
        if contradiction.conflict_type in ("factual", "temporal"):
            return ContradictionAction.NEWER_WINS
        elif contradiction.conflict_type in ("scope", "terminology"):
            return ContradictionAction.SOURCE_PRIORITY
        return ContradictionAction.NEWER_WINS

    # ------------------------------------------------------------------
    # Cross-References
    # ------------------------------------------------------------------

    async def _discover_cross_references(
        self,
        db: AsyncSession,
        page: WikiPage,
        entity_names: list[str],
    ) -> int:
        """Discover and create cross-references from a page to other pages."""
        slugs = [self._slugify(name) for name in entity_names if self._slugify(name) != page.slug]
        if not slugs:
            return 0

        stmt = select(WikiPage).where(WikiPage.slug.in_(slugs))
        result = await db.execute(stmt)
        target_pages = result.scalars().all()

        added = 0
        for target in target_pages:
            existing = await db.execute(
                select(WikiCrossReference).where(
                    WikiCrossReference.source_page_id == page.id,
                    WikiCrossReference.target_page_id == target.id,
                )
            )
            if existing.scalars().first():
                continue

            ref = WikiCrossReference(
                source_page_id=page.id,
                target_page_id=target.id,
                ref_type=WikiRefType.RELATED_TO,
                context=f"Discovered during compilation of {page.title}",
            )
            db.add(ref)
            added += 1

        if added:
            await db.flush()
        return added

    # ------------------------------------------------------------------
    # Page Assembly
    # ------------------------------------------------------------------

    async def _assemble_page_content(
        self, db: AsyncSession, page: WikiPage
    ) -> None:
        """Assemble section rows into the page's content field."""
        stmt = (
            select(WikiPageSection)
            .where(WikiPageSection.wiki_page_id == page.id)
            .order_by(WikiPageSection.section_index)
        )
        result = await db.execute(stmt)
        sections = result.scalars().all()

        refs_stmt = select(WikiCrossReference).where(
            WikiCrossReference.source_page_id == page.id
        )
        refs_result = await db.execute(refs_stmt)
        cross_refs = refs_result.scalars().all()

        lines: list[str] = []
        lines.append(f"# {page.title}\n")
        lines.append(
            f"> Entity type: {page.entity_type or 'unknown'} | "
            f"Version: {page.version} | "
            f"Last compiled: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n"
        )

        for section in sections:
            if section.superseded_by_section_id:
                lines.append(f"~~## {section.heading} (superseded)~~\n")
                lines.append(f"~~{section.content}~~\n")
                lines.append(
                    f"*Superseded by: section above | Source document: "
                    f"{section.source_document_id}*\n"
                )
            else:
                lines.append(f"## {section.heading}\n")
                lines.append(f"{section.content}\n")
                priority_note = f"Priority: {section.source_priority}" if section.source_priority else ""
                source_note = f"Source document: {section.source_document_id}" if section.source_document_id else ""
                attribution = " | ".join(filter(None, [source_note, priority_note]))
                if attribution:
                    lines.append(f"*{attribution}*\n")
                if section.contradiction_flag:
                    lines.append(f"**Contradiction:** {section.contradiction_flag}\n")

        if cross_refs:
            lines.append("## Cross-References\n")
            for ref in cross_refs:
                target_page = await db.get(WikiPage, ref.target_page_id)
                if target_page:
                    lines.append(f"- -> {ref.ref_type.value}: [[{target_page.title}]]")

        page.content = "\n".join(lines)
        await db.flush()

    # ------------------------------------------------------------------
    # Compile Job Management
    # ------------------------------------------------------------------

    async def _get_or_create_job(
        self, db: AsyncSession, document_id: str
    ) -> WikiCompileJob:
        """Get existing compile job or create a new one."""
        stmt = select(WikiCompileJob).where(
            WikiCompileJob.document_id == document_id
        )
        result = await db.execute(stmt)
        job = result.scalars().first()
        if job:
            return job

        job = WikiCompileJob(document_id=document_id, status=CompileStatus.PENDING)
        db.add(job)
        await db.flush()
        return job

    async def _fetch_chunks(
        self, db: AsyncSession, document_id: str
    ) -> list[Chunk]:
        """Fetch chunks for a document."""
        stmt = (
            select(Chunk)
            .where(Chunk.document_id == document_id)
            .order_by(Chunk.chunk_index)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Utility Methods
    # ------------------------------------------------------------------

    def _slugify(self, title: str) -> str:
        """Convert a title to a URL-safe slug."""
        slug = title.lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        slug = re.sub(r"-+", "-", slug)
        slug = slug.strip("-")
        return slug

    def _resolve_source_priority(self, source_path: str) -> int:
        """Look up source priority from the source path."""
        for key, priority in self._source_priorities.items():
            if key in source_path:
                return priority
        return 0

    # ------------------------------------------------------------------
    # LLM Communication
    # ------------------------------------------------------------------

    async def _call_llm(self, prompt: str) -> str:
        """Call Ollama LLM API, falling back to the configured fallback URL on failure."""
        result = await self._call_llm_endpoint(
            self._llm_url, self._llm_model, prompt, primary=True
        )
        if result is not None:
            return result

        if self._fallback_llm_url:
            logger.warning(
                f"Primary LLM unreachable, trying fallback at {self._fallback_llm_url} "
                f"(model={self._fallback_llm_model})"
            )
            result = await self._call_llm_endpoint(
                self._fallback_llm_url, self._fallback_llm_model, prompt, primary=False
            )
            if result is not None:
                return result

        raise RuntimeError(
            f"All LLM endpoints failed (primary={self._llm_url}"
            + (f", fallback={self._fallback_llm_url}" if self._fallback_llm_url else "")
            + ")"
        )

    async def _call_llm_endpoint(
        self, url: str, model: str, prompt: str, *, primary: bool
    ) -> str | None:
        """Attempt a single LLM endpoint. Returns the response string or None on failure."""
        label = "primary" if primary else "fallback"
        try:
            logger.debug(f"LLM request ({label}): url={url} model={model}")
            async with httpx.AsyncClient(timeout=180.0) as client:
                response = await client.post(
                    f"{url}/api/generate",
                    json={
                        "model": model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": self._temperature,
                            "num_predict": self._max_tokens,
                        },
                    },
                )
                response.raise_for_status()
                data = response.json()
                text = data.get("response", "").strip()
                logger.debug(f"LLM response ({label}): {len(text)} chars")
                return text
        except httpx.ConnectError:
            logger.warning(f"Cannot connect to {label} LLM at {url}")
            return None
        except httpx.HTTPStatusError as e:
            logger.warning(f"{label} LLM returned HTTP {e.response.status_code} from {url}")
            return None
        except Exception as e:
            logger.error(f"{label} LLM call failed unexpectedly: {e}")
            return None

    # ------------------------------------------------------------------
    # Response Parsing
    # ------------------------------------------------------------------

    def _parse_sections_response(self, response: str) -> list[dict]:
        """Parse LLM sections response."""
        try:
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if not match:
                return []
            data = json.loads(match.group())
            return data.get("sections", [])
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Failed to parse sections response: {e}")
            return []

    def _parse_updates_response(self, response: str) -> list[dict]:
        """Parse LLM updates response."""
        try:
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if not match:
                return []
            data = json.loads(match.group())
            return data.get("updates", [])
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Failed to parse updates response: {e}")
            return []

    def _parse_contradiction_response(
        self, response: str
    ) -> ContradictionResult | None:
        """Parse LLM contradiction detection response."""
        if response.strip().lower() in ("none", '"none"', "no conflict"):
            return None
        try:
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if not match:
                return None
            data = json.loads(match.group())
            return ContradictionResult(
                conflict_type=data.get("conflict_type", "factual"),
                description=data.get("description", ""),
                existing_claim=data.get("existing_claim", ""),
                new_claim=data.get("new_claim", ""),
                severity=data.get("severity", "medium"),
            )
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Failed to parse contradiction response: {e}")
            return None