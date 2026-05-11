# LLM Metadata Extraction for Security Strategies

Overview
--------

The :class:`grimoire.strategies.security.extractor.SecurityMetadataExtractor`
uses a local Ollama model to classify security-related prose into structured
:class:`grimoire.strategies.security.metadata.SecurityMetadata`.  It runs
**after** source-type detection has classified a document as ``prose`` or
``unknown`` and **before** the text is chunked, so every prose chunk inherits
the extracted metadata.

This document explains how the extractor works, how to enable/disable it, and
how it behaves when the LLM is unreachable or returns garbage.

When the extractor runs
-----------------------

The extractor is invoked inside :meth:`SecurityChunker.chunk` only when **all**
of the following are true:

1. ``source_type`` is ``PROSE``, ``UNKNOWN``, or ``IOC_LIST``.
2. ``settings`` is passed to :class:`SecurityChunker` (e.g.
   ``SecurityChunker(settings=app_settings)``).
3. ``settings.security.llm_extract_enabled`` is ``True``.

If any of the above is false, the chunker skips extraction and uses the
default ``SecurityMetadata(source_type=SourceType.PROSE)``.

Configuration
-------------

``llm_extract_enabled`` lives on ``SecurityConfig``:

```python
# grimoire/config/settings.py
class SecurityConfig(BaseModel):
    domain: str = "security"
    severity_weights: dict[str, float] = {}
    recency_half_life_days: int = 365
    intent_source_matrix: dict[str, float] = {}
    llm_extract_enabled: bool = False          # feature flag
```

Toggle it in ``.env`` or ``config.yaml``:

```yaml
security:
  llm_extract_enabled: true
```

LLM settings are read from the shared ``LLMConfig`` (``settings.llm.url``,
``settings.llm.model``, ``settings.llm.timeout``).  The extractor does not use
separate LLM credentials — it reuses whatever model the rest of the app is
configured for.

Extraction prompt
-----------------

The prompt is a zero-shot classification task.  It shows the model a JSON
schema with example values, then asks it to fill in the actual values for the
provided text.

Supported fields:

| Field | Type | Constraints |
|---|---|---|
| ``severity`` | ``str`` | ``critical``, ``high``, ``medium``, ``low``, ``info``, ``unknown`` |
| ``mitre_technique_id`` | ``str`` | Must match ``T\d{4}(\.\d{3})?`` or null |
| ``threat_actors`` | ``list[str]`` | Empty list if none found |
| ``malware_families`` | ``list[str]`` | Empty list if none found |
| ``platforms`` | ``list[str]`` | e.g. ``[windows, linux, aws]`` |
| ``ioc_types`` | ``list[str]`` | e.g. ``[ipv4, domain, sha256]`` |
| ``content_date`` | ``str`` | ISO-8601 date or null |

The model is instructed to return **only** a JSON object with no markdown
fences or extra text.

Failure modes (handled)
-----------------------

| Scenario | Behaviour |
|---|---|
| LLM unreachable (timeout, connection refused, etc.) | Log warning, return empty ``SecurityMetadata(source_type=PROSE)``. Chunker continues. |
| LLM returns non-JSON | Log warning, return empty metadata. Chunker continues. |
| LLM returns JSON with unknown ``severity`` value | Normalized to ``Severity.UNKNOWN``. |
| LLM returns invalid ``mitre_technique_id`` | Rejected (None). |
| LLM returns invalid ``content_date`` | Rejected (None). |

The chunker treats a failed extraction the same as disabled extraction: it
falls back to default metadata and the document is still ingested.

Usage (standalone)
------------------

```python
from grimoire.config.settings import SecurityConfig
from grimoire.strategies.security.extractor import SecurityMetadataExtractor
from grimoire.strategies.security.metadata import SecurityMetadata

settings = ...  # your GrimoireSettings instance
extractor = SecurityMetadataExtractor(settings)
meta: SecurityMetadata = await extractor.extract(
    "APT28 has been observed using Cobalt Strike against Windows targets."
)
print(meta.severity)          # Severity.HIGH
print(meta.threat_actors)     # ["APT28"]
print(meta.mitre_technique_id)# None  (model didn't spot one)
```

Metadata inheritance
--------------------

When extraction succeeds the :class:`SecurityMetadata` is applied to **every**
chunk produced by the prose splitter.  The metadata is serialised via
:meth:`SecurityMetadata.to_chromadb_metadata()` (pipe-joined lists, scalar
defaults) before being attached to each chunk.

Example ChromaDB payload for a prose chunk with extracted metadata:

```json
{
  "source_type": "prose",
  "severity": "high",
  "tlp_level": "clear",
  "threat_actors": "APT28",
  "platforms": "windows",
  "mitre_technique_id": "",
  "content_date": "",
  "cwe_ids": "",
  "cve_id": "",
  "cvss_score": 0.0,
  "source_url": ""
}
```

Testing
-------

The extractor tests mock the LLM call to avoid talking to a real model:

```python
async def _fake_llm(prompt: str) -> str:
    return '{"severity": "high", "threat_actors": ["APT28"]}'

monkeypatch.setattr(extractor, "_call_llm", staticmethod(_fake_llm))
```

Tests cover:

* Full field extraction
* Partial field extraction
* Malformed JSON fallback
* LLM failure fallback
* Empty text
* Markdown fence stripping
* Invalid severity / technique-id / date rejection

Future work
-----------

* Batch extraction — send multiple prompts in one LLM call when processing a
  folder of prose files.
* Structured output (e.g. ``Outlines`` or ``JSONSchema``) instead of prompt
  engineering.
* Cached extraction results keyed by content hash to avoid re-prompting
  unchanged files.
* Domain-specific models fine-tuned on security prose.
