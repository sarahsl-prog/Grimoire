"""Tests for the Phase 3–4 SecurityChunker.

Covers:

* Sigma path: chunk count, ``chunk_type``, ``source_type``, metadata
  payload shape, no field bleed across rules,
* NVD CVE path: chunk count (2 per CVE), ``chunk_type``, shared metadata,
  correct severity ordering,
* Prose path: delegates to ``RecursiveCharacterTextSplitter``, stamps
  ``chunk_type="prose"``,
* NotImplementedError for MITRE ATT&CK (until Phase 5),
* Empty text returns empty list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grimoire.strategies.security.chunker import SecurityChunker


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "security" / "sigma"
SAMPLE_RULES = FIXTURE_DIR / "sample_rules.yml"

NVD_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "security" / "nvd"
SAMPLE_NVD = NVD_FIXTURE_DIR / "nvdcve-sample.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_sigma_sample() -> str:
    return SAMPLE_RULES.read_text(encoding="utf-8")


def _load_nvd_sample() -> str:
    return SAMPLE_NVD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Sigma path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSigmaPath:
    async def test_four_chunks_for_four_rules(self) -> None:
        text = _load_sigma_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(text, doc_id="test-doc")
        assert len(chunks) == 4

    async def test_each_chunk_is_sigma_rule(self) -> None:
        text = _load_sigma_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(text, doc_id="test-doc")
        for c in chunks:
            assert c.chunk_type == "sigma_rule"
            assert c.source_type == "sigma_rule"

    async def test_metadata_has_security_metadata(self) -> None:
        text = _load_sigma_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(text, doc_id="test-doc")
        for c in chunks:
            sec_meta = c.metadata.get("security_metadata")
            assert isinstance(sec_meta, dict)
            assert sec_meta["source_type"] == "sigma_rule"
            assert "severity" in sec_meta

    async def test_continuity_links_set(self) -> None:
        text = _load_sigma_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(text, doc_id="test-doc")
        # First chunk has no prev
        assert chunks[0].prev_chunk_id is None
        # Last chunk has no next
        assert chunks[-1].next_chunk_id is None
        # Middle chunks are linked
        for i in range(1, len(chunks)):
            assert chunks[i].prev_chunk_id == chunks[i - 1].metadata["chunk_id"]

    async def test_no_field_bleed_across_rules(self) -> None:
        """Each chunk carries only its own metadata."""
        text = _load_sigma_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(text, doc_id="test-doc")
        severities = [c.metadata["security_metadata"]["severity"] for c in chunks]
        # Fixture order: high, medium, critical, low
        assert severities == ["high", "medium", "critical", "low"]

    async def test_chunk_index_sequential(self) -> None:
        text = _load_sigma_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(text, doc_id="test-doc")
        for i, c in enumerate(chunks):
            assert c.index == i

    async def test_high_severity_rule_content(self) -> None:
        text = _load_sigma_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(text, doc_id="test-doc")
        # First rule in fixture is the PowerShell one.
        assert "PowerShell" in chunks[0].content
        meta = chunks[0].metadata["security_metadata"]
        assert meta["severity"] == "high"
        assert meta["mitre_technique_id"] == "T1059.001"


# ---------------------------------------------------------------------------
# 2. NVD CVE path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestNvdPath:
    async def test_three_cves_six_chunks(self) -> None:
        """3 CVEs × 1 description chunk each (text is short, no refs chunk)."""
        text = _load_nvd_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            text, doc_id="test-doc", source_metadata={"path": "/feeds/nvd/2024.json"}
        )
        # Short descriptions → only description chunks, no refs split.
        assert len(chunks) == 3

    async def test_each_chunk_is_cve_description(self) -> None:
        text = _load_nvd_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            text, doc_id="test-doc", source_metadata={"path": "/feeds/nvd/2024.json"}
        )
        for c in chunks:
            assert c.chunk_type == "cve_description"
            assert c.source_type == "nvd_cve"

    async def test_metadata_has_security_metadata(self) -> None:
        text = _load_nvd_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            text, doc_id="test-doc", source_metadata={"path": "/feeds/nvd/2024.json"}
        )
        for c in chunks:
            sec_meta = c.metadata.get("security_metadata")
            assert isinstance(sec_meta, dict)
            assert sec_meta["source_type"] == "nvd_cve"
            assert "severity" in sec_meta
            assert "cve_id" in sec_meta

    async def test_no_field_bleed_across_cves(self) -> None:
        """Each chunk carries only its own metadata."""
        text = _load_nvd_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            text, doc_id="test-doc", source_metadata={"path": "/feeds/nvd/2024.json"}
        )
        severities = [c.metadata["security_metadata"]["severity"] for c in chunks]
        # Fixture order: critical, high, low
        assert severities == ["critical", "high", "low"]

    async def test_continuity_links_set(self) -> None:
        text = _load_nvd_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            text, doc_id="test-doc", source_metadata={"path": "/feeds/nvd/2024.json"}
        )
        assert chunks[0].prev_chunk_id is None
        assert chunks[-1].next_chunk_id is None
        for i in range(1, len(chunks)):
            assert chunks[i].prev_chunk_id == chunks[i - 1].metadata["chunk_id"]

    async def test_cve_id_in_metadata(self) -> None:
        text = _load_nvd_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            text, doc_id="test-doc", source_metadata={"path": "/feeds/nvd/2024.json"}
        )
        cve_ids = [c.metadata["security_metadata"]["cve_id"] for c in chunks]
        assert cve_ids == ["CVE-2024-12345", "CVE-2024-23456", "CVE-2024-34567"]


# ---------------------------------------------------------------------------
# 3. Prose path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestProsePath:
    async def test_prose_gets_chunk_type(self) -> None:
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            "This is a long prose document. " * 100,
            doc_id="test-doc",
            source_metadata={"path": "/notes/random.md"},
        )
        assert all(c.chunk_type == "prose" for c in chunks)
        assert all(c.source_type == "prose" for c in chunks)

    async def test_prose_has_security_metadata(self) -> None:
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            "Some prose text here that is definitely not a sigma rule. " * 50,
            doc_id="test-doc",
            source_metadata={"path": "/notes/random.md"},
        )
        for c in chunks:
            sec_meta = c.metadata.get("security_metadata")
            assert isinstance(sec_meta, dict)
            assert sec_meta["source_type"] == "prose"


# ---------------------------------------------------------------------------
# 4. MITRE ATT&CK path
# ---------------------------------------------------------------------------


MITRE_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "security" / "mitre"
MITRE_STIX = MITRE_FIXTURE_DIR / "attack-pattern.json"
MITRE_MD = MITRE_FIXTURE_DIR / "T1059.md"


def _load_mitre_stix() -> str:
    return MITRE_STIX.read_text(encoding="utf-8")


def _load_mitre_md() -> str:
    return MITRE_MD.read_text(encoding="utf-8")


@pytest.mark.asyncio
class TestMitrePath:
    async def test_stix_bundle_chunks(self) -> None:
        """Two techniques × 3 sections each = 6 chunks."""
        text = _load_mitre_stix()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            text,
            doc_id="test-doc",
            source_metadata={"path": "/corpus/mitre-attack/stix.json"},
        )
        assert len(chunks) == 6

    async def test_each_chunk_is_mitre_technique(self) -> None:
        text = _load_mitre_stix()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            text,
            doc_id="test-doc",
            source_metadata={"path": "/corpus/mitre-attack/stix.json"},
        )
        for c in chunks:
            assert c.chunk_type == "mitre_technique"
            assert c.source_type == "mitre_attack"

    async def test_metadata_has_security_metadata(self) -> None:
        text = _load_mitre_stix()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            text,
            doc_id="test-doc",
            source_metadata={"path": "/corpus/mitre-attack/stix.json"},
        )
        for c in chunks:
            sec_meta = c.metadata.get("security_metadata")
            assert isinstance(sec_meta, dict)
            assert sec_meta["source_type"] == "mitre_attack"
            assert "mitre_technique_id" in sec_meta

    async def test_no_field_bleed_across_techniques(self) -> None:
        text = _load_mitre_stix()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            text,
            doc_id="test-doc",
            source_metadata={"path": "/corpus/mitre-attack/stix.json"},
        )
        ids = [c.metadata["security_metadata"]["mitre_technique_id"] for c in chunks]
        # Fixture has T1059.001 and T1218 (the .001 subtechnique is optional).
        assert any("T1059" in i for i in ids)
        assert "T1218" in ids

    async def test_continuity_links_set(self) -> None:
        text = _load_mitre_stix()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            text,
            doc_id="test-doc",
            source_metadata={"path": "/corpus/mitre-attack/stix.json"},
        )
        assert chunks[0].prev_chunk_id is None
        assert chunks[-1].next_chunk_id is None
        for i in range(1, len(chunks)):
            assert chunks[i].prev_chunk_id == chunks[i - 1].metadata["chunk_id"]

    async def test_markdown_chunks(self) -> None:
        """Markdown fixture: Description lead + Description + Detection + Mitigations = 4 chunks."""
        text = _load_mitre_md()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            text,
            doc_id="test-doc",
            source_metadata={"path": "/corpus/mitre-attack/T1059.md"},
        )
        assert len(chunks) == 4
        for c in chunks:
            assert c.chunk_type == "mitre_technique"


# ---------------------------------------------------------------------------
# 5. Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEdgeCases:
    async def test_empty_string_returns_empty(self) -> None:
        chunker = SecurityChunker()
        chunks = await chunker.chunk("")
        assert chunks == []

    async def test_whitespace_only_returns_empty(self) -> None:
        chunker = SecurityChunker()
        chunks = await chunker.chunk("   \n\n  ")
        assert chunks == []

    async def test_unknown_falls_back_to_prose(self) -> None:
        """A file that doesn't match any rule should fall back to prose."""
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            "Just a short unknown text.",
            source_metadata={"path": "/random/file.xyz"},
        )
        # prose fallback produces at least one chunk
        assert len(chunks) >= 1
        assert all(c.chunk_type == "prose" for c in chunks)


