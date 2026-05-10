"""Security-domain strategy package.

Phase 1 of the security strategy plan adds the deterministic source-type
detector (:mod:`grimoire.strategies.security.corpus`). Parsers, the
``SecurityChunker`` dispatch, metadata schema, and the ``SecurityRetriever``
arrive in subsequent phases — see ``docs/plans/security_strategy_plan.md``
for the full roadmap.

Public surface re-exports the detector so callers can import it from the
package root::

    from grimoire.strategies.security import SourceType, detect_source_type
"""

from grimoire.strategies.security.corpus import SourceType, detect_source_type

__all__ = ["SourceType", "detect_source_type"]
