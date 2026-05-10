"""Tests for the Phase 1 source-type detector.

The detector is a pure function with deterministic rule precedence. These
tests cover every rule with at least one positive and one negative case,
plus precedence and robustness guards.
"""

from __future__ import annotations

import json

import pytest

from grimoire.strategies.security.corpus import SourceType, detect_source_type


# ---------------------------------------------------------------------------
# 1. Path hints
# ---------------------------------------------------------------------------


class TestPathHints:
    """Path-substring rules win first in the precedence chain."""

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/corpus/sigma-rules/win/foo.yml", SourceType.SIGMA_RULE),
            ("/data/sigma/rule.yml", SourceType.SIGMA_RULE),
            ("/feeds/nvd-cve/2024.json", SourceType.NVD_CVE),
            ("/feeds/nvd/2024.json", SourceType.NVD_CVE),
            ("/feeds/cve/2024.json", SourceType.NVD_CVE),
            ("/corpus/mitre-attack/T1059.md", SourceType.MITRE_ATTACK),
            ("/some/attack/data.json", SourceType.MITRE_ATTACK),
            ("/some/mitre/data.json", SourceType.MITRE_ATTACK),
            ("/feeds/iocs/list.txt", SourceType.IOC_LIST),
            ("/feeds/ioc-lists/list.txt", SourceType.IOC_LIST),
        ],
    )
    def test_positive(self, path: str, expected: SourceType) -> None:
        assert detect_source_type("body text", {"path": path}) is expected

    def test_mitre_defend_excluded(self) -> None:
        """``/mitre-defend/`` must NOT trigger MITRE ATT&CK classification."""
        result = detect_source_type(
            "Some defensive technique notes here.",
            {"path": "/corpus/mitre-defend/D3-AAA.md"},
        )
        assert result is not SourceType.MITRE_ATTACK

    def test_irrelevant_path_does_not_match(self) -> None:
        """Random paths fall through to content-based rules."""
        result = detect_source_type(
            "A short note.",
            {"path": "/notes/2024/random.md"},
        )
        # Should fall through; not any of the structured types.
        assert result not in {
            SourceType.SIGMA_RULE,
            SourceType.NVD_CVE,
            SourceType.MITRE_ATTACK,
            SourceType.IOC_LIST,
        }

    def test_source_path_alias_works(self) -> None:
        """``source_path`` is accepted as an alternative metadata key."""
        assert (
            detect_source_type("body", {"source_path": "/corpus/sigma/r.yml"})
            is SourceType.SIGMA_RULE
        )


# ---------------------------------------------------------------------------
# 2. Extension hints (Sigma)
# ---------------------------------------------------------------------------


class TestExtensionHints:
    SIGMA_BODY = (
        "title: Suspicious cmd usage\n"
        "logsource:\n"
        "    product: windows\n"
        "    service: security\n"
        "detection:\n"
        "    selection:\n"
        "        EventID: 4688\n"
        "    condition: selection\n"
    )

    def test_yaml_with_detection_and_logsource(self) -> None:
        result = detect_source_type(
            self.SIGMA_BODY,
            {"path": "/somewhere/rule.yml"},
        )
        assert result is SourceType.SIGMA_RULE

    def test_yaml_without_required_keys(self) -> None:
        body = "name: foo\nversion: 1\n"
        result = detect_source_type(body, {"path": "/somewhere/cfg.yml"})
        assert result is not SourceType.SIGMA_RULE

    def test_yaml_with_only_one_required_key(self) -> None:
        body = "logsource:\n    product: windows\n"
        result = detect_source_type(body, {"path": "/somewhere/cfg.yaml"})
        assert result is not SourceType.SIGMA_RULE


# ---------------------------------------------------------------------------
# 3. JSON shape
# ---------------------------------------------------------------------------


