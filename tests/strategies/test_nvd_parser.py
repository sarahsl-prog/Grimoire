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


# ---------------------------------------------------------------------------
# 7. baseSeverity "NONE" (regression: silently dropped records)
# ---------------------------------------------------------------------------


class TestBaseSeverityNone:
    """NVD emits ``baseSeverity: "NONE"`` with ``baseScore: 0.0``.

    "none" is not a :class:`Severity` member, so the unguarded
    ``Severity(severity_str.lower())`` raised ValueError, which
    ``parse_nvd_json`` swallowed — dropping the record entirely.
    Shape copied from live CVE-2026-23634 / CVE-2025-6591.
    """

    @staticmethod
    def _payload(metric_key: str, version: str) -> dict:
        return {
            "cve": {
                "id": "CVE-2026-23634",
                "published": "2026-01-14T18:15:00.000",
                "descriptions": [{"lang": "en", "value": "Zero-impact advisory."}],
                "metrics": {
                    metric_key: [
                        {
                            "source": "security-advisories@github.com",
                            "type": "Primary",
                            "cvssData": {
                                "version": version,
                                "baseScore": 0.0,
                                "baseSeverity": "NONE",
                            },
                        }
                    ]
                },
            }
        }

    @pytest.mark.parametrize(
        "metric_key,version",
        [("cvssMetricV31", "3.1"), ("cvssMetricV40", "4.0")],
    )
    def test_none_severity_record_is_not_dropped(
        self, metric_key: str, version: str
    ) -> None:
        results = parse_nvd_json(self._payload(metric_key, version))
        assert len(results) == 1, "record must survive, not be swallowed"

    @pytest.mark.parametrize(
        "metric_key,version",
        [("cvssMetricV31", "3.1"), ("cvssMetricV40", "4.0")],
    )
    def test_none_severity_falls_back_to_score(
        self, metric_key: str, version: str
    ) -> None:
        _, meta = parse_nvd_json(self._payload(metric_key, version))[0]
        assert meta.cve_id == "CVE-2026-23634"
        assert meta.cvss_score == 0.0
        # severity_from_cvss_score(0.0) -> INFO
        assert meta.severity is Severity.INFO

    def test_unrecognised_severity_string_falls_back_to_score(self) -> None:
        """Any out-of-enum baseSeverity should degrade, not explode."""
        payload = self._payload("cvssMetricV31", "3.1")
        payload["cve"]["metrics"]["cvssMetricV31"][0]["cvssData"].update(
            {"baseScore": 7.5, "baseSeverity": "SEVERE"}
        )
        results = parse_nvd_json(payload)
        assert len(results) == 1
        _, meta = results[0]
        assert meta.cvss_score == 7.5
        assert meta.severity is Severity.HIGH

    def test_none_severity_in_bulk_feed_survives(self) -> None:
        """The drop happened inside parse_nvd_json's bulk loop."""
        payload = {
            "vulnerabilities": [
                {"cve": self._payload("cvssMetricV40", "4.0")["cve"]},
                {
                    "cve": {
                        "id": "CVE-2026-00001",
                        "descriptions": [{"lang": "en", "value": "Normal record."}],
                        "metrics": {
                            "cvssMetricV31": [
                                {
                                    "type": "Primary",
                                    "cvssData": {
                                        "baseScore": 9.8,
                                        "baseSeverity": "CRITICAL",
                                    },
                                }
                            ]
                        },
                    }
                },
            ]
        }
        results = parse_nvd_json(payload)
        assert len(results) == 2
        assert results[0][1].severity is Severity.INFO
        assert results[1][1].severity is Severity.CRITICAL


# ---------------------------------------------------------------------------
# 8. CVSS v4.0 extraction
# ---------------------------------------------------------------------------


# Shape copied verbatim from live NVD 2.0 data (CVE-2026-* QNAP advisory).
_V40_ENTRY = {
    "source": "security@qnapsecurity.com.tw",
    "type": "Secondary",
    "cvssData": {
        "version": "4.0",
        "vectorString": "CVSS:4.0/AV:L/AC:H/AT:P/PR:L/UI:N/VC:H/VI:H/VA:H",
        "baseScore": 4.4,
        "baseSeverity": "MEDIUM",
        "attackVector": "LOCAL",
        "vulnConfidentialityImpact": "HIGH",
    },
}


