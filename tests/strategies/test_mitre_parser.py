"""Tests for the Phase 5 MITRE ATT&CK parser.

Covers:

* STIX bundle parsing: technique count, metadata extraction, section text.
* Markdown parsing: frontmatter extraction, H2 section split, fallback id scan.
* Edge cases: empty input, non-MITRE text raises ValueError.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grimoire.strategies.security.parsers.mitre import parse_mitre

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "security" / "mitre"
STIX_FIXTURE = FIXTURE_DIR / "attack-pattern.json"
MD_FIXTURE = FIXTURE_DIR / "T1059.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_stix() -> str:
    return STIX_FIXTURE.read_text(encoding="utf-8")


def _load_md() -> str:
    return MD_FIXTURE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# STIX bundle
# ---------------------------------------------------------------------------


class TestStixBundle:
    def test_two_techniques_parsed(self) -> None:
        text = _load_stix()
        parsed = parse_mitre(text)
        # T1059.001 → 3 sections (Description, Detection, Mitigations)
        # T1218    → 3 sections (Description, Detection, Mitigations)
        assert len(parsed) == 6

    def test_technique_id_extracted(self) -> None:
        text = _load_stix()
        parsed = parse_mitre(text)
        ids = {sec_meta.mitre_technique_id for _, sec_meta in parsed}
        assert "T1059.001" in ids
        assert "T1218" in ids

    def test_tactic_extracted(self) -> None:
        text = _load_stix()
        parsed = parse_mitre(text)
        tactics = {sec_meta.mitre_tactic for _, sec_meta in parsed}
        assert "execution" in tactics
        assert "defense_evasion" in tactics

    def test_platforms_extracted(self) -> None:
        text = _load_stix()
        parsed = parse_mitre(text)
        platforms = {p for _, sec_meta in parsed for p in sec_meta.platforms}
        assert "windows" in platforms

    def test_section_text_contains_technique_name(self) -> None:
        text = _load_stix()
        parsed = parse_mitre(text)
        for sec_text, _ in parsed:
            assert "Technique:" in sec_text

    def test_source_type_is_mitre_attack(self) -> None:
        text = _load_stix()
        parsed = parse_mitre(text)
        for _, sec_meta in parsed:
            assert sec_meta.source_type.value == "mitre_attack"

    def test_security_metadata_is_unknown_severity(self) -> None:
        text = _load_stix()
        parsed = parse_mitre(text)
        for _, sec_meta in parsed:
            assert sec_meta.severity.value == "unknown"

    def test_description_section_present(self) -> None:
        text = _load_stix()
        parsed = parse_mitre(text)
        descs = [t for t, _ in parsed if "Description:" in t]
        assert len(descs) == 2  # one per technique

    def test_detection_section_present(self) -> None:
        text = _load_stix()
        parsed = parse_mitre(text)
        dets = [t for t, _ in parsed if "Detection:" in t]
        assert len(dets) == 2

    def test_mitigations_section_present(self) -> None:
        text = _load_stix()
        parsed = parse_mitre(text)
        mits = [t for t, _ in parsed if "Mitigations:" in t]
        assert len(mits) == 2


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


class TestMarkdown:
    def test_parses_markdown(self) -> None:
        text = _load_md()
        parsed = parse_mitre(text)
        # Lead text before first H2 + Description + Detection + Mitigations = 4
        assert len(parsed) == 4

    def test_attack_id_from_frontmatter(self) -> None:
        text = _load_md()
        parsed = parse_mitre(text)
        for _, sec_meta in parsed:
            assert sec_meta.mitre_technique_id == "T1059.001"

    def test_tactic_from_frontmatter(self) -> None:
        text = _load_md()
        parsed = parse_mitre(text)
        for _, sec_meta in parsed:
            assert sec_meta.mitre_tactic == "execution"

    def test_platforms_from_frontmatter(self) -> None:
        text = _load_md()
        parsed = parse_mitre(text)
        for _, sec_meta in parsed:
            assert "windows" in sec_meta.platforms

    def test_sections_split_by_h2(self) -> None:
        text = _load_md()
        parsed = parse_mitre(text)
        # Only look at chunks that have a "Section:" prefix.
        section_chunks = [t for t, _ in parsed if "Section:" in t]
        # "Section: X" is the 3rd line (index 2) in each chunk.
        headings = {t.split("\n")[2] for t in section_chunks}
        assert any("Detection" in h for h in headings)
        assert any("Mitigations" in h for h in headings)

    def test_leading_text_becomes_description(self) -> None:
        text = _load_md()
        parsed = parse_mitre(text)
        descs = [t for t, _ in parsed if "Description:" in t]
        # Lead text before first H2 is treated as Description.
        assert len(descs) == 1
        assert "PowerShell" in descs[0]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_returns_empty(self) -> None:
        assert parse_mitre("") == []

    def test_whitespace_returns_empty(self) -> None:
        assert parse_mitre("   \n  ") == []

    def test_non_mitre_raises(self) -> None:
        with pytest.raises(ValueError, match="does not appear to be MITRE"):
            parse_mitre("This is just random prose.")

    def test_plain_json_without_attack_pattern_raises(self) -> None:
        with pytest.raises(ValueError, match="does not appear to be MITRE"):
            parse_mitre(json.dumps({"foo": "bar"}))

    def test_plain_yaml_without_frontmatter_raises(self) -> None:
        with pytest.raises(ValueError, match="does not appear to be MITRE"):
            parse_mitre("title: hello\nbody: world\n")