class TestJSONShape:
    def test_modern_cve_wrapper(self) -> None:
        payload = json.dumps(
            {
                "cve": {
                    "id": "CVE-2024-12345",
                    "descriptions": [{"lang": "en", "value": "demo"}],
                }
            }
        )
        assert detect_source_type(payload) is SourceType.NVD_CVE

    def test_legacy_cve_top_level_key(self) -> None:
        payload = json.dumps({"CVE-2024-12345": {"summary": "demo"}})
        assert detect_source_type(payload) is SourceType.NVD_CVE

    def test_bulk_feed_vulnerabilities(self) -> None:
        payload = json.dumps(
            {
                "vulnerabilities": [
                    {"cve": {"id": "CVE-2024-99999", "descriptions": []}},
                    {"cve": {"id": "CVE-2024-11111", "descriptions": []}},
                ]
            }
        )
        assert detect_source_type(payload) is SourceType.NVD_CVE

    def test_stix_bundle_with_attack_pattern(self) -> None:
        payload = json.dumps(
            {
                "type": "bundle",
                "id": "bundle--xyz",
                "objects": [
                    {"type": "identity", "name": "MITRE"},
                    {"type": "attack-pattern", "name": "OS Credential Dumping"},
                ],
            }
        )
        assert detect_source_type(payload) is SourceType.MITRE_ATTACK

    def test_stix_bundle_without_attack_pattern(self) -> None:
        payload = json.dumps(
            {
                "type": "bundle",
                "id": "bundle--xyz",
                "objects": [
                    {"type": "identity", "name": "MITRE"},
                    {"type": "intrusion-set", "name": "Group X"},
                ],
            }
        )
        assert detect_source_type(payload) is not SourceType.MITRE_ATTACK

    def test_unrelated_json_object(self) -> None:
        payload = json.dumps({"foo": "bar", "count": 3})
        # Falls through to prose/unknown branches.
        assert detect_source_type(payload) in {SourceType.PROSE, SourceType.UNKNOWN}


# ---------------------------------------------------------------------------
# 4. Markdown frontmatter
# ---------------------------------------------------------------------------


class TestFrontmatter:
    def test_kind_attack_pattern(self) -> None:
        body = (
            "---\n"
            "kind: attack-pattern\n"
            "name: OS Credential Dumping\n"
            "---\n"
            "# OS Credential Dumping\n\n"
            "Adversaries may attempt to dump credentials...\n"
        )
        assert detect_source_type(body) is SourceType.MITRE_ATTACK

    def test_attack_id_field(self) -> None:
        body = "---\ntitle: Demo\nattack_id: T1059\n---\nBody content here.\n"
        assert detect_source_type(body) is SourceType.MITRE_ATTACK

    def test_frontmatter_without_attack_keys(self) -> None:
        body = (
            "---\n"
            "title: Demo\n"
            "tags: [misc]\n"
            "---\n"
            "Just a normal markdown note about something.\n"
        )
        assert detect_source_type(body) is not SourceType.MITRE_ATTACK


# ---------------------------------------------------------------------------
# 5. Filename hint
# ---------------------------------------------------------------------------


class TestFilenameHint:
    @pytest.mark.parametrize(
        "path",
        [
            "/notes/T1059.md",
            "/notes/T1059.001.md",
            "/some/dir/T9999",
            "/some/dir/T1234.567.txt",
        ],
    )
    def test_positive(self, path: str) -> None:
        assert (
            detect_source_type("just some short note", {"path": path})
            is SourceType.MITRE_ATTACK
        )

    @pytest.mark.parametrize(
        "path",
        [
            "/notes/T-1059.md",
            "/notes/t1059.md",  # lowercase: rule expects uppercase T
            "/notes/TX1059.md",
            "/notes/notes-T1059.md",
        ],
    )
    def test_negative(self, path: str) -> None:
        result = detect_source_type("a", {"path": path})
        assert result is not SourceType.MITRE_ATTACK


# ---------------------------------------------------------------------------
# 6. IOC sniff
# ---------------------------------------------------------------------------