class TestCvssV40:
    """~4-6% of 2026 records carry only a ``cvssMetricV40`` block."""

    def test_v40_only_record_extracts_score(self) -> None:
        payload = {
            "cve": {
                "id": "CVE-2026-40001",
                "descriptions": [{"lang": "en", "value": "v4.0-only record."}],
                "metrics": {"cvssMetricV40": [_V40_ENTRY]},
            }
        }
        results = parse_nvd_json(payload)
        assert len(results) == 1
        text, meta = results[0]
        assert meta.cvss_score == 4.4
        assert meta.severity is Severity.MEDIUM
        assert "CVSS Score: 4.4" in text

    def test_v40_only_with_ssvc_sibling(self) -> None:
        """Real records pair cvssMetricV40 with a non-CVSS ssvcV203 block."""
        payload = {
            "cve": {
                "id": "CVE-2026-40002",
                "descriptions": [{"lang": "en", "value": "v4.0 plus ssvc."}],
                "metrics": {
                    "cvssMetricV40": [_V40_ENTRY],
                    "ssvcV203": [{"source": "cisa", "type": "Primary"}],
                },
            }
        }
        _, meta = parse_nvd_json(payload)[0]
        assert meta.cvss_score == 4.4
        assert meta.severity is Severity.MEDIUM

    def test_v40_preferred_over_older_at_same_authority(self) -> None:
        """Among equally-authoritative entries, the newest version wins."""
        payload = {
            "cve": {
                "id": "CVE-2026-40003",
                "descriptions": [{"lang": "en", "value": "Both v4.0 and v3.1."}],
                "metrics": {
                    "cvssMetricV40": [
                        {
                            "type": "Secondary",
                            "cvssData": {"baseScore": 6.9, "baseSeverity": "MEDIUM"},
                        }
                    ],
                    "cvssMetricV31": [
                        {
                            "type": "Secondary",
                            "cvssData": {"baseScore": 4.9, "baseSeverity": "MEDIUM"},
                        }
                    ],
                },
            }
        }
        _, meta = parse_nvd_json(payload)[0]
        assert meta.cvss_score == 6.9

    def test_primary_v31_outranks_secondary_v40(self) -> None:
        """NVD-authored Primary scores beat CNA Secondary ones.

        NVD does not yet issue Primary v4.0 scores; version-only ordering
        would demote authoritative v3.1 scores (live: CVE-2026-0544,
        CRITICAL 9.8 -> MEDIUM 5.5).
        """
        payload = {
            "cve": {
                "id": "CVE-2026-0544",
                "descriptions": [{"lang": "en", "value": "Mixed authority."}],
                "metrics": {
                    "cvssMetricV40": [
                        {
                            "source": "cna@vendor.example",
                            "type": "Secondary",
                            "cvssData": {"baseScore": 5.5, "baseSeverity": "MEDIUM"},
                        }
                    ],
                    "cvssMetricV31": [
                        {
                            "source": "nvd@nist.gov",
                            "type": "Primary",
                            "cvssData": {"baseScore": 9.8, "baseSeverity": "CRITICAL"},
                        }
                    ],
                },
            }
        }
        _, meta = parse_nvd_json(payload)[0]
        assert meta.cvss_score == 9.8
        assert meta.severity is Severity.CRITICAL

    def test_v40_none_severity_combined_regression(self) -> None:
        """Both bugs at once: a v4.0-only record scored 0.0 / NONE."""
        payload = {
            "cve": {
                "id": "CVE-2025-6591",
                "descriptions": [{"lang": "en", "value": "v4.0 zero-impact."}],
                "metrics": {
                    "cvssMetricV40": [
                        {
                            "type": "Secondary",
                            "cvssData": {
                                "version": "4.0",
                                "baseScore": 0.0,
                                "baseSeverity": "NONE",
                            },
                        }
                    ]
                },
            }
        }
        results = parse_nvd_json(payload)
        assert len(results) == 1
        _, meta = results[0]
        assert meta.cvss_score == 0.0
        assert meta.severity is Severity.INFO

    def test_malformed_entry_falls_through_to_next(self) -> None:
        """A junk v4.0 entry must not mask a usable v3.1 score."""
        payload = {
            "cve": {
                "id": "CVE-2026-40004",
                "descriptions": [{"lang": "en", "value": "Malformed v4.0."}],
                "metrics": {
                    "cvssMetricV40": [{"type": "Primary", "cvssData": None}],
                    "cvssMetricV31": [
                        {
                            "type": "Primary",
                            "cvssData": {"baseScore": 7.5, "baseSeverity": "HIGH"},
                        }
                    ],
                },
            }
        }
        _, meta = parse_nvd_json(payload)[0]
        assert meta.cvss_score == 7.5
        assert meta.severity is Severity.HIGH
