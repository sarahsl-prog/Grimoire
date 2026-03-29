"""Core business logic for Grimoire."""

from grimoire.core.parser import (
    DOCLEY_AVAILABLE,
    DocumentMetadata,
    DocumentParser,
    ParsedDocument,
    ParserConfig,
    parse_document,
)

__all__ = [
    "DOCLEY_AVAILABLE",
    "DocumentMetadata",
    "DocumentParser",
    "ParsedDocument",
    "ParserConfig",
    "parse_document",
]

from grimoire.core.dedup import (
    CHUNK_SIZE,
    ConflictDetails,
    DedupResult,
    DedupStrategy,
    DeduplicationAction,
    Deduplicator,
    check_duplicate,
    compute_bytes_hash,
    compute_file_hash,
    get_file_mtime,
)
from grimoire.core.tagger import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    MAX_SAMPLE_LENGTH,
    CategoryContext,
    Tagger,
    TaggingResult,
    TagSuggestion,
)

__all__ = [
    # Tagger exports
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "MAX_SAMPLE_LENGTH",
    "CategoryContext",
    "Tagger",
    "TaggingResult",
    "TagSuggestion",
    # Dedup exports
    "CHUNK_SIZE",
    "ConflictDetails",
    "DedupResult",
    "DedupStrategy",
    "DeduplicationAction",
    "Deduplicator",
    "check_duplicate",
    "compute_bytes_hash",
    "compute_file_hash",
    "get_file_mtime",
]
