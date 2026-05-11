"""Tests for the Phase 6 LLM metadata extractor.

Covers:

* Successful extraction (mocked LLM): all fields populated.
* Partial fields: only severity + platforms returned.
* Malformed JSON response: graceful fallback to empty metadata.
* Empty text: empty metadata.
* Missing fields in JSON: defaults handled.
* _parse_response markdown fence stripping.
"""

from __future__ import annotations

import pytest

from grimoire.strategies.security.corpus import SourceType
from grimoire.strategies.security.extractor import (
    LLMExtractionResult,
    SecurityMetadataExtractor,
)
from grimoire.strategies.security.metadata import SecurityMetadata, Severity


@pytest.fixture
def settings() -> object:
    """Minimal mock settings with an ``llm`` config."""

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

    return _Settings()


class TestExtract:
    async def test_full_success(
        self, settings: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extractor = SecurityMetadataExtractor(settings)

        async def _fake_llm(prompt: str) -> str:
            return """{
                "severity": "high",
                "mitre_technique_id": "T1059.001",
                "threat_actors": ["APT28"],
                "malware_families": ["Cobalt Strike"],
                "platforms": ["windows"],
                "ioc_types": ["sha256"],
                "content_date": "2024-06-15"
            }"""

        monkeypatch.setattr(
            extractor, "_call_llm", staticmethod(lambda p: _fake_llm(p))
        )

        meta = await extractor.extract("APT28 uses Cobalt Strike on Windows.")
        assert isinstance(meta, SecurityMetadata)
        assert meta.source_type == SourceType.PROSE
        assert meta.severity == Severity.HIGH
        assert meta.mitre_technique_id == "T1059.001"
        assert meta.threat_actors == ["APT28"]
        assert meta.malware_families == ["Cobalt Strike"]
        assert meta.platforms == ["windows"]
        assert meta.ioc_types == ["sha256"]
        assert meta.content_date is not None
        assert meta.content_date.year == 2024

    async def test_partial_fields(
        self, settings: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extractor = SecurityMetadataExtractor(settings)

        async def _fake_llm(prompt: str) -> str:
            return '{"severity": "medium", "platforms": ["linux"]}'

        monkeypatch.setattr(
            extractor, "_call_llm", staticmethod(lambda p: _fake_llm(p))
        )

        meta = await extractor.extract("Some linux notes.")
        assert meta.severity == Severity.MEDIUM
        assert meta.platforms == ["linux"]
        assert meta.threat_actors == []
        assert meta.mitre_technique_id is None

    async def test_empty_returns_empty(self, settings: object) -> None:
        extractor = SecurityMetadataExtractor(settings)
        meta = await extractor.extract("")
        assert meta == SecurityMetadata(source_type=SourceType.PROSE)

    async def test_malformed_json_fallback(
        self, settings: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extractor = SecurityMetadataExtractor(settings)

        async def _fake_llm(prompt: str) -> str:
            return "not json at all"

        monkeypatch.setattr(
            extractor, "_call_llm", staticmethod(lambda p: _fake_llm(p))
        )

        meta = await extractor.extract("Whatever text.")
        assert meta == SecurityMetadata(source_type=SourceType.PROSE)

    async def test_llm_call_failure_fallback(
        self, settings: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extractor = SecurityMetadataExtractor(settings)

        async def _fake_llm(prompt: str) -> str:
            raise ConnectionError("Ollama unreachable")

        monkeypatch.setattr(
            extractor, "_call_llm", staticmethod(lambda p: _fake_llm(p))
        )

        meta = await extractor.extract("Some prose.")
        assert meta == SecurityMetadata(source_type=SourceType.PROSE)


class TestParseResponse:
    def test_plain_json(self) -> None:
        extractor = SecurityMetadataExtractor.__new__(SecurityMetadataExtractor)
        result = extractor._parse_response('{"severity": "low"}')
        assert result.severity == "low"

    def test_markdown_fence(self) -> None:
        extractor = SecurityMetadataExtractor.__new__(SecurityMetadataExtractor)
        result = extractor._parse_response('```json\n{"severity": "critical"}\n```')
        assert result.severity == "critical"

    def test_unknown_severity(self) -> None:
        extractor = SecurityMetadataExtractor.__new__(SecurityMetadataExtractor)
        result = extractor._parse_response('{"severity": "banana"}')
        # validator should null-out invalid severity.
        assert result.severity is None

    def test_invalid_tid(self) -> None:
        extractor = SecurityMetadataExtractor.__new__(SecurityMetadataExtractor)
        result = extractor._parse_response('{"mitre_technique_id": "XYZ123"}')
        assert result.mitre_technique_id is None

    def test_bad_date(self) -> None:
        extractor = SecurityMetadataExtractor.__new__(SecurityMetadataExtractor)
        result = extractor._parse_response('{"content_date": "not-a-date"}')
        assert result.content_date is None


class TestLLMExtractionResult:
    def test_to_metadata_conversion(self) -> None:
        raw = LLMExtractionResult(
            severity="high",
            mitre_technique_id="T1059.001",
            threat_actors=["APT28"],
            platforms=["windows"],
            content_date="2024-06-15",
        )
        meta = raw.to_security_metadata()
        assert meta.severity == Severity.HIGH
        assert meta.mitre_technique_id == "T1059.001"
        assert meta.content_date is not None
        assert meta.content_date.year == 2024

    def test_to_metadata_unknown_severity(self) -> None:
        raw = LLMExtractionResult(severity="banana")
        meta = raw.to_security_metadata()
        assert meta.severity == Severity.UNKNOWN
