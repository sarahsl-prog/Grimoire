"""Security-domain retriever with post-fusion re-ranking.

Phase 7 wraps :class:`grimoire.search.hybrid.HybridSearch` with
domain-specific re-ranking: query-intent classification drives a
source-type boost matrix, severity gets a multiplicative weight, and
content age applies an exponential recency decay.

The retriever is **compositional**: it delegates the heavy lifting
(vector + FTS + cross-encoder rerank) to the wrapped ``HybridSearch``
instance and only applies security-domain post-processing to the
merged results.

Design notes:

* ``_classify_query`` uses simple regex first; no LLM required on the
  hot path.
* All boost factors come from ``settings.security`` so operators can
  tune weights without code changes.
* Recency decay is an exponential half-life model: if
  ``recency_half_life_days=365``, content from one year ago scores 0.5×
  as much as today's content. ``0`` disables the decay entirely.
* The retriever always returns ``list[HybridResult]`` so downstream
  ``QueryAgent`` is unaffected — no signature changes needed.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from loguru import logger

from grimoire.search.hybrid import HybridSearch
from grimoire.strategies.base import BaseRetriever

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from grimoire.config.settings import GrimoireSettings
    from grimoire.search.hybrid import HybridResult

__all__ = ["SecurityRetriever", "QueryIntent"]

# ---------------------------------------------------------------------------
# Intent taxonomy
# ---------------------------------------------------------------------------


class QueryIntent(str, Enum):
    """Query intent labels used to drive source-type boosting.

    Inheriting from ``str`` means ``QueryIntent.CVE_LOOKUP == "cve_lookup"``
    is ``True``, so the values can be used interchangeably as dictionary
    keys in :attr:`SecurityConfig.intent_source_matrix` and as type hints
    in function signatures.
    """

    CVE_LOOKUP = "cve_lookup"
    TECHNIQUE_LOOKUP = "technique_lookup"
    IOC_LOOKUP = "ioc_lookup"
    GENERAL_SECURITY = "general_security"


# Anything that can stand in for an intent: a strict :class:`QueryIntent`
# from the classifier, or a raw string (e.g. an experimental intent injected
# by a settings override). ``_security_rerank`` falls back to
# ``general_security`` when the value is unknown.
IntentLike = Union[QueryIntent, str]


# ---------------------------------------------------------------------------
# Query classifier
# ---------------------------------------------------------------------------

# Compiled once at module load for speed.
_RE_CVE = re.compile(r"^CVE-\d{4}-\d+$", re.IGNORECASE)
_RE_TECHNIQUE = re.compile(r"^T\d{4}(?:\.\d{3})?$")


def _is_valid_ipv4(text: str) -> bool:
    """Return True if text is a valid dotted-quad IPv4 address (0-255 per octet)."""
    parts = text.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


_RE_DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"
)
_RE_MD5 = re.compile(r"^[a-fA-F0-9]{32}$")
_RE_SHA1 = re.compile(r"^[a-fA-F0-9]{40}$")
_RE_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")

# Fragments that suggest an IOC intent even without a perfect match.
_IOC_FRAGMENTS = (
    "ip:",
    "domain:",
    "hash:",
    "sha256:",
    "md5:",
    "sha1:",
    "192.168.",
    "10.0.",
    "172.16.",
    "hash ",  # "hash <value>" prefix
)


def _classify_query(query: str) -> QueryIntent:
    """Classify the user query into one of the known intent buckets.

    Regex matching takes priority; fragment scanning is a fallback.
    Empty / whitespace query falls through to ``GENERAL_SECURITY``.

    Args:
        query: Raw search query string.

    Returns:
        A :class:`QueryIntent` value.
    """
    if not query or not query.strip():
        return QueryIntent.GENERAL_SECURITY

    stripped = query.strip()

    # CVE id standalone — CVE-2024-12345
    if _RE_CVE.fullmatch(stripped):
        return QueryIntent.CVE_LOOKUP

    # MITRE technique id — T1059, T1059.001
    if _RE_TECHNIQUE.match(stripped):
        return QueryIntent.TECHNIQUE_LOOKUP

    # Perfect IOC match — IP, domain, or hash
    if _is_valid_ipv4(stripped):
        return QueryIntent.IOC_LOOKUP
    if _RE_DOMAIN.fullmatch(stripped):
        return QueryIntent.IOC_LOOKUP
    if (
        _RE_MD5.fullmatch(stripped)
        or _RE_SHA1.fullmatch(stripped)
        or _RE_SHA256.fullmatch(stripped)
    ):
        return QueryIntent.IOC_LOOKUP

    # Fragment fallback for composite queries — "CVE-2024-12345 powershell"
    lower = stripped.lower()
    if "cve-" in lower and any(f"cve-{d}" in lower for d in ("2024", "2025")):
        return QueryIntent.CVE_LOOKUP
    if any(t in lower for t in ("t1059", "t1218", "t1547", "t1021")):
        return QueryIntent.TECHNIQUE_LOOKUP
    if any(frag in lower for frag in _IOC_FRAGMENTS):
        return QueryIntent.IOC_LOOKUP

    return QueryIntent.GENERAL_SECURITY


# ---------------------------------------------------------------------------
# Recency helper
# ---------------------------------------------------------------------------


def _recency_multiplier(
    content_date: Optional[datetime],
    half_life_days: int,
    now: Optional[datetime] = None,
) -> float:
    """Return a 0..1 multiplier applying exponential recency decay.

    If ``half_life_days`` is ``0`` the function returns ``1.0`` (decay
    disabled). If ``content_date`` is ``None`` the function returns
    ``1.0`` (unknown age is treated as equally relevant).

    Args:
        content_date: When the underlying content was published/created.
        half_life_days: Days for 50 % score decay.
        now: Wall-clock reference for "now" (defaults to UTC now).
    """
    if half_life_days == 0:
        return 1.0
    if content_date is None:
        return 1.0

    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)

    # Normalise naive content_date values to UTC — ChromaDB / FTS often
    # stores date-only ISO strings (e.g. "2024-06-15") and
    # ``datetime.fromisoformat`` returns them tz-naive. Without this we
    # would raise ``TypeError: can't subtract offset-naive and offset-aware
    # datetimes`` and crash retrieval on tz-less metadata.
    if content_date.tzinfo is None:
        content_date = content_date.replace(tzinfo=timezone.utc)

    age_days = (effective_now - content_date).total_seconds() / 86400.0
    if age_days < 0:
        return 1.0  # Future-dated content gets no penalty

    return math.pow(0.5, age_days / half_life_days)


# ---------------------------------------------------------------------------
# SecurityRetriever
# ---------------------------------------------------------------------------


class SecurityRetriever(BaseRetriever):
    """Security-domain retriever with intent-aware re-ranking.

    Wraps a :class:`HybridSearch` instance and applies three re-rank
    transforms to the merged results:

    1. **Intent classification** — regex-based, no LLM needed.
    2. **Severity boost** — multiplicative weight per severity level.
    3. **Recency decay** — exponential half-life on ``content_date``.
    4. **Intent-source alignment** — source-type boost matrix per intent.

    Args:
        hybrid: The underlying hybrid search engine.
        settings: Grimoire settings (used for ``settings.security``).
    """

    def __init__(
        self,
        hybrid: HybridSearch,
        settings: GrimoireSettings,
    ) -> None:
        self._hybrid = hybrid
        self._settings = settings
        self._security = settings.security

    async def retrieve(
        self,
        db: AsyncSession,
        query: str,
        *,
        top_k: int = 10,
        filter_dict: Optional[Dict[str, Any]] = None,
    ) -> List[HybridResult]:
        """Retrieve ranked security results for ``query``.

        Args:
            db: Database session, forwarded to the wrapped hybrid search.
            query: User-provided search query string.
            top_k: Number of final results to return.
            filter_dict: Optional metadata filter dictionary, applied to
                the vector store and/or other backing search backends.

        Returns:
            List of :class:`HybridResult` ordered by descending relevance,
            after security-domain re-ranking.
        """
        intent = _classify_query(query)
        logger.debug("SecurityRetriever intent: {}", intent)

        merged = await self._hybrid.search(
            db,
            query,
            top_k=top_k * 3,  # Retrieve extra candidates before re-ranking.
            filter_dict=filter_dict,
            rerank=True,
        )

        if not merged:
            return []

        ranked = self._security_rerank(merged, intent)
        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked[:top_k]

    def _security_rerank(
        self,
        results: List[HybridResult],
        intent: IntentLike,
    ) -> List[HybridResult]:
        """Apply severity boost + recency decay + intent-source alignment.

        The transforms are multiplicative and commutative, so they can be
        applied in any order. The base score starts at whatever the hybrid
        search produced (already normalized to 0–1 range).

        Args:
            results: Results from the hybrid search (pre-rerank).
            intent: Classified :class:`QueryIntent`, or a raw string for
                callers that inject custom intents via a settings override
                — unknown values fall back to ``general_security``.

        Returns:
            Re-scored results (mutated in place and also returned).
        """
        severity_weights = self._security.severity_weights
        half_life_days = self._security.recency_half_life_days
        source_matrix = self._security.intent_source_matrix
        # QueryIntent is a ``str, Enum`` so ``intent.value`` == ``str(intent)``
        # for enum members; for raw-string callers we just use the value as-is.
        intent_key = intent.value if isinstance(intent, QueryIntent) else str(intent)
        intent_row = source_matrix.get(
            intent_key,
            source_matrix.get(QueryIntent.GENERAL_SECURITY.value, {}),
        )

        for result in results:
            base = result.score

            # 1. Severity boost.
            metadata = result.metadata or {}
            sev_str = metadata.get("severity", "unknown")
            sev_mult = severity_weights.get(sev_str, 0.0)
            base *= sev_mult

            # 2. Recency decay.
            content_date_raw = metadata.get("content_date")
            content_date: Optional[datetime] = None
            if content_date_raw:
                try:
                    if isinstance(content_date_raw, datetime):
                        content_date = content_date_raw
                    else:
                        # String from ChromaDB metadata — parse ISO-8601.
                        content_date = datetime.fromisoformat(
                            str(content_date_raw).replace("Z", "+00:00")
                        )
                except (ValueError, TypeError):
                    pass
            base *= _recency_multiplier(content_date, half_life_days)

            # 3. Intent-source alignment boost.
            source_type = metadata.get("source_type", "")
            source_mult = intent_row.get(source_type, 1.0)
            base *= source_mult

            result.score = base

        return results
