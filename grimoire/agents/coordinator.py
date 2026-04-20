"""Coordinator Agent — top-level router for all Grimoire agents.

Routes natural-language or structured requests to the appropriate
specialised agent (Ingestion, Query, ContentGeneration, or Watcher)
and returns a unified result.

Intent is classified in two steps:
  1. Fast keyword matching (no network call).
  2. Optional LLM-assisted classification when keywords are ambiguous,
     if an LLM URL is provided and ``use_llm_fallback=True``.

Example::

    coordinator = CoordinatorAgent(
        ingestion_agent=ingestion_agent,
        query_agent=query_agent,
        content_gen_agent=content_gen_agent,
        watcher_agent=watcher_agent,
    )
    async with get_db_context() as db:
        result = await coordinator.execute(
            db,
            "What are the key findings about transformer architectures?",
        )
        print(result.intent, result.result.answer)
"""

from __future__ import annotations

import re
import time
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.agents.content_gen import ContentGenerationAgent, GenerationResult
from grimoire.agents.ingestion import BatchIngestionResult, IngestionAgent, IngestionResult
from grimoire.agents.query import QueryAgent, QueryResult, SearchOnlyResult
from grimoire.agents.watcher import WatcherAgent
from grimoire.db.models import ContentType


# =============================================================================
# Intent Classification
# =============================================================================


class IntentType(str, Enum):
    """Possible intents the coordinator can route to.

    Attributes:
        INGEST: Ingest one or more files / directories.
        QUERY: Answer a question using RAG.
        SEARCH: Search without LLM answer generation.
        GENERATE: Generate derived content (summary, flashcards, etc.).
        WATCH: Start monitoring a directory for new files.
        UNWATCH: Stop monitoring a directory.
        WIKI: Compile, list, or manage wiki pages.
        UNKNOWN: Could not determine intent; defaults to QUERY.
    """

    INGEST = "ingest"
    QUERY = "query"
    SEARCH = "search"
    GENERATE = "generate"
    WATCH = "watch"
    UNWATCH = "unwatch"
    WIKI = "wiki"
    UNKNOWN = "unknown"


# Keyword sets used for fast intent classification (checked in order)
_INTENT_KEYWORDS: list[tuple[IntentType, frozenset[str]]] = [
    (
        IntentType.UNWATCH,
        frozenset({"unwatch", "stop watching", "stop monitor", "remove watch"}),
    ),
    (
        IntentType.WIKI,
        frozenset({"wiki", "compile", "wikipage"}),
    ),
    (
        IntentType.WATCH,
        frozenset({"watch", "monitor", "start watching", "observe"}),
    ),
    (
        IntentType.INGEST,
        frozenset({
            "ingest", "scan", "import", "index", "process files",
            "add files", "add documents", "load files", "load documents",
            "parse", "embed",
        }),
    ),
    (
        IntentType.GENERATE,
        frozenset({
            "generate", "create a summary", "write a summary", "make a summary",
            "summarize", "summarise", "flashcard", "flash card",
            "cliff note", "cliffnote", "outline", "key points",
        }),
    ),
    (
        IntentType.SEARCH,
        frozenset({
            "search for", "find documents", "list documents",
            "show documents", "show me documents",
        }),
    ),
]

# Question words that strongly signal a QUERY intent
_QUERY_STARTERS = frozenset({
    "what", "why", "how", "who", "when", "where", "which", "explain",
    "describe", "tell me", "can you", "could you", "is there", "are there",
    "does", "do you know",
})

# GeneratedContent type keyword mapping
_CONTENT_TYPE_KEYWORDS: list[tuple[ContentType, frozenset[str]]] = [
    (ContentType.FLASH_CARD, frozenset({"flashcard", "flash card", "cards", "quiz"})),
    (ContentType.CLIFF_NOTES, frozenset({"cliff note", "cliffnote", "cliff-note", "bullets", "key points"})),
    (ContentType.OUTLINE, frozenset({"outline", "structure", "table of contents", "toc"})),
    (ContentType.EXTRACT, frozenset({"extract", "find information", "pull out"})),
    (ContentType.SUMMARY, frozenset({"summary", "summarize", "summarise", "overview", "brief", "abstract"})),
]


