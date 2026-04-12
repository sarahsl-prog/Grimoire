"""Query Agent for agentic RAG question answering.

Provides an intelligent query pipeline that:
1. Embeds the user query
2. Performs hybrid search (vector + FTS)
3. Reranks results
4. Assembles context with citations
5. Generates an answer using an LLM
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.core.cache import Cache
from grimoire.db.models import Document
from grimoire.search.hybrid import HybridResult, HybridSearch


# =============================================================================
# Data Models
# =============================================================================


class Citation(BaseModel):
    """A citation referencing a source document and chunk.

    Attributes:
        document_id: UUID of the source document.
        document_title: Title of the source document.
        chunk_id: UUID of the specific chunk.
        chunk_index: Position of the chunk in the document.
        content_snippet: Short excerpt from the cited content.
        relevance_score: How relevant this citation is to the answer.
    """

    document_id: str
    document_title: Optional[str] = None
    chunk_id: str
    chunk_index: Optional[int] = None
    content_snippet: str = ""
    relevance_score: float = 0.0


class QueryResult(BaseModel):
    """Result of a query operation.

    Attributes:
        query: Original user query.
        answer: Generated answer text.
        citations: Source citations for the answer.
        model_used: LLM model that generated the answer.
        search_results_count: Number of search results found.
        cached: Whether the result was served from cache.
        duration_ms: Total query processing time.
    """

    model_config = ConfigDict(extra="allow")

    query: str
    answer: str = ""
    citations: List[Citation] = Field(default_factory=list)
    model_used: str = ""
    search_results_count: int = 0
    cached: bool = False
    llm_error: bool = False
    duration_ms: int = 0


class SearchOnlyResult(BaseModel):
    """Result of a search-only operation (no LLM generation).

    Attributes:
        query: Original search query.
        results: Search results with scores.
        total_results: Number of results found.
        duration_ms: Search processing time.
    """

    query: str
    results: List[Dict[str, Any]] = Field(default_factory=list)
    total_results: int = 0
    duration_ms: int = 0


# =============================================================================
# Query Agent
# =============================================================================


class QueryAgent:
    """Agentic RAG query pipeline.

    Coordinates hybrid search, context assembly, and LLM answer
    generation with citations and caching.

    Args:
        hybrid_search: HybridSearch instance.
        llm_url: Base URL for Ollama API.
        llm_model: Ollama model name.
        cache: Optional cache for query results.
        temperature: LLM sampling temperature.
        max_tokens: Maximum tokens in LLM response.
        max_context_chunks: Maximum chunks to include in LLM context.

    Example:
        ```python
        agent = QueryAgent(
            hybrid_search=hybrid_search,
            llm_url="http://localhost:11434",
            llm_model="llama3:8b",
        )
        result = await agent.query(db, "What is machine learning?")
        print(result.answer)
        for citation in result.citations:
            print(f"  Source: {citation.document_title}")
        ```
    """

    def __init__(
        self,
        hybrid_search: HybridSearch,
        llm_url: str = "http://localhost:11434",
        llm_model: str = "llama3:8b",
        cache: Optional[Cache] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        max_context_chunks: int = 5,
    ) -> None:
        self._hybrid_search = hybrid_search
        self._llm_url = llm_url.rstrip("/")
        self._llm_model = llm_model
        self._cache = cache
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_context_chunks = max_context_chunks

        logger.debug(
            f"QueryAgent initialized (model={llm_model}, "
            f"max_context_chunks={max_context_chunks})"
        )

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def query(
        self,
        db: AsyncSession,
        query: str,
        *,
        top_k: int = 5,
        filter_dict: Optional[Dict[str, Any]] = None,
        use_cache: bool = True,
    ) -> QueryResult:
        """Execute a full RAG query: search, assemble context, generate answer.

        Args:
            db: Database session.
            query: User's question.
            top_k: Number of search results to use for context.
            filter_dict: Optional metadata filters.
            use_cache: Whether to check/store cache.

        Returns:
            QueryResult with answer and citations.
        """
        start_time = time.monotonic()

        if not query or not query.strip():
            return QueryResult(query=query, answer="Please provide a question.")

        # Check cache
        if use_cache and self._cache:
            cached = await self._check_cache(query, filter_dict)
            if cached:
                cached.duration_ms = self._elapsed_ms(start_time)
                cached.cached = True
                return cached

        # Step 1: Hybrid search
        search_results = await self._hybrid_search.search(
            db, query, top_k=top_k, filter_dict=filter_dict,
        )

        if not search_results:
            result = QueryResult(
                query=query,
                answer="I couldn't find any relevant information to answer your question.",
                search_results_count=0,
                duration_ms=self._elapsed_ms(start_time),
            )
            return result

        # Step 2: Build citations
        citations = self._build_citations(search_results)

        # Step 3: Assemble context
        context = self._assemble_context(search_results)

        # Step 4: Generate answer
        answer, is_error = await self._generate_answer(query, context)

        result = QueryResult(
            query=query,
            answer=answer,
            citations=citations,
            model_used=self._llm_model,
            search_results_count=len(search_results),
            llm_error=is_error,
            duration_ms=self._elapsed_ms(start_time),
        )

        # Store in cache
        if use_cache and self._cache:
            await self._store_cache(query, filter_dict, result)

        return result

    async def search(
        self,
        db: AsyncSession,
        query: str,
        *,
        top_k: int = 10,
        filter_dict: Optional[Dict[str, Any]] = None,
    ) -> SearchOnlyResult:
        """Search without generating an answer.

        Args:
            db: Database session.
            query: Search query.
            top_k: Number of results.
            filter_dict: Optional metadata filters.

        Returns:
            SearchOnlyResult with raw search results.
        """
        start_time = time.monotonic()

        search_results = await self._hybrid_search.search(
            db, query, top_k=top_k, filter_dict=filter_dict,
        )

        results = [
            {
                "chunk_id": r.chunk_id,
                "document_id": r.document_id,
                "content": r.content,
                "score": r.score,
                "document_title": r.document_title,
            }
            for r in search_results
        ]

        return SearchOnlyResult(
            query=query,
            results=results,
            total_results=len(results),
            duration_ms=self._elapsed_ms(start_time),
        )

    async def get_document_details(
        self, db: AsyncSession, document_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get full details for a document.

        Args:
            db: Database session.
            document_id: Document UUID.

        Returns:
            Document details dict, or None if not found.
        """
        stmt = select(Document).where(Document.id == document_id)
        result = await db.execute(stmt)
        doc = result.scalar_one_or_none()

        if not doc:
            return None

        return {
            "id": doc.id,
            "title": doc.title,
            "source_path": doc.source_path,
            "file_type": doc.file_type.value,
            "processing_status": doc.processing_status.value,
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
            "chunk_count": len(doc.chunks) if doc.chunks else 0,
            "tags": [
                {
                    "category": t.category.name if t.category else None,
                    "confidence": t.confidence,
                }
                for t in (doc.tags or [])
            ],
        }

    # -------------------------------------------------------------------------
    # Context Assembly
    # -------------------------------------------------------------------------

    def _build_citations(
        self, results: List[HybridResult],
    ) -> List[Citation]:
        """Build citation objects from search results.

        Args:
            results: Search results to cite.

        Returns:
            List of Citation objects.
        """
        citations: List[Citation] = []
        for r in results[:self._max_context_chunks]:
            snippet = r.content[:200] + "..." if len(r.content) > 200 else r.content
            metadata = r.metadata or {}
            citations.append(
                Citation(
                    document_id=r.document_id,
                    document_title=r.document_title,
                    chunk_id=r.chunk_id,
                    chunk_index=metadata.get("chunk_index"),
                    content_snippet=snippet,
                    relevance_score=r.score,
                )
            )
        return citations

    def _assemble_context(self, results: List[HybridResult]) -> str:
        """Assemble search results into an LLM context string.

        Args:
            results: Search results to include.

        Returns:
            Formatted context string.
        """
        chunks = results[:self._max_context_chunks]
        parts: List[str] = []

        for i, result in enumerate(chunks, 1):
            title = result.document_title or "Unknown"
            parts.append(
                f"[Source {i}: {title}]\n{result.content}"
            )

        return "\n\n---\n\n".join(parts)

    # -------------------------------------------------------------------------
    # LLM Generation
    # -------------------------------------------------------------------------

    _SYSTEM_PROMPT = (
        "You are a helpful research assistant. Answer the user's question "
        "based on the provided context. Cite your sources by referencing "
        "[Source N] when using information from the context. If the context "
        "doesn't contain enough information, say so clearly. Be concise "
        "and accurate."
    )

    async def _generate_answer(self, query: str, context: str) -> tuple[str, bool]:
        """Generate an answer using the LLM.

        Args:
            query: User's question.
            context: Assembled context from search results.

        Returns:
            Tuple of (generated answer, is_error flag).
        """
        prompt = (
            f"Context:\n{context}\n\n"
            f"Question: {query}\n\n"
            f"Answer based on the context above, citing sources:"
        )

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{self._llm_url}/api/generate",
                    json={
                        "model": self._llm_model,
                        "prompt": prompt,
                        "system": self._SYSTEM_PROMPT,
                        "stream": False,
                        "options": {
                            "temperature": self._temperature,
                            "num_predict": self._max_tokens,
                        },
                    },
                )
                response.raise_for_status()
                data = response.json()
                return data.get("response", "").strip(), False

        except httpx.ConnectError:
            logger.error(f"Cannot connect to LLM at {self._llm_url}")
            return (
                "Unable to generate an answer (LLM unavailable). "
                "Here are the relevant sources found:\n\n" + context,
                True,
            )
        except Exception as e:
            logger.error(f"LLM generation failed: {e}")
            return (
                f"Unable to generate an answer: {e}\n\n"
                "Here are the relevant sources found:\n\n" + context,
                True,
            )

    # -------------------------------------------------------------------------
    # Caching
    # -------------------------------------------------------------------------

    def _cache_key(
        self, query: str, filter_dict: Optional[Dict[str, Any]],
    ) -> str:
        """Generate a cache key for a query.

        Args:
            query: User query.
            filter_dict: Optional filters.

        Returns:
            SHA-256 hash key.
        """
        key_data = json.dumps(
            {"query": query.lower().strip(), "filters": filter_dict},
            sort_keys=True,
        )
        return hashlib.sha256(key_data.encode()).hexdigest()

    async def _check_cache(
        self, query: str, filter_dict: Optional[Dict[str, Any]],
    ) -> Optional[QueryResult]:
        """Check if a query result is cached.

        Args:
            query: User query.
            filter_dict: Optional filters.

        Returns:
            Cached QueryResult if found, None otherwise.
        """
        if not self._cache:
            return None

        try:
            key = self._cache_key(query, filter_dict)
            data = await self._cache.get(key)
            if data:
                logger.debug(f"Cache hit for query: {query[:50]}...")
                return QueryResult(**data)
        except Exception as e:
            logger.warning(f"Cache check failed: {e}")

        return None

    async def _store_cache(
        self,
        query: str,
        filter_dict: Optional[Dict[str, Any]],
        result: QueryResult,
    ) -> None:
        """Store a query result in cache.

        Args:
            query: User query.
            filter_dict: Optional filters.
            result: Result to cache.
        """
        if not self._cache or result.llm_error:
            return

        try:
            key = self._cache_key(query, filter_dict)
            await self._cache.set(
                key, result.model_dump(), ttl=3600,  # 1 hour TTL
            )
            logger.debug(f"Cached result for query: {query[:50]}...")
        except Exception as e:
            logger.warning(f"Cache store failed: {e}")

    @staticmethod
    def _elapsed_ms(start_time: float) -> int:
        """Calculate elapsed time in milliseconds."""
        return int((time.monotonic() - start_time) * 1000)
