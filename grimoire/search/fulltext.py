"""PostgreSQL Full-Text Search (FTS) implementation for Grimoire.

Provides hybrid search capability using PostgreSQL's tsvector/tsquery
with support for weighted fields, ranking, and query operators.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Union

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from grimoire.db.models import Chunk, Document


# =============================================================================
# Pydantic Models
# =============================================================================


@dataclass
class FTSQuery:
    """Full-text search query model.

    Attributes:
        query: Raw search query string
        parsed: Parsed tsquery string for PostgreSQL
        is_phrase: Whether the query is a phrase search
    """

    query: str
    parsed: str
    is_phrase: bool = False

    @classmethod
    def create(cls, query: str, operators: bool = True) -> "FTSQuery":
        """Create an FTSQuery from a raw query string.

        Args:
            query: Raw search query
            operators: Whether to support AND/OR operators

        Returns:
            FTSQuery instance with parsed query
        """
        parsed = parse_query(query, operators=operators)
        is_phrase = '"' in query
        return cls(query=query, parsed=parsed, is_phrase=is_phrase)


@dataclass
class FTSResult:
    """Full-text search result.

    Attributes:
        chunk_id: UUID of the matching chunk
        document_id: UUID of the parent document
        content: Matching chunk content
        rank: Search rank score (higher is better)
        document_title: Title of the parent document
    """

    chunk_id: str
    document_id: str
    content: str
    rank: float
    document_title: Optional[str] = None


# =============================================================================
# Query Parsing
# =============================================================================


def escape_special_chars(query: str) -> str:
    """Escape special characters in search query.

    PostgreSQL FTS has special characters that need escaping:
    ! & | ( ) : * '

    Args:
        query: Raw query string

    Returns:
        Escaped query string
    """
    # Escape backslashes first
    escaped = query.replace("\\", "\\\\")
    # Escape single quotes (doubling them for SQL)
    escaped = escaped.replace("'", "''")
    return escaped


def parse_query(query: str, operators: bool = True) -> str:
    """Parse a user query into PostgreSQL tsquery format.

    Supports:
    - Plain words: "python programming" -> "python" & "programming"
    - AND operator: "python AND django" -> "python" & "django"
    - OR operator: "python OR javascript" -> "python" | "javascript"
    - Phrases: ""hello world"" -> "hello" <-> "world" (followed by)

    Args:
        query: Raw user query
        operators: Whether to parse AND/OR operators

    Returns:
        PostgreSQL tsquery string
    """
    if not query or not query.strip():
        return ""

    # Trim whitespace and normalize
    query = query.strip()

    # Check for phrase search (wrapped in quotes)
    if query.startswith('"') and query.endswith('"') and query.count('"') == 2:
        # Remove quotes and use phrase search (<->)
        inner = query[1:-1].strip()
        escaped = escape_special_chars(inner)
        # Convert to_followed_by operator
        words = escaped.split()
        if len(words) == 1:
            return words[0]
        return " <-> ".join(words)

    if not operators:
        # Simple word search - all words ANDed together
        words = escape_special_chars(query).split()
        if len(words) == 1:
            return words[0]
        return " & ".join(words)

    # Parse with operators
    # Protect quoted phrases first
    phrases: list[str] = []

    def replace_phrase(match: re.Match[str]) -> str:
        phrases.append(match.group(1))
        return f" __PHRASE_{len(phrases) - 1}__ "

    # Replace quoted phrases with placeholders (including surrounding spaces)
    query = re.sub(r'\s*"([^"]*)"\s*', replace_phrase, query)

    # Normalize whitespace
    query = re.sub(r'\s+', ' ', query).strip()

    # Replace OR with | (case insensitive) - with spaces around
    query = re.sub(r'\s+OR\s+', ' | ', query, flags=re.IGNORECASE)

    # Replace AND with & (case insensitive) - with spaces around
    query = re.sub(r'\s+AND\s+', ' & ', query, flags=re.IGNORECASE)

    # Now split by space and rebuild - this ensures proper operator placement
    tokens = query.split()
    new_tokens: list[str] = []
    for i, token in enumerate(tokens):
        if token in ('|', '&'):
            new_tokens.append(token)
        elif token.startswith('__PHRASE_') and token.endswith('__'):
            new_tokens.append(token)
        else:
            # It's a word - escape special chars
            escaped = escape_special_chars(token)
            if i > 0 and new_tokens and new_tokens[-1] not in ('|', '&'):
                # Add implicit AND
                new_tokens.append('&')
            new_tokens.append(escaped)

    query = ' '.join(new_tokens)

    # Restore phrases with <-> operator
    for i, phrase in enumerate(phrases):
        words = phrase.split()
        if len(words) == 1:
            phrase_query = escape_special_chars(words[0])
        else:
            escaped_words = [escape_special_chars(w) for w in words]
            phrase_query = " <-> ".join(escaped_words)
        query = query.replace(f"__PHRASE_{i}__", f"({phrase_query})")

    return query.strip()


# =============================================================================
# Full-Text Search Engine
# =============================================================================


class FulltextSearch:
    """PostgreSQL Full-Text Search engine.

    Provides ranked full-text search over document chunks with support
    for weighted fields and query operators.
    """

    # Weight categories for ranking (A highest, D lowest)
    WEIGHT_TITLE = "A"  # Highest weight
    WEIGHT_CONTENT = "B"  # Standard weight

    def __init__(
        self,
        session: AsyncSession,
        language: str = "english",
        include_title_weight: bool = True,
    ) -> None:
        """Initialize the full-text search engine.

        Args:
            session: SQLAlchemy async session
            language: PostgreSQL text search configuration
            include_title_weight: Whether to include document title in ranking
        """
        self.session = session
        self.language = language
        self.include_title_weight = include_title_weight

    async def search(
        self,
        query: str,
        top_k: int = 10,
        document_ids: Optional[List[str]] = None,
    ) -> List[FTSResult]:
        """Execute a full-text search query.

        Args:
            query: Search query string
            top_k: Maximum number of results to return
            document_ids: Optional filter to specific documents

        Returns:
            List of FTSResult, ranked by relevance

        Raises:
            ValueError: If query is empty or invalid
        """
        if not query or not query.strip():
            return []

        # Parse the query
        fts_query = FTSQuery.create(query, operators=True)
        if not fts_query.parsed:
            return []

        # Build the search query
        results = await self._execute_search(
            fts_query.parsed, top_k, document_ids
        )
        return results

    async def _execute_search(
        self,
        parsed_query: str,
        top_k: int,
        document_ids: Optional[List[str]] = None,
    ) -> list[FTSResult]:
        """Execute the PostgreSQL FTS query.

        Args:
            parsed_query: Parsed PostgreSQL tsquery string
            top_k: Maximum results to return
            document_ids: Optional document filter

        Returns:
            List of ranked search results
        """
        # Build the to_tsquery expression
        tsquery_expr = func.to_tsquery(self.language, parsed_query)

        # Build tsvector for content
        content_vector = func.to_tsvector(self.language, Chunk.content)

        if self.include_title_weight:
            # Weighted search with document title
            # Title gets weight A, content gets weight B
            stmt = (
                select(
                    Chunk.id.label("chunk_id"),
                    Chunk.document_id.label("document_id"),
                    Chunk.content.label("content"),
                    Document.title.label("document_title"),
                    func.ts_rank(
                        func.setweight(
                            func.to_tsvector(self.language, Document.title),
                            self.WEIGHT_TITLE,
                        )
                        .concat(
                            func.setweight(
                                func.to_tsvector(self.language, Chunk.content),
                                self.WEIGHT_CONTENT,
                            )
                        ),
                        tsquery_expr,
                        32,  # RANK_NORMALIZATION = 32 ( divides rank by document length )
                    ).label("rank"),
                )
                .join(Document, Chunk.document_id == Document.id)
                .where(
                    func.to_tsvector(self.language, Chunk.content).bool_op("@@")(
                        tsquery_expr
                    )
                    | func.to_tsvector(self.language, Document.title).bool_op("@@")(
                        tsquery_expr
                    )
                )
            )
        else:
            # Content-only search
            stmt = (
                select(
                    Chunk.id.label("chunk_id"),
                    Chunk.document_id.label("document_id"),
                    Chunk.content.label("content"),
                    Document.title.label("document_title"),
                    func.ts_rank(
                        content_vector,
                        tsquery_expr,
                        32,
                    ).label("rank"),
                )
                .join(Document, Chunk.document_id == Document.id)
                .where(
                    func.to_tsvector(self.language, Chunk.content).bool_op("@@")(
                        tsquery_expr
                    )
                )
            )

        # Add document filter if provided
        if document_ids:
            stmt = stmt.where(Chunk.document_id.in_(document_ids))

        # Order by rank descending and limit results
        stmt = stmt.order_by(text("rank DESC")).limit(top_k)

        # Execute the query
        result = await self.session.execute(stmt)
        rows = result.all()

        # Convert to FTSResult objects
        return [
            FTSResult(
                chunk_id=str(row.chunk_id),
                document_id=str(row.document_id),
                content=row.content,
                rank=float(row.rank) if row.rank is not None else 0.0,
                document_title=row.document_title,
            )
            for row in rows
        ]

    async def search_chunks_only(
        self,
        query: str,
        top_k: int = 10,
    ) -> List[FTSResult]:
        """Search only in chunk content (no title weighting).

        Args:
            query: Search query string
            top_k: Maximum number of results

        Returns:
            List of FTSResult
        """
        if not query or not query.strip():
            return []

        fts_query = FTSQuery.create(query, operators=True)
        if not fts_query.parsed:
            return []

        tsquery_expr = func.to_tsquery(self.language, fts_query.parsed)
        content_vector = func.to_tsvector(self.language, Chunk.content)

        stmt = (
            select(
                Chunk.id.label("chunk_id"),
                Chunk.document_id.label("document_id"),
                Chunk.content.label("content"),
                Document.title.label("document_title"),
                func.ts_rank(
                    content_vector,
                    tsquery_expr,
                    32,
                ).label("rank"),
            )
            .join(Document, Chunk.document_id == Document.id)
            .where(content_vector.bool_op("@@")(tsquery_expr))
            .order_by(text("rank DESC"))
            .limit(top_k)
        )

        result = await self.session.execute(stmt)
        rows = result.all()

        return [
            FTSResult(
                chunk_id=str(row.chunk_id),
                document_id=str(row.document_id),
                content=row.content,
                rank=float(row.rank) if row.rank is not None else 0.0,
                document_title=row.document_title,
            )
            for row in rows
        ]

    async def highlight(
        self,
        chunk_id: str,
        query: str,
        max_fragments: int = 3,
        fragment_delimiter: str = " ... ",
    ) -> Optional[str]:
        """Generate highlighted snippets for a chunk.

        Args:
            chunk_id: UUID of the chunk to highlight
            query: Search query
            max_fragments: Maximum number of fragments to return
            fragment_delimiter: Delimiter between fragments

        Returns:
            Highlighted text with search terms marked
        """
        if not query or not query.strip():
            return None

        # Get the chunk content
        stmt = select(Chunk.content).where(Chunk.id == chunk_id)
        result = await self.session.execute(stmt)
        content = result.scalar_one_or_none()

        if not content:
            return None

        # Use PostgreSQL's ts_headline function
        ftq = FTSQuery.create(query, operators=True)
        if not ftq.parsed:
            return content

        tsquery_expr = func.to_tsquery(self.language, ftq.parsed)
        tsvector_expr = func.to_tsvector(self.language, Chunk.content)

        # Build ts_headline call
        headline_expr = func.ts_headline(
            self.language,
            Chunk.content,
            tsquery_expr,
            text(f"MaxFragments={max_fragments}, "
                 f"FragmentDelimiter='{fragment_delimiter}', "
                 "StartSel=<mark>, StopSel=</mark>"),
        )

        stmt = select(headline_expr).where(Chunk.id == chunk_id)
        result = await self.session.execute(stmt)
        highlighted = result.scalar_one_or_none()

        return highlighted or content


# =============================================================================
# Helper Functions (Non-Class based)
# =============================================================================


async def search_chunks(
    session: AsyncSession,
    query: str,
    top_k: int = 10,
    language: str = "english",
) -> List[FTSResult]:
    """Convenience function for chunk-only full-text search.

    Args:
        session: SQLAlchemy async session
        query: Search query string
        top_k: Maximum results to return
        language: PostgreSQL text search configuration

    Returns:
        List of ranked search results
    """
    searcher = FulltextSearch(session, language=language, include_title_weight=False)
    return await searcher.search(query, top_k=top_k)


async def search_with_title(
    session: AsyncSession,
    query: str,
    top_k: int = 10,
    language: str = "english",
) -> List[FTSResult]:
    """Convenience function for weighted title+content search.

    Args:
        session: SQLAlchemy async session
        query: Search query string
        top_k: Maximum results to return
        language: PostgreSQL text search configuration

    Returns:
        List of ranked search results
    """
    searcher = FulltextSearch(session, language=language, include_title_weight=True)
    return await searcher.search(query, top_k=top_k)
