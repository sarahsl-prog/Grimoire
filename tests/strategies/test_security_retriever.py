"""Tests for the Phase 7 SecurityRetriever.

Covers:

* _classify_query: CVE id, MITRE technique id, IOC patterns,
  composite queries, fallback to general_security.
* _security_rerank: severity boost, recency decay, intent-source matrix.
* recency_half_life_days=0 disables decay.
* retrieve: delegates to wrapped hybrid.search, returns re-ranked list.
* Empty results from hybrid search pass through cleanly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from grimoire.search.hybrid import HybridResult
from grimoire.strategies.security.retriever import (
    QueryIntent,
    SecurityRetriever,
    _classify_query,
    _recency_multiplier,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_result(
    chunk_id: str,
    score: float = 1.0,
    *,
    severity: str = "unknown",
    source_type: str = "prose",
    content_date: Optional[datetime] = None,
    content_date_str: Optional[str] = None,
) -> HybridResult:
    """Build a HybridResult stub with security metadata."""
    metadata = {"severity": severity, "source_type": source_type}
    if content_date is not None:
        metadata["content_date"] = content_date.isoformat()
    elif content_date_str is not None:
        metadata["content_date"] = content_date_str
    return HybridResult(
        chunk_id=chunk_id,
        document_id="doc-1",
        content="test content",
        score=score,
        metadata=metadata,
    )


class _SecuritySettings:
    """Minimal mock security config."""

    def __init__(self, **overrides):
        self.severity_weights = {
            "critical": 3.0,
            "high": 2.0,
            "medium": 1.0,
            "low": 0.5,
            "info": 0.2,
            "unknown": 0.0,
        }
        self.recency_half_life_days = 365
        self.intent_source_matrix = {
            "cve_lookup": {
                "nvd_cve": 2.0,
                "sigma_rule": 1.0,
                "mitre_attack": 0.5,
                "prose": 0.2,
            },
            "technique_lookup": {
                "mitre_attack": 2.0,
                "sigma_rule": 1.0,
                "nvd_cve": 0.5,
                "prose": 0.2,
            },
            "ioc_lookup": {
                "sigma_rule": 1.5,
                "prose": 0.5,
                "nvd_cve": 0.5,
                "mitre_attack": 0.3,
            },
            "general_security": {
                "sigma_rule": 1.0,
                "nvd_cve": 1.0,
                "mitre_attack": 1.0,
                "prose": 1.0,
            },
        }
        for k, v in overrides.items():
            setattr(self, k, v)


class _MockSettings:
    def __init__(self, **security_overrides):
        self.security = _SecuritySettings(**security_overrides)


# ---------------------------------------------------------------------------
# 1. Query classifier
# ---------------------------------------------------------------------------


class TestClassifyQuery:
    @pytest.mark.parametrize(
        "query,expected",
        [
            # CVE id
            ("CVE-2024-12345", QueryIntent.CVE_LOOKUP),
            ("cve-2024-99999", QueryIntent.CVE_LOOKUP),
            ("CVE-2025-00001", QueryIntent.CVE_LOOKUP),
            # MITRE technique id
            ("T1059", QueryIntent.TECHNIQUE_LOOKUP),
            ("T1059.001", QueryIntent.TECHNIQUE_LOOKUP),
            ("t1218", QueryIntent.TECHNIQUE_LOOKUP),
            # Perfect IOC matches
            ("192.168.1.10", QueryIntent.IOC_LOOKUP),
            ("8.8.8.8", QueryIntent.IOC_LOOKUP),
            ("evil.example.com", QueryIntent.IOC_LOOKUP),
            ("d41d8cd98f00b204e9800998ecf8427e", QueryIntent.IOC_LOOKUP),
            ("da39a3ee5e6b4b0d3255bfef95601890afd80709", QueryIntent.IOC_LOOKUP),
            (
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                QueryIntent.IOC_LOOKUP,
            ),
            # Composite queries
            ("CVE-2024-12345 powershell", QueryIntent.CVE_LOOKUP),
            ("T1059.001 execution", QueryIntent.TECHNIQUE_LOOKUP),
            # IOC fragments
            ("ip: 192.168.1.1", QueryIntent.IOC_LOOKUP),
            ("hash d41d8cd98f00b204e9800998ecf8427e", QueryIntent.IOC_LOOKUP),
            ("domain: evil.com", QueryIntent.IOC_LOOKUP),
            # Fallback
            ("powershell lateral movement", QueryIntent.GENERAL_SECURITY),
            ("APT28 Cobalt Strike", QueryIntent.GENERAL_SECURITY),
            ("buffer overflow exploit", QueryIntent.GENERAL_SECURITY),
            ("", QueryIntent.GENERAL_SECURITY),
            ("   ", QueryIntent.GENERAL_SECURITY),
        ],
    )
    def test_classify(self, query: str, expected: QueryIntent) -> None:
        assert _classify_query(query) is expected

    def test_ipv4_octet_ranges(self) -> None:
        assert _classify_query("256.256.256.256") is QueryIntent.GENERAL_SECURITY
        assert _classify_query("0.0.0.0") is QueryIntent.IOC_LOOKUP  # noqa: S104
        assert _classify_query("999.999.999.999") is QueryIntent.GENERAL_SECURITY

    def test_fragment_mitigation(self) -> None:
        assert _classify_query("cve in title") is QueryIntent.GENERAL_SECURITY
        assert _classify_query("T9999 technique") is QueryIntent.GENERAL_SECURITY


# ---------------------------------------------------------------------------
# 2. Recency multiplier
# ---------------------------------------------------------------------------


class TestRecencyMultiplier:
    def test_disabled_when_half_life_zero(self) -> None:
        now = datetime(2025, 6, 1, tzinfo=timezone.utc)
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        assert _recency_multiplier(old, 0, now=now) == 1.0

    def test_null_content_date_returns_one(self) -> None:
        assert _recency_multiplier(None, 365) == 1.0

    def test_exponential_decay_half_life(self) -> None:
        now = datetime(2025, 6, 1, tzinfo=timezone.utc)
        # Exactly at half-life → 0.5
        at_half = datetime(2024, 6, 1, tzinfo=timezone.utc)
        assert abs(_recency_multiplier(at_half, 365, now=now) - 0.5) < 0.001

    def test_full_recent_content(self) -> None:
        now = datetime(2025, 6, 1, tzinfo=timezone.utc)
        today = datetime(2025, 6, 1, tzinfo=timezone.utc)
        # Very recent → close to 1.0
        mult = _recency_multiplier(today, 365, now=now)
        assert 0.9 < mult <= 1.0

    def test_old_content_decay(self) -> None:
        now = datetime(2025, 6, 1, tzinfo=timezone.utc)
        two_years = datetime(2023, 6, 1, tzinfo=timezone.utc)
        # 2 half-lives → 0.25
        mult = _recency_multiplier(two_years, 365, now=now)
        assert abs(mult - 0.25) < 0.01

    def test_future_date_no_penalty(self) -> None:
        now = datetime(2025, 6, 1, tzinfo=timezone.utc)
        future = datetime(2030, 1, 1, tzinfo=timezone.utc)
        assert _recency_multiplier(future, 365, now=now) == 1.0


# ---------------------------------------------------------------------------
# 3. _security_rerank
# ---------------------------------------------------------------------------


class TestSecurityRerank:
    def _rerank(
        self,
        results: list[HybridResult],
        settings: _MockSettings,
        intent: QueryIntent = QueryIntent.GENERAL_SECURITY,
    ) -> list[HybridResult]:
        retriever = SecurityRetriever(MagicMock(), settings)
        out = retriever._security_rerank(list(results), intent)
        out.sort(key=lambda r: r.score, reverse=True)
        return out

    def test_severity_critical_boosts(self) -> None:
        settings = _MockSettings()
        results = [
            _make_result("a", score=0.5, severity="low"),
            _make_result("b", score=0.5, severity="critical"),
        ]
        reranked = self._rerank(results, settings)
        # Both start at 0.5 score; critical gets 3.0×, low gets 0.5×
        assert reranked[0].chunk_id == "b"  # critical boosted
        assert reranked[1].chunk_id == "a"  # low demoted

    def test_recency_decay_applies(self) -> None:
        settings = _MockSettings(recency_half_life_days=365)
        results = [
            _make_result(
                "old",
                score=0.5,
                severity="high",
                content_date=datetime(2023, 6, 1, tzinfo=timezone.utc),
            ),
            _make_result(
                "new",
                score=0.5,
                severity="high",
                content_date=datetime(2025, 5, 1, tzinfo=timezone.utc),
            ),
        ]
        reranked = self._rerank(results, settings)
        # Both same severity (high=2.0×); old has recency penalty
        assert reranked[0].chunk_id == "new"
        assert reranked[1].chunk_id == "old"

    def test_severity_and_recency_combined(self) -> None:
        """critical+old vs low+new — order depends on weight magnitudes."""
        settings = _MockSettings()
        results = [
            _make_result(
                "crit_old",
                score=1.0,
                severity="critical",
                content_date=datetime(2023, 6, 1, tzinfo=timezone.utc),
            ),
            _make_result(
                "low_new",
                score=0.5,
                severity="low",
                content_date=datetime(2025, 5, 1, tzinfo=timezone.utc),
            ),
        ]
        reranked = self._rerank(results, settings)
        # crit_old: 1.0 × 3.0 × 0.25 = 0.75
        # low_new:  0.5 × 0.5 × ~1.0 = 0.25
        assert reranked[0].chunk_id == "crit_old"
        assert reranked[1].chunk_id == "low_new"

    def test_intent_source_alignment(self) -> None:
        settings = _MockSettings()
        results = [
            _make_result("cve", score=0.5, source_type="nvd_cve"),
            _make_result("sigma", score=0.5, source_type="sigma_rule"),
        ]
        reranked = self._rerank(results, settings, intent=QueryIntent.CVE_LOOKUP)
        # cve_lookup intent → nvd_cve gets 2.0×, sigma_rule gets 1.0×
        # cve: 0.5 × 0.0 × 2.0 = 0.0  (unknown severity = 0.0 weight!)
        # sigma: 0.5 × 0.0 × 1.0 = 0.0  → both 0, order preserved
        # Use known severity to make the intent matrix matter
        results = [
            _make_result("cve", score=0.5, severity="high", source_type="nvd_cve"),
            _make_result("sigma", score=0.5, severity="high", source_type="sigma_rule"),
        ]
        reranked = self._rerank(results, settings, intent=QueryIntent.CVE_LOOKUP)
        # cve: 0.5 × 2.0 × 2.0 = 2.0
        # sigma: 0.5 × 2.0 × 1.0 = 1.0
        assert reranked[0].chunk_id == "cve"
        assert reranked[1].chunk_id == "sigma"

    def test_intent_matrix_technique_lookup(self) -> None:
        settings = _MockSettings()
        results = [
            _make_result(
                "mitre", score=0.5, severity="high", source_type="mitre_attack"
            ),
            _make_result("cve", score=0.5, severity="high", source_type="nvd_cve"),
        ]
        reranked = self._rerank(results, settings, intent=QueryIntent.TECHNIQUE_LOOKUP)
        # mitre_attack: 0.5 × 2.0 × 2.0 = 2.0
        # nvd_cve: 0.5 × 2.0 × 0.5 = 0.5
        assert reranked[0].chunk_id == "mitre"
        assert reranked[1].chunk_id == "cve"

    def test_recency_disabled_when_half_life_zero(self) -> None:
        settings = _MockSettings(recency_half_life_days=0)
        results = [
            _make_result(
                "old",
                score=0.5,
                severity="high",
                content_date=datetime(2020, 1, 1, tzinfo=timezone.utc),
            ),
            _make_result(
                "new",
                score=0.5,
                severity="high",
                content_date=datetime(2025, 5, 1, tzinfo=timezone.utc),
            ),
        ]
        reranked = self._rerank(results, settings)
        # Both get full severity weight × recency=1.0 (disabled)
        assert all(r.score == 1.0 for r in reranked)

    def test_unknown_severity_gets_zero_weight(self) -> None:
        settings = _MockSettings()
        results = [
            _make_result("unknown", score=0.8, severity="unknown"),
            _make_result("high", score=0.3, severity="high"),
        ]
        reranked = self._rerank(results, settings)
        # unknown: 0.8 × 0.0 = 0.0 (pruned or last)
        # high: 0.3 × 2.0 = 0.6
        assert reranked[0].chunk_id == "high"

    def test_empty_metadata_uses_defaults(self) -> None:
        settings = _MockSettings()
        result = HybridResult(
            chunk_id="x", document_id="d", content="c", score=0.5, metadata={}
        )
        reranked = self._rerank([result], settings)
        # severity unknown → 0.0 weight, source unknown → 1.0
        assert reranked[0].score == 0.0

    def test_null_content_date_no_penalty(self) -> None:
        settings = _MockSettings()
        results = [
            _make_result(
                "with_date",
                score=0.5,
                severity="high",
                content_date_str="2020-01-01T00:00:00+00:00",
            ),
            _make_result("no_date", score=0.5, severity="high"),
        ]
        reranked = self._rerank(results, settings)
        assert reranked[0].chunk_id == "no_date"  # no recency penalty

    def test_iso_date_parsing_tolerates_z_suffix(self) -> None:
        settings = _MockSettings()
        results = [
            _make_result(
                "z", score=0.5, severity="high", content_date_str="2020-01-01T00:00:00Z"
            ),
        ]
        reranked = self._rerank(results, settings)
        # Should parse without error
        assert reranked[0].chunk_id == "z"

    def test_mutation_documents_internal_list(self) -> None:
        """The method mutates and returns the same list object."""
        settings = _MockSettings()
        results = [
            _make_result("a", score=0.5, severity="high"),
            _make_result("b", score=0.4, severity="critical"),
        ]
        original = list(results)
        retriever = SecurityRetriever(MagicMock(), settings)
        out = retriever._security_rerank(results, QueryIntent.GENERAL_SECURITY)
        assert out is results
        assert all(r is original[i] for i, r in enumerate(out))


# ---------------------------------------------------------------------------
# 4. retrieve()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRetrieve:
    async def test_delegates_to_hybrid_search(self) -> None:
        settings = _MockSettings()
        mock_hybrid = AsyncMock()
        mock_hybrid.search.return_value = [
            _make_result(
                "chunk-1", score=0.9, severity="high", source_type="sigma_rule"
            ),
            _make_result(
                "chunk-2", score=0.8, severity="critical", source_type="mitre_attack"
            ),
        ]

        retriever = SecurityRetriever(mock_hybrid, settings)
        db = MagicMock()
        results = await retriever.retrieve(db, "T1059", top_k=5)

        mock_hybrid.search.assert_awaited_once()
        call_args = mock_hybrid.search.call_args
        # query is the 2nd positional arg (after db); top_k is a keyword.
        assert call_args.args[1] == "T1059"
        assert call_args.kwargs["top_k"] == 15  # 5 × 3
        # technique_lookup intent: mitre_attack (2.0×) × critical (3.0×) × 0.8 = 4.8
        # vs sigma_rule (1.0×) × high (2.0×) × 0.9 = 1.8 → chunk-2 wins.
        assert results[0].chunk_id == "chunk-2"

    async def test_empty_results_pass_through(self) -> None:
        mock_hybrid = AsyncMock()
        mock_hybrid.search.return_value = []
        retriever = SecurityRetriever(mock_hybrid, _MockSettings())
        db = MagicMock()
        results = await retriever.retrieve(db, "nonexistent", top_k=5)
        assert results == []

    async def test_results_truncated_to_top_k(self) -> None:
        mock_hybrid = AsyncMock()
        mock_hybrid.search.return_value = [
            _make_result(f"c-{i}", score=1.0 - i * 0.1, severity="high")
            for i in range(30)
        ]
        retriever = SecurityRetriever(mock_hybrid, _MockSettings())
        db = MagicMock()
        results = await retriever.retrieve(db, "query", top_k=5)
        assert len(results) == 5
        assert results[0].score >= results[-1].score

    async def test_classify_query_controls_rerank(self) -> None:
        """Intent drives source-type boost: CVE query should prefer nvd_cve."""
        mock_hybrid = AsyncMock()
        mock_hybrid.search.return_value = [
            _make_result("sigma", score=1.0, severity="high", source_type="sigma_rule"),
            _make_result("cve", score=0.9, severity="high", source_type="nvd_cve"),
        ]
        retriever = SecurityRetriever(mock_hybrid, _MockSettings())
        db = MagicMock()

        # CVE query → nvd_cve boosted
        results = await retriever.retrieve(db, "CVE-2024-12345", top_k=5)
        assert results[0].chunk_id == "cve"

        # T1059 query → mitre_attack boosted (but none in results, sigma wins)
        results = await retriever.retrieve(db, "T1059.001", top_k=5)
        assert results[0].chunk_id == "sigma"


# ---------------------------------------------------------------------------
# 5. Settings overrides drive behaviour (regression guards)
# ---------------------------------------------------------------------------


class TestSettingsOverrides:
    """All re-rank weights are configurable; no hard-coded magic numbers."""

    def test_custom_intent_matrix_reorders_results(self) -> None:
        """Swapping the intent → source-type matrix flips the ordering."""
        # Baseline matrix favors nvd_cve for cve_lookup.
        baseline = _MockSettings()
        results_baseline = [
            _make_result("cve", score=0.5, severity="high", source_type="nvd_cve"),
            _make_result("sigma", score=0.5, severity="high", source_type="sigma_rule"),
        ]
        retriever = SecurityRetriever(MagicMock(), baseline)
        out = retriever._security_rerank(list(results_baseline), QueryIntent.CVE_LOOKUP)
        out.sort(key=lambda r: r.score, reverse=True)
        assert out[0].chunk_id == "cve"

        # Override: penalize nvd_cve, boost sigma_rule under CVE intent.
        overridden = _MockSettings(
            intent_source_matrix={
                "cve_lookup": {
                    "nvd_cve": 0.1,
                    "sigma_rule": 5.0,
                    "mitre_attack": 0.1,
                    "prose": 0.1,
                },
                "technique_lookup": {},
                "ioc_lookup": {},
                "general_security": {"sigma_rule": 1.0, "nvd_cve": 1.0},
            }
        )
        results_override = [
            _make_result("cve", score=0.5, severity="high", source_type="nvd_cve"),
            _make_result("sigma", score=0.5, severity="high", source_type="sigma_rule"),
        ]
        retriever = SecurityRetriever(MagicMock(), overridden)
        out = retriever._security_rerank(list(results_override), QueryIntent.CVE_LOOKUP)
        out.sort(key=lambda r: r.score, reverse=True)
        assert out[0].chunk_id == "sigma"

    def test_custom_severity_weights(self) -> None:
        """Severity multipliers come from settings, not hard-coded."""
        settings = _MockSettings(
            severity_weights={
                "critical": 0.1,  # invert: critical demoted
                "high": 0.5,
                "medium": 1.0,
                "low": 5.0,  # low boosted
                "info": 1.0,
                "unknown": 1.0,
            }
        )
        results = [
            _make_result("crit", score=0.5, severity="critical"),
            _make_result("low", score=0.5, severity="low"),
        ]
        retriever = SecurityRetriever(MagicMock(), settings)
        out = retriever._security_rerank(list(results), QueryIntent.GENERAL_SECURITY)
        out.sort(key=lambda r: r.score, reverse=True)
        assert out[0].chunk_id == "low"

    def test_unknown_intent_falls_back_to_general_security(self) -> None:
        """An intent missing from the matrix falls back to general_security row."""
        settings = _MockSettings()
        # Inject an unrecognized intent string — `_security_rerank` should still work.
        results = [
            _make_result("prose", score=0.5, severity="high", source_type="prose"),
            _make_result("sigma", score=0.5, severity="high", source_type="sigma_rule"),
        ]
        retriever = SecurityRetriever(MagicMock(), settings)
        # general_security treats prose and sigma_rule equally (1.0×).
        out = retriever._security_rerank(list(results), "no_such_intent")
        assert out[0].score == out[1].score == 0.5 * 2.0  # severity=high × 1.0 source

    def test_unknown_source_type_defaults_to_one(self) -> None:
        """A source_type missing from the intent row gets a neutral 1.0 multiplier."""
        settings = _MockSettings()
        results = [
            _make_result(
                "mystery", score=0.5, severity="high", source_type="weird_unknown_type"
            ),
        ]
        retriever = SecurityRetriever(MagicMock(), settings)
        out = retriever._security_rerank(list(results), QueryIntent.CVE_LOOKUP)
        # 0.5 × high(2.0) × 1.0(default for unknown source_type) = 1.0
        assert out[0].score == 1.0

    def test_malformed_content_date_does_not_raise(self) -> None:
        """Garbage content_date strings are tolerated as if missing."""
        settings = _MockSettings()
        results = [
            _make_result(
                "bad", score=0.5, severity="high", content_date_str="not-a-date"
            ),
        ]
        retriever = SecurityRetriever(MagicMock(), settings)
        out = retriever._security_rerank(list(results), QueryIntent.GENERAL_SECURITY)
        # Parse fails silently → recency multiplier=1.0; severity high=2.0 → 1.0
        assert out[0].score == 1.0


def test_security_retriever_subclasses_base_retriever() -> None:
    """Per the Phase 7 plan, SecurityRetriever satisfies the BaseRetriever ABC."""
    from grimoire.strategies.base import BaseRetriever

    assert issubclass(SecurityRetriever, BaseRetriever)
