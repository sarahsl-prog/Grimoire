"""LLM-based security metadata extractor (Phase 6).

Extracts structured security metadata from unstructured prose using Ollama.
Never blocks ingest: on any parse failure it returns an empty
:class:`SecurityMetadata` and logs a warning.

Typical usage::

    from grimoire.config import get_settings
    from grimoire.strategies.security.extractor import SecurityMetadataExtractor

    settings = get_settings()
    extractor = SecurityMetadataExtractor(settings)
    meta = await extractor.extract(prose_text)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import httpx
from loguru import logger
from pydantic import BaseModel, Field, field_validator

from grimoire.strategies.security.corpus import SourceType
from grimoire.strategies.security.metadata import SecurityMetadata, Severity

if TYPE_CHECKING:
    from grimoire.config.settings import GrimoireSettings

__all__ = ["SecurityMetadataExtractor", "LLMExtractionResult"]

# Maximum text length sent to the LLM (characters) to avoid timeouts.
_MAX_SAMPLE_CHARS = 4000

# Prompt template — kept minimal because the model is small.
_EXTRACTION_PROMPT = """Extract security metadata from the following text.
Return ONLY a JSON object matching this schema (no markdown, no explanation):

{{
  "severity": "unknown",
  "mitre_technique_id": "T1059.001",
  "threat_actors": ["APT28", "Lazarus Group"],
  "malware_families": ["Cobalt Strike", "Emotet"],
  "platforms": ["windows", "linux", "aws"],
  "ioc_types": ["ipv4", "domain", "sha256"],
  "content_date": "2024-06-15"
}}

Rules:
- severity must be one of: critical, high, medium, low, info, unknown
- mitre_technique_id must match T\\d{4}(\\.\\d{3})? or null
- content_date must be ISO-8601 (YYYY-MM-DD) or null
- All list fields default to [] when absent
- Return null for any unknown field instead of "N/A" or "unknown"

Text:
---
{text}
---
"""


class LLMExtractionResult(BaseModel):
    """Schema for the LLM JSON-mode response."""

    model_config = {"extra": "ignore"}

    severity: Optional[str] = Field(default=None)
    mitre_technique_id: Optional[str] = Field(default=None)
    threat_actors: List[str] = Field(default_factory=list)
    malware_families: List[str] = Field(default_factory=list)
    platforms: List[str] = Field(default_factory=list)
    ioc_types: List[str] = Field(default_factory=list)
    content_date: Optional[str] = Field(default=None)

    @field_validator("severity", mode="before")
    @classmethod
    def _validate_severity(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip().lower()
        allowed = {"critical", "high", "medium", "low", "info", "unknown"}
        return s if s in allowed else None

    @field_validator("mitre_technique_id", mode="before")
    @classmethod
    def _validate_tid(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        if re.match(r"^T\d{4}(?:\.\d{3})?$", s):
            return s
        return None

    @field_validator("content_date", mode="before")
    @classmethod
    def _validate_date(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        # Accept YYYY-MM-DD or broader ISO-8601.
        iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
        return iso_match.group(1) if iso_match else None

    def to_security_metadata(self) -> SecurityMetadata:
        """Convert to :class:`SecurityMetadata`."""

        severity = Severity.UNKNOWN
        if self.severity:
            try:
                severity = Severity(self.severity)
            except ValueError:
                pass

        content_dt: Optional[datetime] = None
        if self.content_date:
            content_dt = datetime.strptime(self.content_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )

        return SecurityMetadata(
            source_type=SourceType.PROSE,
            severity=severity,
            mitre_technique_id=self.mitre_technique_id,
            threat_actors=self.threat_actors,
            malware_families=self.malware_families,
            platforms=[p.lower() for p in self.platforms],
            ioc_types=[t.lower() for t in self.ioc_types],
            content_date=content_dt,
        )


class SecurityMetadataExtractor:
    """Extract security metadata from prose via Ollama LLM.

    Args:
        settings: Grimoire configuration (uses ``settings.llm`` for endpoint).
    """

    def __init__(self, settings: GrimoireSettings) -> None:
        self.settings = settings
        self.llm_config = settings.llm
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.llm_config.timeout,
                headers={"Content-Type": "application/json"},
            )
        return self._client

    async def _close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def extract(self, text: str) -> SecurityMetadata:
        """Extract security metadata from prose text.

        Args:
            text: Raw prose document text.

        Returns:
            Populated :class:`SecurityMetadata` on success, or an empty one
            with ``source_type=SourceType.PROSE`` on failure.
        """

        if not text or not text.strip():
            return SecurityMetadata(source_type=SourceType.PROSE)

        sample = text[:_MAX_SAMPLE_CHARS]
        prompt = _EXTRACTION_PROMPT.replace("{text}", sample)

        try:
            response_text = await self._call_llm(prompt)
        except Exception as e:
            logger.warning("SecurityMetadataExtractor LLM call failed: {}", e)
            return SecurityMetadata(source_type=SourceType.PROSE)

        try:
            parsed = self._parse_response(response_text)
        except Exception as e:
            logger.warning("SecurityMetadataExtractor parse failed: {}", e)
            return SecurityMetadata(source_type=SourceType.PROSE)

        return parsed.to_security_metadata()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _call_llm(self, prompt: str) -> str:
        client = await self._get_client()

        url = f"{self.llm_config.url.rstrip('/')}/api/generate"
        payload: Dict[str, Any] = {
            "model": self.llm_config.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.0,
                "num_predict": 512,
            },
        }

        response = await client.post(url, json=payload)
        response.raise_for_status()
        result = response.json()

        if "response" not in result:
            raise ValueError(f"Unexpected Ollama response keys: {list(result.keys())}")
        return str(result["response"])

    def _parse_response(self, raw: str) -> LLMExtractionResult:
        """Parse raw LLM JSON response."""

        # Strip markdown fences if the model ignored instructions.
        cleaned = raw.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        data = json.loads(cleaned)
        return LLMExtractionResult(**data)
