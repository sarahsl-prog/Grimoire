"""Tests for the Phase 4 NVD CVE parser.

Covers:

* Bulk feed parsing from the sample fixture (3 CVEs),
* CVSS v3.1 / v3.0 / v2 score and severity extraction,
* CWE scraping,
* CPE product extraction,
* Published date parsing,
* Modern single-record wrapper,
* Legacy key-value shape,
* Edge cases (empty, bad JSON, missing metrics).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grimoire.strategies.security.corpus import SourceType
from grimoire.strategies.security.metadata import SecurityMetadata, Severity
from grimoire.strategies.security.parsers.nvd import (
    parse_nvd_json,
    severity_from_cvss_score,
)


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "security" / "nvd"
SAMPLE_BULK = FIXTURE_DIR / "nvdcve-sample.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_sample() -> str:
    return SAMPLE_BULK.read_text(encoding="utf-8")


def _load_sample_dict() -> dict:
    return json.loads(_load_sample())


# ---------------------------------------------------------------------------
# 1. Bulk feed parse
# ---------------------------------------------------------------------------


class TestParseBulkFeed:
    """End-to-end parse of the 3-CVE fixture."""

    def test_parses_all_three_cves(self) -> None:
        text = _load_sample()
        results = parse_nvd_json(text)
        assert len(results) == 3

    def test_each_result_is_tuple(self) -> None:
        text = _load_sample()
        results = parse_nvd_json(text)
        for item in results:
            assert isinstance(item, tuple)
            assert len(item) == 2
            cve_text, meta = item
            assert isinstance(cve_text, str) and cve_text.strip()
            assert isinstance(meta, SecurityMetadata)

    def test_all_source_types_are_nvd(self) -> None:
        text = _load_sample()
        results = parse_nvd_json(text)
        for _, meta in results:
            assert meta.source_type is SourceType.NVD_CVE


# ---------------------------------------------------------------------------
# 2. Per-CVE metadata
# ---------------------------------------------------------------------------


class TestCve1Critical:
    """CVE-2024-12345 — critical, 9.8, CWE-78, two products."""

    def setup_method(self) -> None:
        text = _load_sample()
        self.results = parse_nvd_json(text)
        self.text, self.meta = self.results[0]

    def test_cve_id(self) -> None:
        assert self.meta.cve_id == "CVE-2024-12345"

    def test_cvss_score(self) -> None:
        assert self.meta.cvss_score == 9.8

    def test_severity_critical(self) -> None:
        assert self.meta.severity is Severity.CRITICAL

    def test_cwe_ids(self) -> None:
        assert self.meta.cwe_ids == ["CWE-78"]

    def test_affected_products(self) -> None:
        assert len(self.meta.affected_products) == 1
        assert "examplecorp exampleapp" in self.meta.affected_products[0]

    def test_published_date(self) -> None:
        assert self.meta.published_date is not None
        assert self.meta.published_date.year == 2024
        assert self.meta.published_date.month == 1

    def test_content_date_matches_published(self) -> None:
        assert self.meta.content_date == self.meta.published_date

    def test_description_in_text(self) -> None:
        assert "remote code execution" in self.text.lower()

    def test_references_in_text(self) -> None:
        assert "example.com/advisories" in self.text


class TestCve2High:
    """CVE-2024-23456 — high, 7.5, CWE-89+CWE-564, one product."""

    def setup_method(self) -> None:
        text = _load_sample()
        self.results = parse_nvd_json(text)
        self.text, self.meta = self.results[1]

    def test_cve_id(self) -> None:
        assert self.meta.cve_id == "CVE-2024-23456"

    def test_cvss_score(self) -> None:
        assert self.meta.cvss_score == 7.5

    def test_severity_high(self) -> None:
        assert self.meta.severity is Severity.HIGH

    def test_cwe_ids(self) -> None:
        assert self.meta.cwe_ids == ["CWE-89", "CWE-564"]

    def test_affected_products(self) -> None:
        assert len(self.meta.affected_products) == 1
        assert "acmecorp acmewidget" in self.meta.affected_products[0].lower()


class TestCve3Low:
    """CVE-2024-34567 — low, 1.9 (v3.1; v3.0 is 2.1 but v3.1 wins)."""

    def setup_method(self) -> None:
        text = _load_sample()
        self.results = parse_nvd_json(text)
        self.text, self.meta = self.results[2]

    def test_cve_id(self) -> None:
        assert self.meta.cve_id == "CVE-2024-34567"

    def test_cvss_score_prefers_v31(self) -> None:
        # v3.1 is 1.9; v3.0 is 2.1. v3.1 should win.
        assert self.meta.cvss_score == 1.9

    def test_severity_low(self) -> None:
        assert self.meta.severity is Severity.LOW

    def test_cwe_ids(self) -> None:
        assert self.meta.cwe_ids == ["CWE-532"]


# ---------------------------------------------------------------------------
# 3. severity_from_cvss_score mapping
# ---------------------------------------------------------------------------


class TestSeverityFromScore:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (0.0, Severity.INFO),
            (1.5, Severity.LOW),
            (3.9, Severity.LOW),
            (4.0, Severity.MEDIUM),
            (6.9, Severity.MEDIUM),
            (7.0, Severity.HIGH),
            (8.9, Severity.HIGH),
            (9.0, Severity.CRITICAL),
            (10.0, Severity.CRITICAL),
            (None, Severity.UNKNOWN),
            (-0.1, Severity.UNKNOWN),
            (10.1, Severity.UNKNOWN),
        ],
    )
    def test_mapping(self, score: float | None, expected: Severity) -> None:
        assert severity_from_cvss_score(score) is expected


# ---------------------------------------------------------------------------
# 4. Single-record wrapper
# ---------------------------------------------------------------------------


class TestSingleRecordWrapper:
    def test_modern_wrapper_parses(self) -> None:
        payload = {
            "cve": {
                "id": "CVE-2024-99999",
                "published": "2024-06-01T00:00:00.000",
                "descriptions": [{"lang": "en", "value": "Test vulnerability."}],
                "metrics": {
                    "cvssMetricV31": [
                        {
                            "type": "Primary",
                            "cvssData": {"baseScore": 5.5, "baseSeverity": "MEDIUM"},
                        }
                    ]
                },
            }
        }
        results = parse_nvd_json(payload)
        assert len(results) == 1
        text, meta = results[0]
        assert meta.cve_id == "CVE-2024-99999"
        assert meta.cvss_score == 5.5
        assert meta.severity is Severity.MEDIUM


# ---------------------------------------------------------------------------
# 5. Legacy key-value shape
# ---------------------------------------------------------------------------


class TestLegacyShape:
    def test_legacy_key_value(self) -> None:
        payload = {
            "CVE-2024-88888": {
                "published": "2024-05-01T00:00:00.000",
                "descriptions": [{"lang": "en", "value": "Legacy style record."}],
            }
        }
        results = parse_nvd_json(payload)
        assert len(results) == 1
        text, meta = results[0]
        assert meta.cve_id == "CVE-2024-88888"
        assert "Legacy style record" in text


# ---------------------------------------------------------------------------
# 6. Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_string_returns_empty(self) -> None:
        assert parse_nvd_json("") == []

    def test_whitespace_only_returns_empty(self) -> None:
        assert parse_nvd_json("   \n\n  ") == []

    def test_bad_json_returns_empty(self) -> None:
        assert parse_nvd_json("not json {{[") == []

    def test_missing_metrics_defaults_unknown(self) -> None:
        payload = {
            "cve": {
                "id": "CVE-2024-77777",
                "descriptions": [{"lang": "en", "value": "No metrics."}],
            }
        }
        results = parse_nvd_json(payload)
        assert len(results) == 1
        _, meta = results[0]
        assert meta.cvss_score is None
        assert meta.severity is Severity.UNKNOWN

    def test_dict_input_works(self) -> None:
        d = _load_sample_dict()
        results = parse_nvd_json(d)
        assert len(results) == 3
