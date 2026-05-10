"""Security-domain strategy package.

Phase 1 added the deterministic source-type detector
(:mod:`grimoire.strategies.security.corpus`). Phase 2 adds the
:class:`SecurityMetadata` schema (:mod:`grimoire.strategies.security.metadata`)
plus the matching ``Document`` columns and ChromaDB metadata writer hook.
Parsers, the ``SecurityChunker`` dispatch, and the ``SecurityRetriever``
arrive in subsequent phases — see ``docs/plans/security_strategy_plan.md``.

Public surface re-exports the detector and metadata schema so callers can
import from the package root::

    from grimoire.strategies.security import (
        SecurityMetadata,
        Severity,
        SourceType,
        TLPLevel,
        detect_source_type,
    )
"""

from grimoire.strategies.security.chunker import SecurityChunker
from grimoire.strategies.security.corpus import SourceType, detect_source_type
from grimoire.strategies.security.metadata import (
    SecurityMetadata,
    Severity,
    TLPLevel,
)
from grimoire.strategies.security.parsers import parse_sigma, sigma_level_to_severity

__all__ = [
    "SecurityChunker",
    "SecurityMetadata",
    "Severity",
    "SourceType",
    "TLPLevel",
    "detect_source_type",
    "parse_sigma",
    "sigma_level_to_severity",
]
