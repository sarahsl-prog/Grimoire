"""Content Generation Agent for creating derived content on-demand.

Generates summaries, flashcards, cliff notes, outlines, and extracts
from document content using an LLM, with caching and database persistence.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Dict, List, Optional

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.core.cache import Cache
from grimoire.db.models import (
    Chunk,
    ContentType,
    GeneratedContent,
)


# =============================================================================
# Data Models
# =============================================================================


class GenerationRequest(BaseModel):
    """Request for content generation.

    Attributes:
        document_ids: IDs of documents to generate content from.
        content_type: Type of content to generate.
        query: Optional query for extract-type generation.
        style: Optional style hint (e.g., "brief", "detailed").
        count: Number of items to generate (for flashcards).
    """

    document_ids: List[str]
    content_type: ContentType
    query: Optional[str] = None
    style: Optional[str] = None
    count: int = 10


class GenerationResult(BaseModel):
    """Result of a content generation operation.

    Attributes:
        content: Generated content text.
        content_type: Type of content generated.
        document_ids: Source document IDs.
        model_used: LLM model that generated the content.
        cached: Whether the result was served from cache.
        generation_id: ID of the stored GeneratedContent record.
        duration_ms: Processing time in milliseconds.
    """

    model_config = ConfigDict(extra="allow")

    content: str = ""
    content_type: str = ""
    document_ids: List[str] = Field(default_factory=list)
    model_used: str = ""
    cached: bool = False
    generation_id: Optional[str] = None
    duration_ms: int = 0


# =============================================================================
# Prompt Templates
# =============================================================================

_PROMPTS: Dict[ContentType, str] = {
    ContentType.SUMMARY: (
        "Write a {style} summary of the following document content. "
        "Capture the key ideas, main arguments, and conclusions.\n\n"
        "Document content:\n{content}\n\n"
        "Summary:"
    ),
    ContentType.FLASH_CARD: (
        "Generate {count} study flashcards from the following document content. "
        "Format each flashcard as:\n"
        "Q: [question]\n"
        "A: [answer]\n\n"
        "Make questions specific and answers concise.\n\n"
        "Document content:\n{content}\n\n"
        "Flashcards:"
    ),
    ContentType.CLIFF_NOTES: (
        "Create cliff notes (bullet-point summary) of the following document. "
        "Focus on the most important facts, concepts, and takeaways.\n\n"
        "Document content:\n{content}\n\n"
        "Cliff Notes:"
    ),
    ContentType.OUTLINE: (
        "Create a hierarchical outline of the following document content. "
        "Use numbered sections and subsections to show the structure.\n\n"
        "Document content:\n{content}\n\n"
        "Outline:"
    ),
    ContentType.EXTRACT: (
        "Extract specific information from the following document to answer "
        "this question: {query}\n\n"
        "Document content:\n{content}\n\n"
        "Extracted information:"
    ),
}

# Maximum content length to send to LLM (in characters)
_MAX_CONTENT_LENGTH = 8000


# =============================================================================
# Content Generation Agent
# =============================================================================


class ContentGenerationAgent:
    """Generates derived content from documents using an LLM.

    Supports summaries, flashcards, cliff notes, outlines, and extracts.
    Generated content is cached and persisted to the database.

    Args:
        llm_url: Base URL for Ollama API.
        llm_model: Ollama model name.
        cache: Optional cache for generation results.
        temperature: LLM sampling temperature.
        max_tokens: Maximum tokens in LLM response.

    Example:
        ```python
        agent = ContentGenerationAgent(
            llm_url="http://localhost:11434",
            llm_model="llama3:8b",
        )
        result = await agent.generate_summary(db, ["doc-id-1"])
        print(result.content)
        ```
    """

    def __init__(
        self,
        llm_url: str = "http://localhost:11434",
        llm_model: str = "llama3:8b",
        cache: Optional[Cache] = None,
        temperature: float = 0.5,
        max_tokens: int = 4096,
    ) -> None:
        self._llm_url = llm_url.rstrip("/")
        self._llm_model = llm_model
        self._cache = cache
        self._temperature = temperature
        self._max_tokens = max_tokens

        logger.debug(f"ContentGenerationAgent initialized (model={llm_model})")

    # -------------------------------------------------------------------------
    # Public convenience methods
    # -------------------------------------------------------------------------

    async def generate_summary(
        self,
        db: AsyncSession,
        document_ids: List[str],
        *,
        style: str = "concise",
    ) -> GenerationResult:
        """Generate a summary of one or more documents.

        Args:
            db: Database session.
            document_ids: Document IDs to summarize.
            style: Summary style ("concise", "detailed", "brief").

        Returns:
            GenerationResult with the summary.
        """
        request = GenerationRequest(
            document_ids=document_ids,
            content_type=ContentType.SUMMARY,
            style=style,
        )
        return await self.generate(db, request)

    async def generate_flash_cards(
        self,
        db: AsyncSession,
        document_ids: List[str],
        *,
        count: int = 10,
    ) -> GenerationResult:
        """Generate study flashcards from documents.

        Args:
            db: Database session.
            document_ids: Document IDs for flashcard generation.
            count: Number of flashcards to generate.

        Returns:
            GenerationResult with flashcards.
        """
        request = GenerationRequest(
            document_ids=document_ids,
            content_type=ContentType.FLASH_CARD,
            count=count,
        )
        return await self.generate(db, request)

    async def generate_cliff_notes(
        self,
        db: AsyncSession,
        document_ids: List[str],
    ) -> GenerationResult:
        """Generate cliff notes (bullet-point summary) from documents.

        Args:
            db: Database session.
            document_ids: Document IDs.

        Returns:
            GenerationResult with cliff notes.
        """
        request = GenerationRequest(
            document_ids=document_ids,
            content_type=ContentType.CLIFF_NOTES,
        )
        return await self.generate(db, request)

    async def generate_outline(
        self,
        db: AsyncSession,
        document_ids: List[str],
    ) -> GenerationResult:
        """Generate a hierarchical outline from documents.

        Args:
            db: Database session.
            document_ids: Document IDs.

        Returns:
            GenerationResult with outline.
        """
        request = GenerationRequest(
            document_ids=document_ids,
            content_type=ContentType.OUTLINE,
        )
        return await self.generate(db, request)

    async def generate_extract(
        self,
        db: AsyncSession,
        document_ids: List[str],
        query: str,
    ) -> GenerationResult:
        """Extract specific information from documents.

        Args:
            db: Database session.
            document_ids: Document IDs to extract from.
            query: What to extract.

        Returns:
            GenerationResult with extracted information.
        """
        request = GenerationRequest(
            document_ids=document_ids,
            content_type=ContentType.EXTRACT,
            query=query,
        )
        return await self.generate(db, request)

    # -------------------------------------------------------------------------
    # Core generation method
    # -------------------------------------------------------------------------

    async def generate(
        self,
        db: AsyncSession,
        request: GenerationRequest,
    ) -> GenerationResult:
        """Generate content based on a request.

        Args:
            db: Database session.
            request: Generation request with parameters.

        Returns:
            GenerationResult with generated content.
        """
        start_time = time.monotonic()

        # Check cache
        cache_key = self._cache_key(request)
        if self._cache:
            cached = await self._check_cache(cache_key)
            if cached:
                cached.duration_ms = self._elapsed_ms(start_time)
                cached.cached = True
                return cached

        # Check DB for existing generated content
        existing = await self._check_existing(db, request)
        if existing:
            result = GenerationResult(
                content=existing.content,
                content_type=existing.content_type.value,
                document_ids=request.document_ids,
                model_used=existing.model_used,
                cached=True,
                generation_id=existing.id,
                duration_ms=self._elapsed_ms(start_time),
            )
            return result

        # Fetch document content
        content = await self._fetch_document_content(db, request.document_ids)
        if not content:
            return GenerationResult(
                content="No document content found for the specified IDs.",
                content_type=request.content_type.value,
                document_ids=request.document_ids,
                duration_ms=self._elapsed_ms(start_time),
            )

        # Build prompt
        prompt = self._build_prompt(request, content)

        # Generate with LLM
        generated_text = await self._call_llm(prompt)

        # Store in database
        generation_id = await self._store_generated_content(
            db, request, generated_text,
        )

        result = GenerationResult(
            content=generated_text,
            content_type=request.content_type.value,
            document_ids=request.document_ids,
            model_used=self._llm_model,
            generation_id=generation_id,
            duration_ms=self._elapsed_ms(start_time),
        )

        # Store in cache
        if self._cache:
            await self._store_cache(cache_key, result)

        logger.info(
            f"Generated {request.content_type.value} for "
            f"{len(request.document_ids)} document(s) ({result.duration_ms}ms)"
        )
        return result

    # -------------------------------------------------------------------------
    # Internal methods
    # -------------------------------------------------------------------------

    async def _fetch_document_content(
        self, db: AsyncSession, document_ids: List[str],
    ) -> str:
        """Fetch and combine document chunk content.

        Args:
            db: Database session.
            document_ids: Document IDs to fetch.

        Returns:
            Combined text content, truncated to max length.
        """
        stmt = (
            select(Chunk.content)
            .where(Chunk.document_id.in_(document_ids))
            .order_by(Chunk.document_id, Chunk.chunk_index)
        )
        result = await db.execute(stmt)
        chunks = result.scalars().all()

        combined = "\n\n".join(chunks)

        # Truncate to max length
        if len(combined) > _MAX_CONTENT_LENGTH:
            combined = combined[:_MAX_CONTENT_LENGTH] + "\n\n[Content truncated...]"

        return combined

    def _build_prompt(
        self, request: GenerationRequest, content: str,
    ) -> str:
        """Build an LLM prompt from the request and content.

        Args:
            request: Generation request.
            content: Document content.

        Returns:
            Formatted prompt string.
        """
        template = _PROMPTS.get(request.content_type, _PROMPTS[ContentType.SUMMARY])

        return template.format(
            content=content,
            style=request.style or "concise",
            count=request.count,
            query=request.query or "",
        )

    async def _call_llm(self, prompt: str) -> str:
        """Call the LLM to generate content.

        Args:
            prompt: Full prompt to send.

        Returns:
            Generated text.
        """
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                response = await client.post(
                    f"{self._llm_url}/api/generate",
                    json={
                        "model": self._llm_model,
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
                return data.get("response", "").strip()

        except httpx.ConnectError:
            logger.error(f"Cannot connect to LLM at {self._llm_url}")
            return "Error: LLM service unavailable. Please check Ollama is running."
        except Exception as e:
            logger.error(f"LLM generation failed: {e}")
            return f"Error generating content: {e}"

    async def _store_generated_content(
        self,
        db: AsyncSession,
        request: GenerationRequest,
        content: str,
    ) -> Optional[str]:
        """Store generated content in the database.

        Stores one record per document ID in the request.

        Args:
            db: Database session.
            request: Original generation request.
            content: Generated content text.

        Returns:
            ID of the first generated content record.
        """
        first_id: Optional[str] = None

        for doc_id in request.document_ids:
            is_err = content.startswith("Error:")
            record = GeneratedContent(
                document_id=doc_id,
                content_type=request.content_type,
                content=content,
                model_used=self._llm_model,
                generation_params={
                    "temperature": self._temperature,
                    "max_tokens": self._max_tokens,
                    "style": request.style,
                    "count": request.count,
                    "query": request.query,
                },
                cache_hit=False,
                is_error=is_err,
            )
            db.add(record)
            if first_id is None:
                await db.flush()
                first_id = record.id

        await db.flush()
        return first_id

    async def _check_existing(
        self,
        db: AsyncSession,
        request: GenerationRequest,
    ) -> Optional[GeneratedContent]:
        """Check if content has already been generated.

        Args:
            db: Database session.
            request: Generation request.

        Returns:
            Existing GeneratedContent if found, None otherwise.
        """
        if len(request.document_ids) != 1:
            # Only check single-document requests for existing content
            return None

        stmt = (
            select(GeneratedContent)
            .where(
                GeneratedContent.document_id == request.document_ids[0],
                GeneratedContent.content_type == request.content_type,
            )
            .order_by(GeneratedContent.created_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    # -------------------------------------------------------------------------
    # Caching
    # -------------------------------------------------------------------------

    def _cache_key(self, request: GenerationRequest) -> str:
        """Generate a cache key for a generation request.

        Args:
            request: Generation request.

        Returns:
            SHA-256 hash key.
        """
        key_data = json.dumps(
            {
                "doc_ids": sorted(request.document_ids),
                "type": request.content_type.value,
                "style": request.style,
                "count": request.count,
                "query": request.query,
            },
            sort_keys=True,
        )
        return f"gen:{hashlib.sha256(key_data.encode()).hexdigest()}"

    async def _check_cache(self, key: str) -> Optional[GenerationResult]:
        """Check cache for existing result."""
        if not self._cache:
            return None
        try:
            data = await self._cache.get(key)
            if data:
                logger.debug(f"Generation cache hit: {key[:20]}...")
                return GenerationResult(**data)
        except Exception as e:
            logger.warning(f"Generation cache check failed: {e}")
        return None

    async def _store_cache(self, key: str, result: GenerationResult) -> None:
        """Store result in cache."""
        if not self._cache or result.content.startswith("Error:"):
            return
        try:
            await self._cache.set(
                key, result.model_dump(),
                ttl=2592000,  # 30 days
            )
        except Exception as e:
            logger.warning(f"Generation cache store failed: {e}")

    @staticmethod
    def _elapsed_ms(start_time: float) -> int:
        """Calculate elapsed time in milliseconds."""
        return int((time.monotonic() - start_time) * 1000)
