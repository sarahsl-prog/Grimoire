"""Tests for the playbook parser.

Covers:

* Front-matter-only and section-based parsing,
* Metadata extraction (playbook_phase, action_type, trigger,
  severity, mitre_technique_id, platforms),
* One chunk per major section,
* Edge cases (empty text, no front matter, missing trigger, CRLF).
"""

from __future__ import annotations

from pathlib import Path

from grimoire.strategies.security.corpus import SourceType
from grimoire.strategies.security.metadata import Severity
from grimoire.strategies.security.parsers.playbook import parse_playbook


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "security" / "playbooks"
RANSOMWARE = FIXTURE_DIR / "ransomware-containment.md"
PHISHING = FIXTURE_DIR / "phishing-response.md"


class TestParseFrontMatterFixture:
    """Parse the front-matter + sections ransomware fixture."""

    def test_source_type_is_playbook(self) -> None:
        results = parse_playbook(RANSOMWARE.read_text("utf-8"))
        assert results, "expected at least one parsed section"
        for _, meta in results:
            assert meta.source_type is SourceType.PLAYBOOK

    def test_extracts_playbook_fields(self) -> None:
        results = parse_playbook(RANSOMWARE.read_text("utf-8"))
        meta = results[0][1]
        assert meta.playbook_phase == "contain"
        assert meta.action_type == "manual"
        assert meta.trigger == "Ransomware encryption activity detected on a host"
        assert meta.severity is Severity.CRITICAL
        assert meta.mitre_technique_id == "T1486"
        assert meta.platforms == ["windows", "linux"]

    def test_one_chunk_per_major_section(self) -> None:
        results = parse_playbook(RANSOMWARE.read_text("utf-8"))
        # Sections: Trigger, Preparation, Actions, Containment, Recovery
        texts = [text for text, _ in results]
        assert len(results) >= 4
        # The trigger section should contain the trigger description
        assert any("EDR" in t for t in texts)


class TestParseSectionsOnly:
    """Parse markdown with canonical sections but no front matter."""

    def test_phishing_fixture_sections(self) -> None:
        results = parse_playbook(PHISHING.read_text("utf-8"))
        assert len(results) >= 2
        for _, meta in results:
            assert meta.source_type is SourceType.PLAYBOOK
        # No front matter → no phase/severity extracted.
        first_meta = results[0][1]
        assert first_meta.playbook_phase is None

    def test_sections_are_nonempty(self) -> None:
        results = parse_playbook(PHISHING.read_text("utf-8"))
        for text, _ in results:
            assert text.strip()


class TestEdgeCases:
    def test_empty_text(self) -> None:
        assert parse_playbook("") == []
        assert parse_playbook("   \n  ") == []

    def test_plain_markdown_without_sections(self) -> None:
        text = "# A Note\n\nJust some prose without Trigger or Actions sections.\n"
        assert parse_playbook(text) == []

    def test_single_section_still_parses(self) -> None:
        """The parser is lenient: a lone section is chunkable. Classification
        strictness (requiring Trigger AND Actions) lives in corpus.py."""
        text = "# Runbook\n\n## Actions\n\n1. Do the thing.\n"
        results = parse_playbook(text)
        assert len(results) == 1
        assert results[0][0].startswith("Actions")
        assert results[0][1].source_type is SourceType.PLAYBOOK

    def test_crlf_line_endings(self) -> None:
        text = RANSOMWARE.read_text("utf-8").replace("\n", "\r\n")
        results = parse_playbook(text)
        assert results
        assert results[0][1].playbook_phase == "contain"

    def test_malformed_frontmatter_falls_back_to_sections(self) -> None:
        text = (
            "---\n"
            "title: Broken\n"
            "  bad indent: [unclosed\n"
            "---\n"
            "## Trigger\n\nSomething happened.\n\n"
            "## Actions\n\n1. Handle it.\n"
        )
        results = parse_playbook(text)
        assert results
        _, meta = results[0]
        # Front matter failed → no phase extracted, but sections parsed
        assert meta.playbook_phase is None


class TestSeverityMapping:
    """Playbook severity strings map onto the shared Severity buckets."""

    def test_all_valid_severities(self) -> None:
        for level, expected in [
            ("critical", Severity.CRITICAL),
            ("high", Severity.HIGH),
            ("medium", Severity.MEDIUM),
            ("low", Severity.LOW),
            ("info", Severity.INFO),
        ]:
            text = (
                "---\n"
                "title: T\n"
                "playbook: x\n"
                f"severity: {level}\n"
                "---\n"
                "## Trigger\n\nt\n\n## Actions\n\na\n"
            )
            results = parse_playbook(text)
            assert results, level
            assert results[0][1].severity is expected

    def test_unknown_severity_defaults_to_unknown(self) -> None:
        text = (
            "---\n"
            "title: T\n"
            "playbook: x\n"
            "severity: catastrophic\n"
            "---\n"
            "## Trigger\n\nt\n\n## Actions\n\na\n"
        )
        results = parse_playbook(text)
        assert results[0][1].severity is Severity.UNKNOWN
