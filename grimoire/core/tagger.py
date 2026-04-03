"""LLM-based auto-tagging system for hierarchical categorization.

This module provides automatic document categorization using Ollama LLM.
It supports hierarchical categories, confidence scoring, and configurable
thresholds for tag assignment.

Example:
    >>> from grimoire.core.tagger import Tagger, TagSuggestion
    >>> from grimoire.config import get_settings
    >>> tagger = Tagger(get_settings())
    >>> suggestions = await tagger.suggest_tags(document_text, categories)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx
from loguru import logger
from pydantic import BaseModel, Field, field_validator

from grimoire.db.models import Category, Document, DocumentTag, TaggedBy

if TYPE_CHECKING:
    from grimoire.config.settings import GrimoireSettings

# ============================================================================
# Constants
# ============================================================================

# Default confidence threshold for auto-tagging
DEFAULT_CONFIDENCE_THRESHOLD: Final[float] = 0.7

# Maximum sample length to send to LLM
MAX_SAMPLE_LENGTH: Final[int] = 4000

# Default prompt template for categorization
DEFAULT_CATEGORIZATION_PROMPT: Final[
    str
] = """Categorize this document into one or more categories from the provided list.

Return your response in JSON format with the following structure:
{{
  "suggestions": [
    {{"category": "Category Name", "confidence": 0.95, "reasoning": "Brief explanation"}},
    {{"category": "Another Category", "confidence": 0.82, "reasoning": "Brief explanation"}}
  ]
}}

Available Categories:
{categories}

Document Sample:
---
{document_sample}
---

Instructions:
- Select 0-5 most relevant categories based on document content
- Confidence must be between 0.0 and 1.0
- Higher confidence = more certain the category applies
- Use confidence {threshold}+ for clear matches, lower for tentative
- Consider parent and child categories independently
- Return empty suggestions array if no categories fit"""

SAMPLE_CATEGORIZATION_PROMPT: Final[
    str
] = """Categorize this document sample into relevant categories.

Categories:
{categories}

Document:
{document_sample}