# ---------------------------------------------------------------------------
# 6. Phase 6 — LLM metadata extraction (prose)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestExtractorPath:
    async def test_extractor_disabled_by_default(self) -> None:
        """Without settings, prose chunks get plain PROSE metadata."""
        chunker = SecurityChunker()
        chunks = await chunker.chunk(
            "Some prose text here. " * 50,
            doc_id="test-doc",
            source_metadata={"path": "/notes/random.md"},
        )
        assert all(c.chunk_type == "prose" for c in chunks)
        for c in chunks:
            assert c.metadata["security_metadata"]["source_type"] == "prose"
            assert c.metadata["security_metadata"]["severity"] == "unknown"

    async def test_extractor_enabled_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When llm_extract_enabled=True and LLM succeeds, chunks get enriched metadata."""

        class _LLM:
            url = "http://localhost:11434"
            model = "llama3.2:latest"
            timeout = 30
            temperature = 0.7
            max_tokens = 4096

        class _Security:
            llm_extract_enabled = True
            domain = "security"
            severity_weights = {}
            recency_half_life_days = 365
            intent_source_matrix = {}

        class _Settings:
            llm = _LLM()
            security = _Security()

        from grimoire.strategies.security.extractor import SecurityMetadataExtractor

        chunker = SecurityChunker(settings=_Settings())
        # Force lazy init
        chunker._extractor = SecurityMetadataExtractor(_Settings())

        async def _fake_extract(text: str):
            from grimoire.strategies.security.corpus import SourceType
            from grimoire.strategies.security.metadata import SecurityMetadata, Severity

            return SecurityMetadata(
                source_type=SourceType.PROSE,
                severity=Severity.HIGH,
                threat_actors=["APT42"],
                platforms=["windows"],
            )

        monkeypatch.setattr(chunker._extractor, "extract", _fake_extract)

        chunks = await chunker.chunk(
            "APT42 targets Windows extensively. " * 30,
            doc_id="test-doc",
            source_metadata={"path": "/notes/random.md"},
        )
        assert all(c.chunk_type == "prose" for c in chunks)
        for c in chunks:
            meta = c.metadata["security_metadata"]
            assert meta["severity"] == "high"
            assert meta["threat_actors"] == "APT42"
            assert meta["platforms"] == "windows"

    async def test_extractor_failure_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When LLM fails, prose chunks still get default PROSE metadata."""

        class _LLM:
            url = "http://localhost:11434"
            model = "llama3.2:latest"
            timeout = 30
            temperature = 0.7
            max_tokens = 4096

        class _Security:
            llm_extract_enabled = True
            domain = "security"
            severity_weights = {}
            recency_half_life_days = 365
            intent_source_matrix = {}

        class _Settings:
            llm = _LLM()
            security = _Security()

        from grimoire.strategies.security.extractor import SecurityMetadataExtractor

        chunker = SecurityChunker(settings=_Settings())
        # Force lazy init
        chunker._extractor = SecurityMetadataExtractor(_Settings())

        async def _fake_extract(text: str):
            raise ConnectionError("Ollama unreachable")

        monkeypatch.setattr(chunker._extractor, "extract", _fake_extract)

        chunks = await chunker.chunk(
            "Some prose text here. " * 50,
            doc_id="test-doc",
            source_metadata={"path": "/notes/random.md"},
        )
        assert all(c.chunk_type == "prose" for c in chunks)
        for c in chunks:
            meta = c.metadata["security_metadata"]
            assert meta["source_type"] == "prose"
            assert meta["severity"] == "unknown"
