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
| `prose`, `unknown` | `_chunk_prose` | `RecursiveCharacterTextSplitter` |
| `nvd_cve` | Not yet implemented | Phase 4 |
| `mitre_attack` | Not yet implemented | Phase 5 |

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

## Future phases

* **Phase 4** — NVD CVE: two chunks per CVE (description + references).
* **Phase 5** — MITRE ATT&CK: one chunk per H2 section (Description,
  Procedure Examples, Mitigations, Detection).