Return category names and confidence scores (0.0-1.0) in this JSON format:
{{"suggestions": [{{"category": "Name", "confidence": 0.9, "reasoning": "why"}}]}}"""

# ============================================================================
# Pydantic Models
# ============================================================================


class TagSuggestion(BaseModel):
    """A single tag suggestion with confidence and reasoning.

    Attributes:
        category: The category name or path (e.g., "Research" or "Research/AI")
        confidence: Confidence score between 0.0 and 1.0
        reasoning: Brief explanation of why this category applies
        category_id: Optional UUID of the category (set after DB lookup)
    """

    category: str = Field(..., description="Category name or hierarchical path")
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence score (0.0-1.0)"
    )
    reasoning: str = Field(default="", description="Brief explanation")
    category_id: str | None = Field(
        default=None, description="UUID of matched category"
    )

    @field_validator("category")
    @classmethod
    def validate_category_not_empty(cls, v: str) -> str:
        """Ensure category name is not empty."""
        if not v or not v.strip():
            raise ValueError("Category name cannot be empty")
        return v.strip()

    def __hash__(self) -> int:
        """Make hashable for deduplication."""
        return hash(self.category)

    def __eq__(self, other: object) -> bool:
        """Equality based on category name."""
        if not isinstance(other, TagSuggestion):
            return NotImplemented
        return self.category == other.category


class TaggingResult(BaseModel):
    """Result of a tagging operation.

    Attributes:
        document_id: UUID of the document being tagged
        suggestions: List of tag suggestions
        applied_tags: Tags that passed threshold and were applied
        threshold: Confidence threshold used
        model_used: LLM model used for tagging
    """

    document_id: str | None = Field(default=None, description="UUID of the document")
    suggestions: list[TagSuggestion] = Field(
        default_factory=list, description="All suggestions from LLM"
    )
    applied_tags: list[TagSuggestion] = Field(
        default_factory=list, description="Tags that passed threshold"
    )
    threshold: float = Field(default=DEFAULT_CONFIDENCE_THRESHOLD, ge=0.0, le=1.0)
    model_used: str = Field(default="", description="LLM model used")
    cached: bool = Field(default=False, description="Whether result was cached")


class LLMResponse(BaseModel):
    """Structure for LLM categorization response."""

    suggestions: list[TagSuggestion] = Field(default_factory=list)


# ============================================================================
# Dataclass for Category Context
# ============================================================================


@dataclass
class CategoryContext:
    """Category context for LLM prompt.

    Holds both the hierarchical path and the category object for
    matching responses back to database records.
    """

    path: str
    category: Category
    description: str | None = None

    @property
    def display_path(self) -> str:
        """Get display-friendly path."""
        return self.path

    def to_prompt_line(self) -> str:
        """Format for LLM prompt."""
        desc = f" - {self.description}" if self.description else ""
        return f"  - {self.path}{desc}"


# ============================================================================
# Tagger Class
# ============================================================================


class Tagger:
    """LLM-based auto-tagging system.

    Uses Ollama LLM to suggest categories for documents based on content.
    Supports hierarchical categories, confidence scoring, and caching.

    Example:
        >>> tagger = Tagger(settings)
        >>> result = await tagger.suggest_tags(
        ...     document_sample="Machine learning paper...",
        ...     categories=[cat1, cat2, cat3],
        ...     document_id="doc-uuid"
        ... )
        >>> print(result.applied_tags)
    """

    def __init__(self, settings: GrimoireSettings) -> None:
        """Initialize the tagger with configuration.

        Args:
            settings: Grimoire configuration settings
        """
        self.settings = settings
        self.llm_config = settings.llm
        self.processing_config = settings.processing
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client for Ollama API.

        Returns:
            httpx.AsyncClient: Client with configured timeout
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.llm_config.timeout,
                headers={"Content-Type": "application/json"},
            )
        return self._client

    async def _close_client(self) -> None:
        """Close HTTP client connection."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _format_categories(
        self, categories: list[Category], include_hierarchy: bool = True
    ) -> list[CategoryContext]:
        """Format categories for LLM prompt.

        Builds hierarchical paths (e.g., "Research/AI/ML") and returns
        context objects for each category.

        Args:
            categories: List of category database objects
            include_hierarchy: Whether to include full paths

        Returns:
            List of CategoryContext objects
        """
        contexts: list[CategoryContext] = []

        # Build parent lookup for path construction
        categories_by_id: dict[str, Category] = {
            c.id: c for c in categories if c.id is not None
        }

        def build_path(cat: Category) -> str:
            """Build hierarchical path for a category."""
            if not include_hierarchy or cat.parent_id is None:
                return cat.name
            parent = categories_by_id.get(cat.parent_id)
            if parent:
                parent_path = build_path(parent)
                return f"{parent_path}/{cat.name}"
            return cat.name

        for cat in categories:
            path = build_path(cat)
            contexts.append(
                CategoryContext(path=path, category=cat, description=cat.description)
            )

        # Sort by path for consistent prompt format
        contexts.sort(key=lambda x: x.path)
        return contexts

    def _prepare_sample(self, document_sample: str) -> str:
        """Prepare document sample for LLM prompt.

        Truncates if needed and sanitizes the content.

        Args:
            document_sample: Raw document text

        Returns:
            Sanitized and possibly truncated sample
        """
        if not document_sample:
            return ""

        # Clean whitespace
        sample = " ".join(document_sample.split())

        # Truncate if needed
        if len(sample) > MAX_SAMPLE_LENGTH:
            sample = sample[:MAX_SAMPLE_LENGTH] + "..."

        return sample

    def _build_prompt(
        self,
        document_sample: str,
        category_contexts: list[CategoryContext],
        threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> str:
        """Build the LLM prompt for categorization.

        Args:
            document_sample: Prepared document sample
            category_contexts: Formatted category contexts
            threshold: Confidence threshold to mention in prompt

        Returns:
            Formatted prompt string
        """
        categories_text = "\n".join(ctx.to_prompt_line() for ctx in category_contexts)

        return DEFAULT_CATEGORIZATION_PROMPT.format(
            categories=categories_text,
            document_sample=document_sample,
            threshold=threshold,
        )

    def _parse_llm_response(self, response_text: str) -> list[TagSuggestion]:
        """Parse LLM response into tag suggestions.

        Handles various response formats and validates structure.

        Args:
            response_text: Raw response from LLM

        Returns:
            List of tag suggestions

        Raises:
            ValueError: If response cannot be parsed
        """
        if not response_text or not response_text.strip():
            logger.warning("Empty LLM response")
            return []

        # Try to extract JSON from response (handles markdown code blocks)
        json_text = response_text.strip()

        # Remove markdown code block if present
        if "```json" in json_text:
            json_match = re.search(
                r"```json\s*(.*?)\s*```", json_text, re.DOTALL | re.IGNORECASE
            )
            if json_match:
                json_text = json_match.group(1).strip()
        elif "```" in json_text:
            json_match = re.search(
                r"```\s*(.*?)\s*```", json_text, re.DOTALL | re.IGNORECASE
            )
            if json_match:
                json_text = json_match.group(1).strip()

        try:
            data = json.loads(json_text)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM response as JSON: {e}")
            # Try to find JSON-like structure
            json_pattern = re.search(r"\{.*\}", response_text, re.DOTALL)
            if json_pattern:
                try:
                    data = json.loads(json_pattern.group())
                except json.JSONDecodeError:
                    return []
            else:
                return []

        # Handle different response structures
        suggestions: list[TagSuggestion] = []

        if isinstance(data, dict):
            if "suggestions" in data and isinstance(data["suggestions"], list):
                raw_suggestions = data["suggestions"]
            elif "categories" in data and isinstance(data["categories"], list):
                raw_suggestions = data["categories"]
            else:
                # Try to interpret dict as single suggestion
                raw_suggestions = [data]
        elif isinstance(data, list):
            raw_suggestions = data
        else:
            return []

        for raw in raw_suggestions:
            if not isinstance(raw, dict):
                continue

            try:
                # Extract category name
                category = raw.get("category", raw.get("name", ""))
                if not category:
                    continue

                # Extract confidence with type-safe conversion
                raw_confidence = raw.get("confidence")
                if raw_confidence is None:
                    raw_confidence = raw.get("score")
                if raw_confidence is None:
                    raw_confidence = DEFAULT_CONFIDENCE_THRESHOLD
                confidence = max(0.0, min(1.0, float(raw_confidence)))

                # Extract reasoning (optional)
                reasoning = raw.get("reasoning", raw.get("reason", ""))

                suggestion = TagSuggestion(
                    category=str(category).strip(),
                    confidence=confidence,
                    reasoning=str(reasoning)[:200] if reasoning else "",  # Limit length
                )
                suggestions.append(suggestion)
            except (ValueError, TypeError) as e:
                logger.debug(f"Skipping invalid suggestion: {raw}, error: {e}")
                continue

        return suggestions

    async def _call_ollama(self, prompt: str) -> str:
        """Call Ollama API with prompt.

        Args:
            prompt: Prompt text to send

        Returns:
            Raw LLM response text

        Raises:
            httpx.HTTPError: If API call fails
            ValueError: If response is invalid
        """
        client = await self._get_client()

        url = f"{self.llm_config.url.rstrip('/')}/api/generate"
        payload = {
            "model": self.llm_config.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.llm_config.temperature,
                "num_predict": self.llm_config.max_tokens,
            },
        }

        logger.debug(f"Calling Ollama API at {url} with model {self.llm_config.model}")

        response = await client.post(url, json=payload)
        response.raise_for_status()

        result = response.json()

        if "response" not in result:
            raise ValueError(f"Unexpected Ollama response format: {result.keys()}")

        return str(result["response"])

    def _match_suggestions_to_categories(
        self,
        suggestions: list[TagSuggestion],
        category_contexts: list[CategoryContext],
    ) -> list[TagSuggestion]:
        """Match LLM category names to database category IDs.

        Uses exact path matching, then name matching with fuzzy fallback.

        Args:
            suggestions: Raw suggestions from LLM
            category_contexts: Available categories with paths

        Returns:
            Suggestions with category_id populated where matched
        """
        # Build lookup maps
        path_to_id: dict[str, str] = {
            ctx.path.lower(): ctx.category.id for ctx in category_contexts
        }
        name_to_id: dict[str, str] = {}
        for ctx in category_contexts:
            name_lower = ctx.category.name.lower()
            if name_lower not in name_to_id:
                name_to_id[name_lower] = ctx.category.id

        matched: list[TagSuggestion] = []

        for suggestion in suggestions:
            cat_name_lower = suggestion.category.lower()

            # Try exact path match first
            category_id = path_to_id.get(cat_name_lower)

            # Fallback to name match
            if category_id is None:
                # Extract last component from path-like strings
                if "/" in cat_name_lower:
                    simple_name = cat_name_lower.split("/")[-1].strip()
                else:
                    simple_name = cat_name_lower
                category_id = name_to_id.get(simple_name)

            if category_id:
                suggestion.category_id = category_id
                matched.append(suggestion)
            else:
                logger.debug(
                    f"Could not match category '{suggestion.category}' to any "
                    f"known category"
                )

        return matched

    async def suggest_tags(
        self,
        document_sample: str,
        categories: list[Category],
        document_id: str | None = None,
        threshold: float | None = None,
    ) -> TaggingResult:
        """Suggest tags for a document based on content sample.

        Uses Ollama LLM to categorize the document against provided categories.

        Args:
            document_sample: Sample text from the document (e.g., first chunks)
            categories: Available categories to choose from (hierarchical OK)
            document_id: Optional document UUID for caching
            threshold: Confidence threshold (uses config default if not set)

        Returns:
            TaggingResult with all suggestions and applied tags

        Raises:
            ValueError: If inputs are invalid
            httpx.HTTPError: If LLM API call fails
        """
        # Validate inputs
        if not document_sample or not document_sample.strip():
            raise ValueError("Document sample cannot be empty")

        if not categories:
            logger.warning("No categories provided for tagging")
            return TaggingResult(
                document_id=document_id,
                suggestions=[],
                applied_tags=[],
                threshold=threshold or self.processing_config.auto_tag_threshold,
                model_used=self.llm_config.model,
            )

        # Use configured threshold if not specified
        if threshold is None:
            threshold = self.processing_config.auto_tag_threshold

        # Prepare inputs
        prepared_sample = self._prepare_sample(document_sample)
        category_contexts = self._format_categories(categories)

        logger.info(
            f"Requesting tags for document {document_id or 'unknown'} "
            f"against {len(categories)} categories with threshold {threshold}"
        )

        # Build and send prompt
        prompt = self._build_prompt(prepared_sample, category_contexts, threshold)

        try:
            response_text = await self._call_ollama(prompt)
        except httpx.HTTPStatusError as e:
            logger.error(f"Ollama API HTTP error: {e.response.status_code} - {e}")
            raise
        except httpx.RequestError as e:
            logger.error(f"Ollama API request error: {e}")
            raise

        # Parse response
        raw_suggestions = self._parse_llm_response(response_text)
        logger.debug(f"Received {len(raw_suggestions)} raw suggestions from LLM")

        # Match to category IDs
        suggestions = self._match_suggestions_to_categories(
            raw_suggestions, category_contexts
        )

        # Filter by threshold
        applied_tags = [s for s in suggestions if s.confidence >= threshold]

        logger.info(
            f"Tagging complete: {len(suggestions)} matched, "
            f"{len(applied_tags)} applied (threshold: {threshold})"
        )

        return TaggingResult(
            document_id=document_id,
            suggestions=suggestions,
            applied_tags=applied_tags,
            threshold=threshold,
            model_used=self.llm_config.model,
        )

    async def apply_tags(
        self,
        db_session: Any,
        document: Document,
        suggestions: list[TagSuggestion],
    ) -> list[DocumentTag]:
        """Apply suggested tags to a document in the database.

        Creates DocumentTag records with confidence and tagged_by=llm.

        Args:
            db_session: Async SQLAlchemy session
            document: Document to tag
            suggestions: Tag suggestions to apply

        Returns:
            List of created DocumentTag objects
        """
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession

        if not isinstance(db_session, AsyncSession):
            raise TypeError("db_session must be an AsyncSession")

        created_tags: list[DocumentTag] = []

        # Get existing tags for this document in one query
        existing_tags_query = select(DocumentTag).where(
            DocumentTag.document_id == document.id
        )
        existing_result = await db_session.execute(existing_tags_query)
        existing_tags_by_category: dict[str, DocumentTag] = {
            t.category_id: t for t in existing_result.scalars().all() if t.category_id
        }

        for suggestion in suggestions:
            if not suggestion.category_id:
                logger.warning(f"Skipping tag '{suggestion.category}' - no category_id")
                continue

            existing = existing_tags_by_category.get(suggestion.category_id)

            if existing:
                # Update confidence if this is higher
                if suggestion.confidence > existing.confidence:
                    existing.confidence = suggestion.confidence
                    existing.tagged_by = TaggedBy.LLM
                    logger.debug(f"Updated tag confidence for {suggestion.category}")
            else:
                # Create new tag
                tag = DocumentTag(
                    document_id=document.id,
                    category_id=suggestion.category_id,
                    confidence=suggestion.confidence,
                    tagged_by=TaggedBy.LLM,
                )
                db_session.add(tag)
                created_tags.append(tag)
                logger.debug(f"Created new tag: {suggestion.category}")

        if created_tags:
            await db_session.flush()

        return created_tags

    async def tag_document(
        self,
        db_session: Any,
        document: Document,
        categories: list[Category],
        sample: str | None = None,
        auto_apply: bool = True,
    ) -> TaggingResult:
        """Full tagging pipeline: suggest and optionally apply tags.

        Convenience method that combines suggest_tags and apply_tags.

        Args:
            db_session: Async SQLAlchemy session
            document: Document to tag
            categories: Available categories
            sample: Document sample (uses document chunks if not provided)
            auto_apply: Whether to automatically apply suggested tags

        Returns:
            TaggingResult with suggestions and applied tags
        """
        from grimoire.db.models import Chunk

        # Build sample if not provided
        if sample is None:
            if document.chunks:
                # Use first few chunks
                sample_parts: list[str] = []
                for chunk in document.chunks[:3]:
                    sample_parts.append(chunk.content)
                    if sum(len(p) for p in sample_parts) > MAX_SAMPLE_LENGTH:
                        break
                sample = "\n\n".join(sample_parts)
            else:
                sample = document.title or ""

        # Get suggestions
        result = await self.suggest_tags(
            document_sample=sample,
            categories=categories,
            document_id=document.id,
        )

        # Apply tags if requested
        if auto_apply and result.applied_tags:
            await self.apply_tags(db_session, document, result.applied_tags)

        return result