class TestIOCSniff:
    def test_ip_and_hash_list(self) -> None:
        body = (
            "192.168.1.10\n"
            "10.0.0.5\n"
            "8.8.8.8\n"
            "d41d8cd98f00b204e9800998ecf8427e\n"
            "da39a3ee5e6b4b0d3255bfef95601890afd80709\n"
            "evil.example.com\n"
        )
        assert detect_source_type(body) is SourceType.IOC_LIST

    def test_prose_paragraph_is_not_ioc(self) -> None:
        body = (
            "This is a perfectly ordinary paragraph of English text "
            "that should not be classified as an indicator of compromise list "
            "regardless of how aggressively the sniffer tries.\n"
        )
        assert detect_source_type(body) is not SourceType.IOC_LIST

    def test_too_few_lines(self) -> None:
        body = "192.168.1.1\n"
        assert detect_source_type(body) is not SourceType.IOC_LIST

    def test_bracketed_ioc_obfuscation(self) -> None:
        body = (
            "1.2.3[.]4\n"
            "5.6.7[.]8\n"
            "evil[.]example[.]com\n"
            "d41d8cd98f00b204e9800998ecf8427e\n"
        )
        assert detect_source_type(body) is SourceType.IOC_LIST


# ---------------------------------------------------------------------------
# 7. Fallback prose / unknown
# ---------------------------------------------------------------------------


class TestFallback:
    def test_prose_paragraph(self) -> None:
        body = (
            "The threat actor used a combination of phishing emails and "
            "malicious attachments to gain initial access to the network. "
            "Once inside, they moved laterally using stolen credentials.\n"
        )
        assert detect_source_type(body) is SourceType.PROSE

    @pytest.mark.parametrize("text", ["", "   ", "\n\n\t  \n"])
    def test_empty_or_whitespace_is_unknown(self, text: str) -> None:
        assert detect_source_type(text) is SourceType.UNKNOWN

    def test_short_unstructured_is_unknown(self) -> None:
        assert detect_source_type("hi\nthere\n") is SourceType.UNKNOWN


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_malformed_json_does_not_raise(self) -> None:
        # Must not raise.
        result = detect_source_type("{")
        assert isinstance(result, SourceType)

    def test_invalid_yaml_does_not_raise(self) -> None:
        body = "logsource: [unclosed\ndetection: ???\n"
        # Even though the path triggers the YAML extension sniff, regexes only
        # peek at line starts and never call a YAML parser — must not raise.
        result = detect_source_type(body, {"path": "/x/r.yml"})
        assert isinstance(result, SourceType)

    def test_binary_like_input_does_not_raise(self) -> None:
        body = "\x00\x01\x02PK\x03\x04weird binary-ish bytes \xff\xfe garbage"
        result = detect_source_type(body)
        assert isinstance(result, SourceType)

    def test_no_metadata_argument(self) -> None:
        """Calling without ``source_metadata`` must not raise."""
        result = detect_source_type("hello world")
        assert isinstance(result, SourceType)

    def test_none_metadata(self) -> None:
        result = detect_source_type("hello world", None)
        assert isinstance(result, SourceType)

    def test_metadata_without_path_keys(self) -> None:
        result = detect_source_type("hello world", {"unrelated": 1})
        assert isinstance(result, SourceType)


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_path_hint_beats_content_sniff(self) -> None:
        """Sigma path wins even when body looks like prose."""
        body = (
            "This is actually just a long prose paragraph that happens to "
            "live in a file under a sigma-rules directory for some reason.\n"
        )
        result = detect_source_type(body, {"path": "/corpus/sigma-rules/foo.yml"})
        assert result is SourceType.SIGMA_RULE

    def test_path_hint_beats_json_shape(self) -> None:
        """NVD path wins even if the body parses as a STIX bundle."""
        payload = json.dumps(
            {
                "type": "bundle",
                "objects": [{"type": "attack-pattern", "name": "x"}],
            }
        )
        result = detect_source_type(payload, {"path": "/feeds/nvd-cve/x.json"})
        assert result is SourceType.NVD_CVE

    def test_extension_hint_runs_before_json(self) -> None:
        """A .yml path with sigma keys must win over any later rule."""
        body = (
            "title: t\n"
            "logsource:\n  product: windows\n"
            "detection:\n  condition: selection\n"
        )
        result = detect_source_type(body, {"path": "/random/r.yml"})
        assert result is SourceType.SIGMA_RULE
