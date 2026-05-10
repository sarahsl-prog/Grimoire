"""Tests for the Phase 3 Sigma parser.

Covers:

* Multi-doc parsing from the sample fixture,
* Metadata extraction (severity, technique id, tactic, platforms, log sources,
  detection categories),
* ``sigma_level_to_severity`` mapping table,
* Edge cases (empty text, bad YAML, missing required fields,
  tags without technique ids).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grimoire.strategies.security.corpus import SourceType
from grimoire.strategies.security.metadata import SecurityMetadata, Severity
from grimoire.strategies.security.parsers.sigma import (
    parse_sigma,
    sigma_level_to_severity,
)


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "security" / "sigma"
SAMPLE_RULES = FIXTURE_DIR / "sample_rules.yml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_sample() -> str:
    return SAMPLE_RULES.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Parse the fixture
# ---------------------------------------------------------------------------


class TestParseSample:
    """End-to-end parse of the 4-rule fixture file."""

    def test_parses_all_four_rules(self) -> None:
        text = _load_sample()
        results = parse_sigma(text)
        assert len(results) == 4

    def test_each_result_is_tuple(self) -> None:
        text = _load_sample()
        results = parse_sigma(text)
        for item in results:
            assert isinstance(item, tuple)
            assert len(item) == 2
            rule_text, meta = item
            assert isinstance(rule_text, str) and rule_text.strip()
            assert isinstance(meta, SecurityMetadata)

    def test_all_source_types_are_sigma(self) -> None:
        text = _load_sample()
        results = parse_sigma(text)
        for _, meta in results:
            assert meta.source_type is SourceType.SIGMA_RULE


# ---------------------------------------------------------------------------
# 2. Metadata extraction — per-rule
# ---------------------------------------------------------------------------


class TestRule1PowerShell:
    """First rule: Windows / PowerShell / high severity / T1059.001."""

    def setup_method(self) -> None:
        text = _load_sample()
        self.results = parse_sigma(text)
        self.text, self.meta = self.results[0]

    def test_title_present(self) -> None:
        assert "Suspicious PowerShell Download" in self.text

    def test_severity_high(self) -> None:
        assert self.meta.severity is Severity.HIGH

    def test_mitre_technique_id(self) -> None:
        assert self.meta.mitre_technique_id == "T1059.001"

    def test_mitre_tactic(self) -> None:
        assert self.meta.mitre_tactic == "execution"

    def test_platform_windows(self) -> None:
        assert "windows" in self.meta.platforms

    def test_log_sources(self) -> None:
        assert "sysmon" in self.meta.log_sources
        assert "process_creation" in self.meta.log_sources

    def test_detection_categories(self) -> None:
        assert any("condition" in cat for cat in self.meta.detection_categories)
        assert any(
            "Legitimate administrative scripts" in cat
            for cat in self.meta.detection_categories
        )


class TestRule2Linux:
    """Second rule: Linux / medium severity / T1095."""

    def setup_method(self) -> None:
        text = _load_sample()
        self.results = parse_sigma(text)
        self.text, self.meta = self.results[1]

    def test_severity_medium(self) -> None:
        assert self.meta.severity is Severity.MEDIUM

    def test_mitre_technique_id(self) -> None:
        assert self.meta.mitre_technique_id == "T1095"

    def test_mitre_tactic(self) -> None:
        assert self.meta.mitre_tactic == "command and control"

    def test_platform_linux(self) -> None:
        assert "linux" in self.meta.platforms


class TestRule3AWS:
    """Third rule: AWS / critical severity / T1078.004."""

    def setup_method(self) -> None:
        text = _load_sample()
        self.results = parse_sigma(text)
        self.text, self.meta = self.results[2]

    def test_severity_critical(self) -> None:
        assert self.meta.severity is Severity.CRITICAL

    def test_mitre_technique_id(self) -> None:
        assert self.meta.mitre_technique_id == "T1078.004"

    def test_mitre_tactic(self) -> None:
        assert self.meta.mitre_tactic == "initial access"

    def test_platform_aws(self) -> None:
        assert "aws" in self.meta.platforms


class TestRule4MacOS:
    """Fourth rule: macOS / low severity / T1059.002 / tactic=execution."""

    def setup_method(self) -> None:
        text = _load_sample()
        self.results = parse_sigma(text)
        self.text, self.meta = self.results[3]

    def test_severity_low(self) -> None:
        assert self.meta.severity is Severity.LOW

    def test_mitre_technique_id(self) -> None:
        assert self.meta.mitre_technique_id == "T1059.002"

    def test_tactic_is_execution(self) -> None:
        assert self.meta.mitre_tactic == "execution"

    def test_platform_macos(self) -> None:
        assert "macos" in self.meta.platforms


# ---------------------------------------------------------------------------
# 3. sigma_level_to_severity mapping
# ---------------------------------------------------------------------------


class TestLevelToSeverity:
    @pytest.mark.parametrize(
        "level,expected",
        [
            ("critical", Severity.CRITICAL),
            ("high", Severity.HIGH),
            ("medium", Severity.MEDIUM),
            ("low", Severity.LOW),
            ("informational", Severity.INFO),
            ("informational_only", Severity.INFO),
            ("unknown", Severity.UNKNOWN),
            ("", Severity.UNKNOWN),
            (None, Severity.UNKNOWN),
        ],
    )
    def test_mapping(self, level: str | None, expected: Severity) -> None:
        assert sigma_level_to_severity(level) is expected


# ---------------------------------------------------------------------------
# 4. Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_string_returns_empty(self) -> None:
        assert parse_sigma("") == []

    def test_whitespace_only_returns_empty(self) -> None:
        assert parse_sigma("   \n\n  ") == []

    def test_bad_yaml_returns_empty(self) -> None:
        assert parse_sigma("not: yaml: [broken") == []

    def test_no_title_or_id_skipped(self) -> None:
        body = (
            "logsource:\n"
            "    product: windows\n"
            "detection:\n"
            "    selection:\n"
            "        EventID: 4688\n"
            "    condition: selection\n"
        )
        assert parse_sigma(body) == []

    def test_single_rule_no_mitre_tags(self) -> None:
        body = (
            "title: Generic Windows Event\n"
            "id: a1b2c3d4-e5f6-7890-abcd-ef1234567890\n"
            "logsource:\n"
            "    product: windows\n"
            "level: low\n"
        )
        results = parse_sigma(body)
        assert len(results) == 1
        _, meta = results[0]
        assert meta.mitre_technique_id is None
        assert meta.mitre_tactic is None
        assert meta.severity is Severity.LOW
        assert meta.platforms == ["windows"]
