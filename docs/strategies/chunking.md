# Chunking in the Security Domain

The security domain uses the same `Chunk` / `Chunker` abstractions as the
rest of Grimoire, but adds **source-type dispatch**: a single
`SecurityChunker` detects what kind of content it is processing and picks
the most appropriate parser + chunking strategy.

---

## Source-type dispatch

`SecurityChunker.chunk()` calls `detect_source_type()` on the incoming text
and routes to the appropriate handler:

| Source type | Handler | Chunking strategy |
|---|---|---|
| `sigma_rule` | `_chunk_sigma` | One chunk per rule |
| `nvd_cve` | `_chunk_nvd` | One chunk per CVE (description; refs inline) |
| `mitre_attack` | `_chunk_mitre` | One chunk per section (Description, Detection, Mitigations) |
| `prose`, `unknown`, `ioc_list` | `_chunk_prose` | `RecursiveCharacterTextSplitter` |

Each handler stamps chunks with `chunk_type` and `source_type` and attaches
a `security_metadata` dict (flattened via `SecurityMetadata.to_chromadb_metadata()`)
to every chunk so that vector-side filters can query on severity, MITRE
technique, CVE id, etc.

---

## Sigma rule chunking

### Philosophy

Sigma rules are already atomic pieces of threat intelligence — each rule
describes a single detection. Splitting a rule across multiple chunks would
destroy its semantic coherence, so `SecurityChunker` produces **exactly one
chunk per rule** even if the rule is tiny.

### What goes into the chunk

The chunk `content` is a human-readable summary produced by
`parse_sigma()`. It includes:

* Title, ID, status, level
* Logsource (product, service, category)
* Detection selectors and condition
* Tags and false positives
* Author and references (if present)

### Metadata attached to each chunk

```python
{
    "security_metadata": {
        "source_type": "sigma_rule",
        "severity": "high",
        "tlp_level": "white",
        "mitre_technique_id": "T1059.001",
        "mitre_tactic": "execution",
        "platforms": "windows",
        "log_sources": "sysmon|process_creation",
        "detection_categories": "condition: selection|Legitimate administrative scripts",
        ...
    },
    "strategy": "sigma_rule",
}
```

The `security_metadata` dict is merged into the ChromaDB payload by
`_embed_and_store()` so vector queries can filter on any of these fields.

---

## Example: a Sigma rule and its chunk

### Input rule (`tests/fixtures/security/sigma/sample_rules.yml`, first rule)

```yaml
title: Suspicious PowerShell Download
id: e71071b4-c276-4e74-8d23-0d7f9b6ca9b1
status: stable
description: Detects suspicious PowerShell download commands
author: Florian Roth
level: high
logsource:
    product: windows
    service: sysmon
    category: process_creation
detection:
    selection:
        CommandLine|contains:
            - 'IEX(New-Object Net.WebClient).downloadString'
            - 'Invoke-Expression'
            - 'bitsadmin /transfer'
    condition: selection
falsepositives:
    - Legitimate administrative scripts
    - Software deployment tools
tags:
    - attack.execution
    - attack.t1059.001
    - attack.t1105
```

### Resulting chunk

```python
Chunk(
    content="""Title: Suspicious PowerShell Download
ID: e71071b4-c276-4e74-8d23-0d7f9b6ca9b1
Status: stable
Level: high
Description: Detects suspicious PowerShell download commands
Author: Florian Roth
Logsource: product=windows | service=sysmon | category=process_creation
Detection:
  selection: {'CommandLine|contains': [...]}
Condition: selection
Tags: attack.execution, attack.t1059.001, attack.t1105
Falsepositives: Legitimate administrative scripts, Software deployment tools""",
    token_count=42,
    index=0,
    chunk_type="sigma_rule",
    source_type="sigma_rule",
    metadata={
        "security_metadata": {
            "source_type": "sigma_rule",
            "severity": "high",
            "mitre_technique_id": "T1059.001",
            "mitre_tactic": "execution",
            "platforms": "windows",
            "log_sources": "sysmon|process_creation",
            "detection_categories": "condition: selection|Legitimate administrative scripts|Software deployment tools",
            "tlp_level": "white",
            "cve_id": "",
            "cvss_score": 0.0,
            "content_date": "",
            "source_url": "",
            "cwe_ids": "",
            "threat_actors": "",
        },
        "strategy": "sigma_rule",
        "chunk_id": "uuid-here",
    },
)
```

