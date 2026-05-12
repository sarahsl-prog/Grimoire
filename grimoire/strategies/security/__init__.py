"""Security-domain strategy package.

Phase 1 added the deterministic source-type detector
(:mod:`grimoire.strategies.security.corpus`). Phase 2 adds the
:class:`SecurityMetadata` schema (:mod:`grimoire.strategies.security.metadata`)
plus the matching ``Document`` columns and ChromaDB metadata writer hook.
Phase 7 adds :class:`SecurityRetriever` for intent-aware re-ranking.

Public re-exports for the chunker, retriever, extractor, and parsers are
loaded lazily via :pep:`562` ``__getattr__`` to avoid a circular import:
``grimoire.db.models`` imports the security enums from
:mod:`grimoire.strategies.security.metadata`, and eager imports here would
otherwise pull :mod:`grimoire.core` (via the chunker) → ``grimoire.core.dedup``
→ ``grimoire.db.models`` before ``models`` finishes defining its names.

The metadata enums (``Severity``, ``TLPLevel``, ``SecurityMetadata``) and the
source-type detector are imported eagerly because they have no dependency
back into ``grimoire.core`` or ``grimoire.db``.

Callers can still import from the package root::

    from grimoire.strategies.security import (
        SecurityChunker,
        SecurityMetadata,
        SecurityMetadataExtractor,
        SecurityRetriever,
        Severity,
        SourceType,
        TLPLevel,
        detect_source_type,
    )
"""

from typing import TYPE_CHECKING, Any

from grimoire.strategies.security.corpus import SourceType, detect_source_type
from grimoire.strategies.security.metadata import (
    SecurityMetadata,
    Severity,
    TLPLevel,
)

if TYPE_CHECKING:
    from grimoire.strategies.security.chunker import SecurityChunker
    from grimoire.strategies.security.extractor import SecurityMetadataExtractor
    from grimoire.strategies.security.parsers import (
        parse_mitre,
        parse_nvd_json,
        parse_sigma,
        sigma_level_to_severity,
    )
    from grimoire.strategies.security.parsers.nvd import severity_from_cvss_score
    from grimoire.strategies.security.retriever import (
        QueryIntent,
        SecurityRetriever,
    )

__all__ = [
    "QueryIntent",
    "SecurityChunker",
    "SecurityMetadata",
    "SecurityMetadataExtractor",
    "SecurityRetriever",
    "Severity",
    "SourceType",
    "TLPLevel",
    "detect_source_type",
    "parse_mitre",
    "parse_nvd_json",
    "parse_sigma",
    "severity_from_cvss_score",
    "sigma_level_to_severity",
]

_LAZY_TARGETS: dict[str, str] = {
    "SecurityChunker": "grimoire.strategies.security.chunker",
    "SecurityMetadataExtractor": "grimoire.strategies.security.extractor",
    "SecurityRetriever": "grimoire.strategies.security.retriever",
    "QueryIntent": "grimoire.strategies.security.retriever",
    "parse_mitre": "grimoire.strategies.security.parsers",
    "parse_nvd_json": "grimoire.strategies.security.parsers",
    "parse_sigma": "grimoire.strategies.security.parsers",
    "sigma_level_to_severity": "grimoire.strategies.security.parsers",
    "severity_from_cvss_score": "grimoire.strategies.security.parsers.nvd",
}


def __getattr__(name: str) -> Any:
    target = _LAZY_TARGETS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
