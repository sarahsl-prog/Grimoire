"""Search components for Grimoire.

Provides full-text search capabilities using PostgreSQL FTS
and hybrid search combining lexical and semantic approaches.
"""

from grimoire.search.fulltext import (
    FTSQuery,
    FTSResult,
    FulltextSearch,
    escape_special_chars,
    parse_query,
    search_chunks,
    search_with_title,
)
from grimoire.search.hybrid import HybridSearch

__all__ = [
    "FTSQuery",
    "FTSResult",
    "FulltextSearch",
    "HybridSearch",
    "escape_special_chars",
    "parse_query",
    "search_chunks",
    "search_with_title",
]