---

## Prose fallback

When `detect_source_type()` returns `prose` or `unknown`,
`SecurityChunker._chunk_prose()` delegates to the existing
`RecursiveCharacterTextSplitter`. Every resulting chunk is stamped with
`chunk_type="prose"` and gets an empty `SecurityMetadata(source_type=PROSE)`
so that downstream code always sees a consistent metadata shape.

---

## Continuity links

Both Sigma and prose paths call `Chunker._set_continuity_links()` so that
`prev_chunk_id` / `next_chunk_id` are available for context restoration
during retrieval. For Sigma rules this is usually uninteresting (N=1), but
for multi-rule files and prose documents the links are fully populated.

---

## NVD CVE chunking

### Philosophy

CVE records are structured security advisories. The chunker produces one
chunk per CVE containing a human-readable summary (description, CVSS
score, severity, CWEs, affected products, references). For unusually long
descriptions a separate references chunk may be split off, but typical
NVD records fit comfortably in a single chunk.

### What goes into the chunk

The chunk `content` is formatted by `parse_cve()` and includes:

* CVE id
* Description (English)
* CVSS score and severity
* CWE list
* Affected product names (from CPE)
* Reference URLs
* Published date

### Metadata attached to each chunk

```python
{
    "security_metadata": {
        "source_type": "nvd_cve",
        "severity": "critical",
        "tlp_level": "white",
        "cve_id": "CVE-2024-12345",
        "cvss_score": 9.8,
        "mitre_technique_id": "",
        "mitre_tactic": "",
        "cwe_ids": "CWE-78",
        "threat_actors": "",
        "platforms": "",
        "content_date": "2024-01-15T00:00:00+00:00",
        "source_url": "",
    },
    "strategy": "cve_description",
}
```

### Supported input shapes

* **Bulk feed** `{"vulnerabilities": [{"cve": {...}}, ...]}` — annual
  download or API paged response.
* **Modern wrapper** `{"cve": {...}}` — single-record API v2.0.
* **Legacy key-value** `{"CVE-YYYY-NNNN": {...}}` — older dumps.

CVSS extraction uses priority order: **v3.1 → v3.0 → v2.0**.

---

## MITRE ATT&CK chunking

### Philosophy

ATT&CK techniques are rich documents with multiple sections (Description,
Detection, Mitigations). Splitting by H2 heading keeps each chunk
semantically coherent while allowing retrieval to surface the most relevant
section for a query.

All chunks for a single technique share the same `mitre_technique_id` so
re-rankers and post-processing can group them into a single logical unit.

### What goes into each chunk

Each chunk's `content` is prefixed with the technique name and id so that
even when viewed in isolation the reader knows what technique the section
belongs to::

    Technique: Command and Scripting Interpreter: PowerShell
    ID: T1059.001
    Section: Detection
    Monitor for PowerShell execution, including use of the -enc flag. ...

### Metadata attached to each chunk

```python
{
    "security_metadata": {
        "source_type": "mitre_attack",
        "severity": "unknown",
        "tlp_level": "white",
        "mitre_technique_id": "T1059.001",
        "mitre_tactic": "execution",
        "platforms": "windows",
        "cve_id": "",
        "cvss_score": 0.0,
        "content_date": "",
    },
    "strategy": "mitre_technique",
}
```

### Supported input shapes

* **STIX 2.1 bundle** — `{"type": "bundle", "objects": [{"type": "attack-pattern", ...}]}`.
  The parser inspects `external_references` for `external_id`,
  `kill_chain_phases` for tactic, and `x_mitre_platforms` / `x_mitre_detection`
  / `x_mitre_mitigations` for section content.
* **Markdown with YAML frontmatter** — files exported from the ATT&CK
  website or generated by `mitre/cti` scripts. Frontmatter keys
  `attack_id`, `tactic`, and `platforms` drive metadata; H2 headings
  drive section splitting.

---

## Future phases

* **Phase 6** — Prose fallback + LLM metadata extractor.