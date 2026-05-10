"""Tests for the Phase 3 SecurityChunker.

Covers:

* Sigma path: chunk count, ``chunk_type``, ``source_type``, metadata
  payload shape, no field bleed across rules,
* Prose path: delegates to ``RecursiveCharacterTextSplitter``, stamps
  ``chunk_type="prose"``,
* NotImplementedError for NVD CVE / MITRE ATT&CK (until Phases 4–5),
* Empty text returns empty list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grimoire.strategies.security.chunker import SecurityChunker


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "security" / "sigma"
SAMPLE_RULES = FIXTURE_DIR / "sample_rules.yml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_sample() -> str:
    return SAMPLE_RULES.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Sigma path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSigmaPath:
    async def test_four_chunks_for_four_rules(self) -> None:
        text = _load_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(text, doc_id="test-doc")
        assert len(chunks) == 4

    async def test_each_chunk_is_sigma_rule(self) -> None:
        text = _load_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(text, doc_id="test-doc")
        for c in chunks:
            assert c.chunk_type == "sigma_rule"
            assert c.source_type == "sigma_rule"

    async def test_metadata_has_security_metadata(self) -> None:
        text = _load_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(text, doc_id="test-doc")
        for c in chunks:
            sec_meta = c.metadata.get("security_metadata")
            assert isinstance(sec_meta, dict)
            assert sec_meta["source_type"] == "sigma_rule"
            assert "severity" in sec_meta

    async def test_continuity_links_set(self) -> None:
        text = _load_sample()
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
        text = _load_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(text, doc_id="test-doc")
        severities = [c.metadata["security_metadata"]["severity"] for c in chunks]
        # Fixture order: high, medium, critical, low
        assert severities == ["high", "medium", "critical", "low"]

    async def test_chunk_index_sequential(self) -> None:
        text = _load_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(text, doc_id="test-doc")
        for i, c in enumerate(chunks):
            assert c.index == i

    async def test_high_severity_rule_content(self) -> None:
        text = _load_sample()
        chunker = SecurityChunker()
        chunks = await chunker.chunk(text, doc_id="test-doc")
        # First rule in fixture is the PowerShell one.
        assert "PowerShell" in chunks[0].content
        meta = chunks[0].metadata["security_metadata"]
        assert meta["severity"] == "high"
        assert meta["mitre_technique_id"] == "T1059.001"


# ---------------------------------------------------------------------------
# 2. Prose path
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
# 3. Not-yet-implemented source types
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestUnimplementedPaths:
    async def test_nvd_cve_raises_not_implemented(self) -> None:
        chunker = SecurityChunker()
        with pytest.raises(NotImplementedError, match="Phase 4"):
            await chunker.chunk(
                '{"cve": {"id": "CVE-2024-12345"}}',
                source_metadata={"path": "/feeds/nvd/2024.json"},
            )

    async def test_mitre_attack_raises_not_implemented(self) -> None:
        chunker = SecurityChunker()
        with pytest.raises(NotImplementedError, match="Phase 5"):
            await chunker.chunk(
                "---\nkind: attack-pattern\n---\nDescription here.",
                source_metadata={"path": "/corpus/mitre-attack/T1059.md"},
            )


# ---------------------------------------------------------------------------
# 4. Edge cases
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