def classify_intent(text: str) -> tuple[IntentType, float]:
    """Classify the intent of a user input string using keyword matching.

    Args:
        text: Raw user input (natural language or command).

    Returns:
        Tuple of (IntentType, confidence) where confidence is 0.0–1.0.
        Keyword matches yield 0.9; question-word matches yield 0.7;
        fallback to UNKNOWN yields 0.3.
    """
    lowered = text.lower().strip()

    # Check compound keyword phrases first (order matters)
    for intent, keywords in _INTENT_KEYWORDS:
        for kw in keywords:
            if kw in lowered:
                logger.debug(f"Intent classified as {intent.value!r} via keyword {kw!r}")
                return intent, 0.9

    # Question-word heuristic → QUERY
    first_word = lowered.split()[0] if lowered.split() else ""
    if first_word in _QUERY_STARTERS:
        logger.debug(f"Intent classified as QUERY via question starter {first_word!r}")
        return IntentType.QUERY, 0.7

    # Check for question marks
    if "?" in text:
        logger.debug("Intent classified as QUERY via question mark")
        return IntentType.QUERY, 0.6

    logger.debug("Intent could not be determined; defaulting to UNKNOWN")
    return IntentType.UNKNOWN, 0.3


def extract_content_type(text: str) -> ContentType:
    """Infer the GeneratedContent type from text.

    Falls back to SUMMARY when no specific type is detected.

    Args:
        text: User input text.

    Returns:
        ContentType enum value.
    """
    lowered = text.lower()
    for content_type, keywords in _CONTENT_TYPE_KEYWORDS:
        for kw in keywords:
            if kw in lowered:
                return content_type
    return ContentType.SUMMARY


# =============================================================================
# Data Models
# =============================================================================


class CoordinatorContext(BaseModel):
    """Pre-parsed parameters that bypass natural-language extraction.

    Provide any fields you already know; the coordinator will use them
    directly instead of trying to infer them from the input text.

    Attributes:
        intent: Force a specific intent (skip classification).
        file_path: File or directory path for INGEST.
        recursive: Recursive flag for INGEST/WATCH.
        document_ids: Document IDs for GENERATE.
        content_type: Content type for GENERATE.
        generation_style: Style hint for GENERATE (e.g. "concise").
        flashcard_count: Number of flashcards for GENERATE.
        top_k: Result count for QUERY/SEARCH.
        filter_dict: Metadata filters for QUERY/SEARCH.
        watch_id: Watch ID for UNWATCH.
        watch_backend: Storage backend for WATCH.
        use_cache: Whether to use the query/generation cache.
        wiki_action: Action for WIKI intent (compile, list, show, export, status).
    """

    intent: Optional[IntentType] = None
    file_path: Optional[str] = None
    recursive: bool = True
    document_ids: List[str] = Field(default_factory=list)
    content_type: Optional[ContentType] = None
    generation_style: str = "concise"
    flashcard_count: int = 10
    top_k: int = 5
    filter_dict: Optional[Dict[str, Any]] = None
    watch_id: Optional[str] = None
    watch_backend: str = "local"
    use_cache: bool = True
    wiki_action: Optional[str] = None  # compile, list, show, export, status


class CoordinatorResult(BaseModel):
    """Unified result from the coordinator.

    Attributes:
        intent: The intent that was used for routing.
        agent_used: Name of the agent that handled the request.
        result: Raw result from the delegated agent (varies by intent).
        input_text: Original user input.
        confidence: Classification confidence (0.0–1.0).
        duration_ms: Total time including classification and dispatch.
        error: Error message if the request failed.
    """

    model_config = ConfigDict(extra="allow")

    intent: IntentType
    agent_used: str
    result: Optional[Any] = None
    input_text: str
    confidence: float = 1.0
    duration_ms: int = 0
    error: Optional[str] = None


# =============================================================================
# Coordinator Agent
# =============================================================================


class CoordinatorAgent:
    """Top-level router that dispatches requests to specialised agents.

    Classifies user intent from natural language and routes to the
    correct agent.  All four agents are optional; if a request is routed
    to a missing agent, a descriptive error is returned rather than
    raising an exception, so callers can always pattern-match on
    ``result.error`` safely.

    Args:
        ingestion_agent: Agent for document ingestion.
        query_agent: Agent for RAG question-answering and search.
        content_gen_agent: Agent for content generation.
        watcher_agent: Agent for directory watching.
        llm_url: Ollama base URL for LLM-assisted classification (optional).
        llm_model: Ollama model to use for LLM-assisted classification.
        use_llm_fallback: Whether to call the LLM when keyword confidence
            is below ``llm_fallback_threshold``.
        llm_fallback_threshold: Confidence below which LLM is consulted.

    Example::

        coordinator = CoordinatorAgent(
            ingestion_agent=ingestion_agent,
            query_agent=query_agent,
            content_gen_agent=content_gen_agent,
        )
        result = await coordinator.execute(db, "summarize doc 42")
        print(result.result.content)
    """

    def __init__(
        self,
        ingestion_agent: Optional[IngestionAgent] = None,
        query_agent: Optional[QueryAgent] = None,
        content_gen_agent: Optional[ContentGenerationAgent] = None,
        watcher_agent: Optional[WatcherAgent] = None,
        wiki_agent: Optional[Any] = None,
        llm_url: str = "http://localhost:11434",
        llm_model: str = "llama3:8b",
        use_llm_fallback: bool = False,
        llm_fallback_threshold: float = 0.5,
    ) -> None:
        self._ingestion_agent = ingestion_agent
        self._query_agent = query_agent
        self._content_gen_agent = content_gen_agent
        self._watcher_agent = watcher_agent
        self._wiki_agent = wiki_agent
        self._llm_url = llm_url.rstrip("/")
        self._llm_model = llm_model
        self._use_llm_fallback = use_llm_fallback
        self._llm_fallback_threshold = llm_fallback_threshold

        agents_available = [
            name
            for name, agent in [
                ("ingestion", ingestion_agent),
                ("query", query_agent),
                ("content_gen", content_gen_agent),
                ("watcher", watcher_agent),
                ("wiki", wiki_agent),
            ]
            if agent is not None
        ]
        logger.debug(
            f"CoordinatorAgent initialized with agents: {agents_available}"
        )

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def execute(
        self,
        db: AsyncSession,
        user_input: str,
        *,
        context: Optional[CoordinatorContext] = None,
    ) -> CoordinatorResult:
        """Classify intent and dispatch to the appropriate agent.

        Args:
            db: Active database session.
            user_input: Natural language request or command string.
            context: Pre-parsed parameters that override inferred values.

        Returns:
            CoordinatorResult with the delegated agent's result and metadata.
        """
        start_time = time.monotonic()
        ctx = context or CoordinatorContext()

        if not user_input or not user_input.strip():
            return CoordinatorResult(
                intent=IntentType.UNKNOWN,
                agent_used="none",
                input_text=user_input,
                error="Input is empty.",
                duration_ms=0,
            )

        # Step 1: Classify intent
        if ctx.intent is not None:
            intent = ctx.intent
            confidence = 1.0
            logger.debug(f"Intent forced by context: {intent.value}")
        else:
            intent, confidence = classify_intent(user_input)

            # Step 2: Optional LLM fallback for low-confidence classifications
            if (
                self._use_llm_fallback
                and confidence < self._llm_fallback_threshold
            ):
                try:
                    llm_intent = await self._llm_classify(user_input)
                except Exception as exc:
                    logger.warning(f"LLM fallback classification raised: {exc}")
                    llm_intent = None
                if llm_intent is not None:
                    intent = llm_intent
                    confidence = 0.85
                    logger.debug(
                        f"LLM overrode intent to {intent.value!r} (threshold={self._llm_fallback_threshold})"
                    )

        # UNKNOWN falls back to QUERY
        if intent == IntentType.UNKNOWN:
            intent = IntentType.QUERY
            logger.debug("UNKNOWN intent → defaulting to QUERY")

        logger.info(
            f"CoordinatorAgent: routing to {intent.value!r} "
            f"(confidence={confidence:.2f}) for input: {user_input[:60]!r}"
        )

        # Step 3: Dispatch
        try:
            result, agent_name = await self._dispatch(db, intent, user_input, ctx)
        except Exception as exc:
            logger.error(f"CoordinatorAgent dispatch error: {exc}")
            return CoordinatorResult(
                intent=intent,
                agent_used="coordinator",
                input_text=user_input,
                confidence=confidence,
                error=str(exc),
                duration_ms=self._elapsed_ms(start_time),
            )

        return CoordinatorResult(
            intent=intent,
            agent_used=agent_name,
            result=result,
            input_text=user_input,
            confidence=confidence,
            duration_ms=self._elapsed_ms(start_time),
        )

    async def ingest(
        self,
        db: AsyncSession,
        path: str,
        *,
        recursive: bool = True,
        auto_tag: bool = True,
    ) -> CoordinatorResult:
        """Convenience method to ingest a file or directory directly.

        Args:
            db: Active database session.
            path: File or directory path.
            recursive: Whether to scan subdirectories.
            auto_tag: Whether to auto-tag ingested documents.

        Returns:
            CoordinatorResult wrapping an IngestionResult or BatchIngestionResult.
        """
        ctx = CoordinatorContext(
            intent=IntentType.INGEST,
            file_path=path,
            recursive=recursive,
        )
        return await self.execute(db, f"ingest {path}", context=ctx)

    async def query(
        self,
        db: AsyncSession,
        question: str,
        *,
        top_k: int = 5,
        filter_dict: Optional[Dict[str, Any]] = None,
        use_cache: bool = True,
    ) -> CoordinatorResult:
        """Convenience method for RAG question-answering.

        Args:
            db: Active database session.
            question: Natural language question.
            top_k: Number of context chunks to retrieve.
            filter_dict: Optional metadata filters.
            use_cache: Whether to use the result cache.

        Returns:
            CoordinatorResult wrapping a QueryResult.
        """
        ctx = CoordinatorContext(
            intent=IntentType.QUERY,
            top_k=top_k,
            filter_dict=filter_dict,
            use_cache=use_cache,
        )
        return await self.execute(db, question, context=ctx)

    async def generate(
        self,
        db: AsyncSession,
        document_ids: List[str],
        content_type: ContentType = ContentType.SUMMARY,
        *,
        style: str = "concise",
        count: int = 10,
        query: Optional[str] = None,
    ) -> CoordinatorResult:
        """Convenience method for content generation.

        Args:
            db: Active database session.
            document_ids: IDs of documents to generate content from.
            content_type: Type of content to generate.
            style: Style hint for summaries ("concise", "detailed").
            count: Number of items for flashcard generation.
            query: Query string for extract-type generation.

        Returns:
            CoordinatorResult wrapping a GenerationResult.
        """
        ctx = CoordinatorContext(
            intent=IntentType.GENERATE,
            document_ids=document_ids,
            content_type=content_type,
            generation_style=style,
            flashcard_count=count,
        )
        input_text = f"generate {content_type.value} for documents {document_ids}"
        if query:
            input_text = query
        return await self.execute(db, input_text, context=ctx)

    # -------------------------------------------------------------------------
    # Dispatch
    # -------------------------------------------------------------------------

    async def _dispatch(
        self,
        db: AsyncSession,
        intent: IntentType,
        user_input: str,
        ctx: CoordinatorContext,
    ) -> tuple[Any, str]:
        """Route the request to the correct agent and return (result, agent_name).

        Args:
            db: Active database session.
            intent: Classified or forced intent.
            user_input: Original user input string.
            ctx: Coordinator context with parsed parameters.

        Returns:
            Tuple of (agent result, agent name string).

        Raises:
            RuntimeError: If the required agent is not configured.
            ValueError: If required parameters cannot be inferred.
        """
        if intent == IntentType.INGEST:
            return await self._handle_ingest(db, user_input, ctx)

        if intent == IntentType.QUERY:
            return await self._handle_query(db, user_input, ctx)

        if intent == IntentType.SEARCH:
            return await self._handle_search(db, user_input, ctx)

        if intent == IntentType.GENERATE:
            return await self._handle_generate(db, user_input, ctx)

        if intent == IntentType.WATCH:
            return await self._handle_watch(user_input, ctx)

        if intent == IntentType.UNWATCH:
            return await self._handle_unwatch(ctx)

        if intent == IntentType.WIKI:
            return await self._handle_wiki(db, user_input, ctx)

        # Should never reach here given UNKNOWN → QUERY above
        raise ValueError(f"Unhandled intent: {intent}")

    # -------------------------------------------------------------------------
    # Per-intent handlers
    # -------------------------------------------------------------------------

    async def _handle_ingest(
        self,
        db: AsyncSession,
        user_input: str,
        ctx: CoordinatorContext,
    ) -> tuple[IngestionResult | BatchIngestionResult, str]:
        if self._ingestion_agent is None:
            raise RuntimeError("IngestionAgent is not configured in this coordinator.")

        path = ctx.file_path or self._extract_path(user_input)
        if not path:
            raise ValueError(
                "Cannot ingest: no file path found in input or context. "
                "Provide context.file_path or include a path in the input."
            )

        import os

        if os.path.isdir(path):
            result: IngestionResult | BatchIngestionResult = (
                await self._ingestion_agent.ingest_directory(
                    db, path, recursive=ctx.recursive
                )
            )
        else:
            result = await self._ingestion_agent.ingest_file(db, path)

        return result, "IngestionAgent"

    async def _handle_query(
        self,
        db: AsyncSession,
        user_input: str,
        ctx: CoordinatorContext,
    ) -> tuple[QueryResult, str]:
        if self._query_agent is None:
            raise RuntimeError("QueryAgent is not configured in this coordinator.")

        result = await self._query_agent.query(
            db,
            user_input,
            top_k=ctx.top_k,
            filter_dict=ctx.filter_dict,
            use_cache=ctx.use_cache,
        )
        return result, "QueryAgent"

    async def _handle_search(
        self,
        db: AsyncSession,
        user_input: str,
        ctx: CoordinatorContext,
    ) -> tuple[SearchOnlyResult, str]:
        if self._query_agent is None:
            raise RuntimeError("QueryAgent is not configured in this coordinator.")

        result = await self._query_agent.search(
            db,
            user_input,
            top_k=ctx.top_k,
            filter_dict=ctx.filter_dict,
        )
        return result, "QueryAgent (search)"

    async def _handle_generate(
        self,
        db: AsyncSession,
        user_input: str,
        ctx: CoordinatorContext,
    ) -> tuple[GenerationResult, str]:
        if self._content_gen_agent is None:
            raise RuntimeError(
                "ContentGenerationAgent is not configured in this coordinator."
            )

        if not ctx.document_ids:
            raise ValueError(
                "Cannot generate content: no document_ids provided in context. "
                "Set context.document_ids to the target document IDs."
            )

        content_type = ctx.content_type or extract_content_type(user_input)

        if content_type == ContentType.FLASH_CARD:
            result = await self._content_gen_agent.generate_flash_cards(
                db, ctx.document_ids, count=ctx.flashcard_count
            )
        elif content_type == ContentType.CLIFF_NOTES:
            result = await self._content_gen_agent.generate_cliff_notes(
                db, ctx.document_ids
            )
        elif content_type == ContentType.OUTLINE:
            result = await self._content_gen_agent.generate_outline(
                db, ctx.document_ids
            )
        elif content_type == ContentType.EXTRACT:
            result = await self._content_gen_agent.generate_extract(
                db, ctx.document_ids, user_input
            )
        else:
            result = await self._content_gen_agent.generate_summary(
                db, ctx.document_ids, style=ctx.generation_style
            )

        return result, "ContentGenerationAgent"

    async def _handle_watch(
        self,
        user_input: str,
        ctx: CoordinatorContext,
    ) -> tuple[dict[str, Any], str]:
        if self._watcher_agent is None:
            raise RuntimeError("WatcherAgent is not configured in this coordinator.")

        path = ctx.file_path or self._extract_path(user_input)
        if not path:
            raise ValueError(
                "Cannot start watch: no path found in input or context. "
                "Provide context.file_path or include a path in the input."
            )

        watch_id = await self._watcher_agent.watch(
            path, backend=ctx.watch_backend
        )
        return {"watch_id": watch_id, "path": path, "status": "watching"}, "WatcherAgent"

    async def _handle_unwatch(
        self, ctx: CoordinatorContext,
    ) -> tuple[dict[str, Any], str]:
        if self._watcher_agent is None:
            raise RuntimeError("WatcherAgent is not configured in this coordinator.")

        if not ctx.watch_id:
            raise ValueError(
                "Cannot unwatch: no watch_id in context. "
                "Set context.watch_id to the ID returned by watch()."
            )

        stopped = await self._watcher_agent.unwatch(ctx.watch_id)
        return {
            "watch_id": ctx.watch_id,
            "stopped": stopped,
        }, "WatcherAgent"

    async def _handle_wiki(
        self, db: AsyncSession, user_input: str, ctx: CoordinatorContext,
    ) -> tuple[Any, str]:
        """Handle wiki-related requests."""
        if self._wiki_agent is None:
            raise RuntimeError("WikiAgent is not configured in this coordinator.")

        action = ctx.wiki_action or "compile"
        if action == "compile":
            results = await self._wiki_agent.compile_pending(db)
            return results, "WikiAgent"
        elif action == "list":
            from grimoire.db.models import WikiPage
            from sqlalchemy import select
            stmt = select(WikiPage).order_by(WikiPage.title)
            result = await db.execute(stmt)
            pages = result.scalars().all()
            return pages, "WikiAgent"
        else:
            results = await self._wiki_agent.compile_pending(db)
            return results, "WikiAgent"

    # -------------------------------------------------------------------------
    # LLM-assisted classification (optional)
    # -------------------------------------------------------------------------

    _LLM_CLASSIFY_PROMPT = (
        "Classify the intent of the following user input into exactly one of these categories: "
        "ingest, query, search, generate, watch, unwatch, wiki.\n\n"
        "Definitions:\n"
        "  ingest  - the user wants to add/process/scan files or directories\n"
        "  query   - the user is asking a question expecting an AI-generated answer\n"
        "  search  - the user wants to find/list documents without an AI answer\n"
        "  generate - the user wants to create a summary, flashcards, outline, or cliff notes\n"
        "  watch   - the user wants to monitor a directory for new files\n"
        "  unwatch - the user wants to stop monitoring a directory\n"
        "  wiki    - the user wants to compile, list, or manage wiki pages\n\n"
        "Respond with only the single category word, nothing else.\n\n"
        "Input: {input}\n"
        "Category:"
    )

    async def _llm_classify(self, user_input: str) -> Optional[IntentType]:
        """Use the LLM to classify intent when keyword matching is ambiguous.

        Args:
            user_input: Raw user input text.

        Returns:
            IntentType if successfully classified, None on failure.
        """
        prompt = self._LLM_CLASSIFY_PROMPT.format(input=user_input[:500])

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{self._llm_url}/api/generate",
                    json={
                        "model": self._llm_model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.0, "num_predict": 5},
                    },
                )
                response.raise_for_status()
                raw = response.json().get("response", "").strip().lower()

            # Normalise the LLM response
            raw = re.sub(r"[^a-z]", "", raw)
            for intent in IntentType:
                if intent.value in raw:
                    logger.debug(f"LLM classified intent as {intent.value!r}")
                    return intent

        except Exception as exc:
            logger.warning(f"LLM intent classification failed: {exc}")

        return None

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _extract_path(text: str) -> Optional[str]:
        """Extract a filesystem path from free-form text.

        Looks for Unix-style absolute paths (``/…``) or Windows-style
        drive paths (``C:\\…``), as well as ``gdrive://`` and
        ``onedrive://`` scheme prefixes.

        Args:
            text: User input text.

        Returns:
            First path found, or None.
        """
        # Cloud scheme paths
        cloud_match = re.search(
            r"((?:gdrive|onedrive|rclone)://[^\s]+)", text
        )
        if cloud_match:
            return cloud_match.group(1)

        # Unix absolute path
        unix_match = re.search(r"(/[^\s]+)", text)
        if unix_match:
            return unix_match.group(1)

        # Windows absolute path
        win_match = re.search(r"([A-Za-z]:\\[^\s]+)", text)
        if win_match:
            return win_match.group(1)

        return None

    @staticmethod
    def _elapsed_ms(start_time: float) -> int:
        """Calculate elapsed milliseconds since start_time."""
        return int((time.monotonic() - start_time) * 1000)
